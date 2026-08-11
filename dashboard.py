"""Web dashboard for the Progressive automation.

Why this exists
---------------
The CLI runs records one at a time. This dashboard lets you:
  * paste your proxy list and flip proxy / bandwidth / headless on or off,
  * choose how many Chrome windows run at once (parallel workers),
  * upload the records JSON and press Start,
  * watch results fill in live, all written to the one shared results file.

It reuses the automation logic from progressive_dynamic_automation_v2.py, so
there is a single source of truth for how a record is processed. Nothing here
re-implements the scraping - run_one_record() does the actual work per record.

Run it:
    pip install flask
    python dashboard.py
    open http://127.0.0.1:5000

Later on the VPS: same thing behind Xvfb (virtual screen) so the visible
Chrome windows have somewhere to render. We wire that when you're ready.
"""

import os
import io
import re
import csv
import json
import time
import secrets
import threading
from functools import wraps
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request, jsonify, Response, send_file, session, redirect
from werkzeug.security import generate_password_hash, check_password_hash

import progressive_dynamic_automation_v2 as m

# Run everything from this file's folder so the shared results file and error
# screenshots land next to the script, and the import above resolves.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

# Host/port the dashboard listens on. In a deployment (Coolify/Docker) these
# come from env: PORT is injected by the platform, HOST must be 0.0.0.0 so the
# proxy can reach the container. Locally they default to 127.0.0.1:5050.
PORT = int(os.environ.get("PORT") or os.environ.get("DASHBOARD_PORT") or "5050")
HOST = os.environ.get("HOST", "127.0.0.1")

app = Flask(__name__)


# --- Authentication ---------------------------------------------------------
# Login credentials + the session secret live in the DB settings table so they
# persist across restarts and travel with the .db to the VPS. First run seeds
# a default admin/admin - change it in the dashboard's Settings panel.
def _init_auth():
    secret = m.get_setting("flask_secret")
    if not secret:
        secret = secrets.token_hex(32)
        m.set_setting("flask_secret", secret)
    app.secret_key = secret
    if not m.get_setting("auth_user"):
        m.set_setting("auth_user", "admin")
        m.set_setting("auth_pass", generate_password_hash("admin"))


def check_login(username, password):
    if username != m.get_setting("auth_user", "admin"):
        return False
    return check_password_hash(m.get_setting("auth_pass", ""), password)


# Endpoints reachable without being logged in.
_PUBLIC = {"login", "do_login", "static"}


@app.before_request
def _require_login():
    if request.endpoint in _PUBLIC:
        return
    if session.get("user"):
        return
    # Not logged in: send the page to the login screen, APIs get a 401.
    if request.method == "GET" and request.path == "/":
        return redirect("/login")
    return jsonify(error="Login required."), 401


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return jsonify(error="Login required."), 401
        return f(*args, **kwargs)
    return wrapper

# Single in-memory job. One run at a time is plenty for this tool.
JOB = {
    "running": False,
    "input_name": "",
    "total": 0,
    "processed": 0,
    "skipped": 0,
    "workers": 0,
    "bandwidth_kb": 0.0,
    "message": "Idle. Upload a records file and press Start.",
    "results": [],       # live, in input order
    "stop": False,
    "started_at": 0.0,
}
JOB_LOCK = threading.Lock()

# Statuses that mean "leave this record alone next time".
DONE = m.DONE_STATUSES


# --- Flexible record input (CSV / paste / JSON) -----------------------------
# Accept messy real-world data: columns in any order, header names that vary,
# extra columns we don't need (car_make etc.). We map each header to a known
# field and keep the rest. Only name + address + zip are required to run - the
# vehicles come from Progressive, so the car columns are optional.
FIELD_ALIASES = {
    "first_name": ["first_name", "firstname", "first", "fname", "givenname"],
    "middle_initial": ["middle_initial", "middle_name", "middle", "mi", "middlename"],
    "last_name": ["last_name", "lastname", "last", "lname", "surname"],
    "address": ["address", "address1", "addressline1", "street", "streetaddress", "addr"],
    "city": ["city", "town"],
    "state": ["state", "st", "province"],
    "zip": ["zip", "zipcode", "postal", "postalcode", "zip4", "postcode"],
    "phone": ["phone", "phonenumber", "phone1", "tel", "telephone", "mobile"],
    "email": ["email", "emailaddress", "mail"],
    "dob": ["dob", "dateofbirth", "birthdate", "birthday"],
}
REQUIRED_FIELDS = ["first_name", "last_name", "address", "zip"]


def _norm(h):
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


_HEADER_TO_FIELD = {}
for _canon, _aliases in FIELD_ALIASES.items():
    _HEADER_TO_FIELD[_norm(_canon)] = _canon
    for _a in _aliases:
        _HEADER_TO_FIELD[_norm(_a)] = _canon


def parse_delimited(text):
    """Parse pasted/CSV records (comma OR tab separated) with a header row.

    Returns (entries, error). Columns may be in any order and extra columns are
    ignored. Missing a required column (name/address/zip) is a clear error.
    """
    text = (text or "").strip()
    if not text:
        return [], "No data provided."
    first_line = text.splitlines()[0]
    delim = "\t" if first_line.count("\t") >= first_line.count(",") else ","
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delim)
            if any((c or "").strip() for c in r)]
    if len(rows) < 2:
        return [], "Need a header row plus at least one data row."

    col_field = {i: _HEADER_TO_FIELD.get(_norm(h)) for i, h in enumerate(rows[0])}
    present = {f for f in col_field.values() if f}
    missing = [f for f in REQUIRED_FIELDS if f not in present]
    if missing:
        seen = ", ".join(h.strip() for h in rows[0] if h.strip())
        return [], (f"Missing required column(s): {', '.join(missing)}. "
                    f"Headers found: {seen}")

    entries = []
    for r in rows[1:]:
        entry = {}
        for i, val in enumerate(r):
            field = col_field.get(i)
            if field:
                entry[field] = (val or "").strip()
        # Only keep rows that actually have the required values filled in.
        if all(entry.get(f) for f in REQUIRED_FIELDS):
            entries.append(entry)
    if not entries:
        return [], "No rows had all of name, address and zip filled in."
    return entries, None


def parse_records(raw_text, filename=""):
    """Turn an uploaded file OR pasted text into a list of record dicts.

    JSON (the original format) still works; anything else is treated as
    CSV/TSV with a header row.
    """
    name = (filename or "").lower()
    stripped = (raw_text or "").lstrip()
    looks_json = name.endswith(".json") or stripped[:1] in ("[", "{")
    if looks_json:
        try:
            data = json.loads(raw_text)
        except Exception as e:
            return [], f"Could not parse JSON: {e}"
        if isinstance(data, dict):
            data = [data]
        return (data, None) if data else ([], "The JSON file is empty.")
    return parse_delimited(raw_text)


def _placeholder(entry, status="pending"):
    return {
        "first_name": entry.get("first_name", ""),
        "middle_initial": entry.get("middle_initial", ""),
        "last_name": entry.get("last_name", ""),
        "phone": entry.get("phone", ""),
        "email": entry.get("email", ""),
        "address": entry.get("address", ""),
        "zip": entry.get("zip", ""),
        "dob": entry.get("dob", ""),
        "status": status,
        "address_recognized_vehicles": [],
    }


def run_job(entries, config):
    """Process all not-yet-done entries with a pool of parallel browsers."""
    # Push the UI toggles into the automation module.
    m.SAVE_BANDWIDTH = config["save_bandwidth"]
    proxy_list = m.parse_proxies(m.PROXIES) if config["use_proxies"] else []
    headless = config["headless"]
    workers = max(1, int(config["concurrency"]))

    slots = [None] * len(entries)
    todo = []  # (slot_index, entry)
    skipped = 0
    for i, entry in enumerate(entries):
        # Per-record DB lookup (indexed) rather than loading the whole DB into
        # memory - keeps resume flat even with millions of stored records.
        prev = m.get_result(entry)
        if prev and prev.get("status") in DONE:
            slots[i] = prev
            skipped += 1
        else:
            slots[i] = _placeholder(entry)
            todo.append((i, entry))

    with JOB_LOCK:
        JOB.update(
            running=True, stop=False, total=len(entries), processed=skipped,
            skipped=skipped, workers=workers, bandwidth_kb=0.0,
            results=slots, started_at=time.time(),
            message=f"Running {len(todo)} record(s) across {workers} browser(s); "
                    f"{skipped} already done.",
        )

    def worker(slot_index, entry, proxy_index):
        if JOB["stop"]:
            slots[slot_index]["status"] = "cancelled"
            return
        proxy = proxy_list[proxy_index % len(proxy_list)] if proxy_list else None
        proxy = m.with_session(proxy)  # unique sticky IP per record (Proxy-Cheap)
        result, bw = m.run_one_record(entry, proxy=proxy, headless=headless)
        slots[slot_index] = result
        m.save_result(result)  # single-row upsert; DB handles concurrency
        with JOB_LOCK:
            JOB["processed"] += 1
            JOB["bandwidth_kb"] += bw.get("total", 0) / 1024.0

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(worker, slot_index, entry, pi)
                for pi, (slot_index, entry) in enumerate(todo)
            ]
            for fut in futures:
                fut.result()
    except Exception as e:
        with JOB_LOCK:
            JOB["message"] = f"Run stopped with an error: {e}"
    finally:
        with JOB_LOCK:
            JOB["running"] = False
            counts = {}
            for r in slots:
                counts[r["status"]] = counts.get(r["status"], 0) + 1
            done_note = "Stopped." if JOB["stop"] else "Finished."
            JOB["message"] = f"{done_note} Summary: {counts}"


@app.route("/start", methods=["POST"])
def start():
    with JOB_LOCK:
        if JOB["running"]:
            return jsonify(error="A run is already in progress."), 409

    # Records can arrive two ways: pasted into the textarea, or an uploaded
    # file (.json, .csv, or .txt). Paste wins if both are given.
    pasted = (request.form.get("records_text") or "").strip()
    if pasted:
        entries, err = parse_records(pasted, "pasted.csv")
    else:
        upload = request.files.get("records")
        if not upload:
            return jsonify(error="Upload a file or paste records first."), 400
        raw = upload.stream.read().decode("utf-8", errors="replace")
        entries, err = parse_records(raw, upload.filename or "")
    if err:
        return jsonify(error=err), 400
    if not entries:
        return jsonify(error="No usable records found."), 400

    # Persist the pasted proxies onto the module so run_one_record uses them.
    m.PROXIES = request.form.get("proxies", m.PROXIES)

    config = {
        "use_proxies": request.form.get("use_proxies") == "true",
        "save_bandwidth": request.form.get("save_bandwidth") == "true",
        "headless": request.form.get("headless") == "true",
        "concurrency": request.form.get("concurrency", "10"),
    }

    with JOB_LOCK:
        JOB["input_name"] = "pasted data" if pasted else (
            request.files.get("records").filename or "uploaded file")

    threading.Thread(target=run_job, args=(entries, config), daemon=True).start()
    return jsonify(ok=True)


FAILED_STATUSES = ("error", "blocked", "cancelled")


def _config_from_form():
    return {
        "use_proxies": request.form.get("use_proxies") == "true",
        "save_bandwidth": request.form.get("save_bandwidth") == "true",
        "headless": request.form.get("headless") == "true",
        "concurrency": request.form.get("concurrency", "10"),
    }


@app.route("/retry", methods=["POST"])
def retry():
    """Re-run only the failed/blocked records, reusing the data already saved.

    We hand run_job the FULL record set (rebuilt from the results file) so the
    successful ones are preserved and skipped, and only the failed ones run
    again - handy when a bad proxy IP errored a few records."""
    with JOB_LOCK:
        if JOB["running"]:
            return jsonify(error="A run is already in progress."), 409
        results = list(JOB["results"])
    if not results:
        results = m.load_all()
    failed = [r for r in results if r.get("status") in FAILED_STATUSES]
    if not failed:
        return jsonify(error="No failed records to retry."), 400

    entries = [{
        "first_name": r.get("first_name", ""),
        "middle_initial": r.get("middle_initial", ""),
        "last_name": r.get("last_name", ""),
        "phone": r.get("phone", ""),
        "email": r.get("email", ""),
        "address": r.get("address", ""),
        "zip": r.get("zip", ""),
        "dob": r.get("dob", ""),
    } for r in results]

    m.PROXIES = request.form.get("proxies", m.PROXIES)
    with JOB_LOCK:
        JOB["input_name"] = f"retry ({len(failed)} failed)"
    threading.Thread(target=run_job, args=(entries, _config_from_form()), daemon=True).start()
    return jsonify(ok=True, retrying=len(failed))


@app.route("/stop", methods=["POST"])
def stop():
    with JOB_LOCK:
        JOB["stop"] = True
        JOB["message"] = "Stop requested - workers already open will finish; " \
                         "no new ones start."
    return jsonify(ok=True)


def _check_one(line):
    """Test a single proxy line against an HTTPS URL.

    Understands every format parse_proxy_line does (incl. Proxy-Cheap
    user:pass@host:port). HTTPS on purpose: it exercises the same CONNECT
    tunnel Progressive needs, so a proxy that passes here can reach the site.
    """
    d = m.parse_proxy_line(line)
    if not d:
        return line, False
    host_port = d["server"].split("://", 1)[1]
    auth = f'{d["username"]}:{d["password"]}@' if d.get("username") else ""
    url = f"http://{auth}{host_port}"
    import urllib.request
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": url, "https": url})
    )
    try:
        with opener.open("https://api.ipify.org", timeout=8):
            return line, True
    except Exception:
        return line, False


@app.route("/check", methods=["POST"])
def check_proxies():
    """Return only the pasted proxies that actually work right now."""
    raw = (request.form.get("proxies") or "").strip()
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.startswith("#")]
    if not lines:
        return jsonify(error="Paste some proxies first."), 400
    alive = []
    with ThreadPoolExecutor(max_workers=30) as pool:
        for line, ok in pool.map(_check_one, lines):
            if ok:
                alive.append(line)
    return jsonify(total=len(lines), alive=len(alive), working="\n".join(alive))


@app.route("/reset", methods=["POST"])
def reset():
    """Clear the saved results so the next run processes every record again."""
    with JOB_LOCK:
        if JOB["running"]:
            return jsonify(error="Stop the current run before clearing results."), 409
    try:
        m.clear_results()
    except Exception as e:
        return jsonify(error=f"Could not clear the database: {e}"), 500
    with JOB_LOCK:
        JOB.update(
            total=0, processed=0, skipped=0, workers=0, bandwidth_kb=0.0,
            results=[], input_name="",
            message="Results cleared. Upload a file and press Start to run all records.",
        )
    return jsonify(ok=True)


@app.route("/status")
def status():
    with JOB_LOCK:
        return jsonify(
            running=JOB["running"],
            input_name=JOB["input_name"],
            total=JOB["total"],
            processed=JOB["processed"],
            skipped=JOB["skipped"],
            workers=JOB["workers"],
            bandwidth_kb=round(JOB["bandwidth_kb"], 1),
            message=JOB["message"],
            results=JOB["results"],
        )


@app.route("/download")
def download():
    """Dump all stored results from the DB as a downloadable JSON file."""
    rows = m.load_all()
    if not rows:
        return jsonify(error="No results yet - run something first."), 404
    buf = io.BytesIO(json.dumps(rows, indent=2).encode("utf-8"))
    return send_file(buf, as_attachment=True, download_name="progressive_results_v2.json",
                     mimetype="application/json")


@app.route("/login", methods=["GET"])
def login():
    if session.get("user"):
        return redirect("/")
    return Response(LOGIN_PAGE, mimetype="text/html")


@app.route("/login", methods=["POST"])
def do_login():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    if check_login(username, password):
        session["user"] = username
        return jsonify(ok=True)
    return jsonify(error="Wrong username or password."), 401


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify(ok=True)


@app.route("/settings/credentials", methods=["POST"])
@login_required
def update_credentials():
    new_user = (request.form.get("new_user") or "").strip()
    new_pass = request.form.get("new_pass") or ""
    current = request.form.get("current_pass") or ""
    if not check_login(session.get("user"), current):
        return jsonify(error="Current password is wrong."), 403
    if not new_user or len(new_pass) < 4:
        return jsonify(error="Username required and password must be 4+ chars."), 400
    m.set_setting("auth_user", new_user)
    m.set_setting("auth_pass", generate_password_hash(new_pass))
    session["user"] = new_user
    return jsonify(ok=True)


@app.route("/me")
@login_required
def me():
    return jsonify(user=session.get("user"))


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Progressive Automation Dashboard</title>
<style>
  :root{
    --bg:#0b0e14; --bg-elev:#0e121b; --surface:#11151f; --surface-2:#161b28; --surface-3:#1b2130;
    --line:#232a3a; --line-soft:#1a2130;
    --ink:#e9edf6; --ink-dim:#9aa3b8; --ink-faint:#616c85;
    --accent:#6d8bff; --accent-hi:#88a0ff; --accent-press:#5570e6; --accent-weak:rgba(109,139,255,.14);
    --good:#54e08b; --good-bg:#10331f; --good-bd:#1c5334;
    --warn:#f2c14e; --warn-bg:#2c2711; --warn-bd:#524711;
    --bad:#ff8f8f; --bad-bg:#361c21; --bad-bd:#5e2a30;
    --idle:#93a0b8; --idle-bg:#1a2130;
    --radius:12px; --radius-sm:9px;
    --shadow:0 1px 0 rgba(255,255,255,.02), 0 14px 34px -20px rgba(0,0,0,.75);
    --mono:ui-monospace,'SF Mono','Cascadia Code',Consolas,monospace;
    color-scheme:dark;
  }
  *{ box-sizing:border-box; }
  html,body{ height:100%; }
  body{ margin:0; color:var(--ink); font-size:14px; line-height:1.45;
        font-family:-apple-system,'Segoe UI',system-ui,Roboto,Arial,sans-serif;
        background:radial-gradient(1100px 560px at 80% -12%, #141b2b 0%, var(--bg) 58%) fixed;
        -webkit-font-smoothing:antialiased; }
  ::selection{ background:var(--accent-weak); }
  *::-webkit-scrollbar{ width:10px; height:10px; }
  *::-webkit-scrollbar-thumb{ background:#212838; border-radius:8px; border:2px solid transparent; background-clip:content-box; }
  *::-webkit-scrollbar-thumb:hover{ background:#2c3547; background-clip:content-box; }

  /* Header */
  .topbar{ display:flex; align-items:center; justify-content:space-between; gap:16px;
           height:60px; padding:0 22px; position:sticky; top:0; z-index:20;
           background:linear-gradient(180deg,#121826,#0d111a); border-bottom:1px solid var(--line); }
  .brand{ display:flex; align-items:center; gap:12px; }
  .brand .mark{ width:32px; height:32px; border-radius:9px; display:grid; place-items:center;
                background:linear-gradient(145deg,var(--accent),#3d4a86); color:#fff; font-weight:800;
                font-size:16px; box-shadow:0 8px 18px -8px var(--accent); }
  .brand .name{ font-size:15px; font-weight:700; letter-spacing:.01em; }
  .brand .name span{ color:var(--accent-hi); }
  .brand .tag{ font-size:10.5px; color:var(--ink-faint); text-transform:uppercase; letter-spacing:.14em; margin-top:2px; }
  .topbar-actions{ display:flex; align-items:center; gap:10px; }

  /* Layout */
  .app{ display:grid; grid-template-columns:360px 1fr; height:calc(100vh - 60px); }
  .rail{ background:var(--bg-elev); border-right:1px solid var(--line); display:flex; flex-direction:column; overflow-y:auto; }
  .rail-inner{ padding:6px 18px 0; }
  .main{ overflow-y:auto; padding:22px 24px 44px; }

  /* Sidebar groups */
  .group{ padding:16px 0; border-top:1px solid var(--line-soft); }
  .group:first-child{ border-top:0; }
  .group-title{ font-size:11px; font-weight:700; letter-spacing:.12em; text-transform:uppercase;
                color:var(--ink-faint); margin:6px 0 12px; }
  label{ display:block; font-size:11px; font-weight:600; letter-spacing:.02em; color:var(--ink-dim); margin:12px 0 6px; }

  /* Inputs */
  input[type=text],input[type=number],input[type=password],input[type=file],select,textarea{
    width:100%; background:var(--surface); color:var(--ink); border:1px solid var(--line);
    border-radius:var(--radius-sm); padding:10px 11px; font-size:13px; font-family:inherit;
    transition:border-color .15s, box-shadow .15s, background .15s; }
  input::placeholder,textarea::placeholder{ color:#54607a; }
  input:focus,select:focus,textarea:focus{ outline:none; border-color:var(--accent);
    box-shadow:0 0 0 3px var(--accent-weak); background:var(--bg-elev); }
  textarea{ font-family:var(--mono); font-size:12px; line-height:1.55; resize:vertical; min-height:104px; }
  select{ appearance:none; cursor:pointer; padding-right:30px;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' fill='%239aa3b8'%3E%3Cpath d='M6 8L1 3h10z'/%3E%3C/svg%3E");
    background-repeat:no-repeat; background-position:right 11px center; }
  input[type=file]{ padding:8px; font-size:12px; color:var(--ink-dim); cursor:pointer; }
  input[type=file]::file-selector-button{ background:var(--surface-3); color:var(--ink); border:1px solid var(--line);
    border-radius:7px; padding:6px 12px; margin-right:10px; font-size:12px; font-weight:600; cursor:pointer; }
  input[type=file]::file-selector-button:hover{ background:#242c3d; }

  /* Checkbox rows */
  .check{ display:flex; align-items:flex-start; gap:11px; margin:11px 0; cursor:pointer; }
  .check > input[type=checkbox]{ appearance:none; flex:none; width:18px; height:18px; margin-top:1px;
    border:1px solid var(--line); border-radius:6px; background:var(--surface); cursor:pointer; position:relative; transition:.15s; }
  .check > input:checked{ background:var(--accent); border-color:var(--accent); }
  .check > input:checked::after{ content:""; position:absolute; left:5px; top:2px; width:5px; height:9px;
    border:solid #fff; border-width:0 2px 2px 0; transform:rotate(45deg); }
  .check > input:focus-visible{ outline:none; box-shadow:0 0 0 3px var(--accent-weak); }
  .check > span{ font-size:13px; font-weight:500; color:var(--ink); }
  .check .sub{ display:block; font-size:11.5px; color:var(--ink-faint); font-weight:400; margin-top:2px; }

  /* Buttons */
  button{ font-family:inherit; font-weight:600; font-size:13px; cursor:pointer; border:1px solid transparent;
    border-radius:var(--radius-sm); padding:10px 14px; transition:.15s; }
  .btns{ display:flex; gap:9px; margin-top:14px; }
  .btns > button{ flex:1; }
  .btn-primary{ background:var(--accent); color:#fff; border-color:var(--accent); box-shadow:0 10px 20px -12px var(--accent); }
  .btn-primary:hover:not(:disabled){ background:var(--accent-hi); }
  .btn-primary:active{ background:var(--accent-press); }
  .btn-danger{ background:var(--bad-bg); color:var(--bad); border-color:var(--bad-bd); }
  .btn-danger:hover:not(:disabled){ background:#42222a; }
  .ghost{ background:var(--surface-2); color:var(--ink); border-color:var(--line); }
  .ghost:hover:not(:disabled){ background:var(--surface-3); border-color:#2e3750; }
  .dl{ flex:none; padding:8px 13px; font-size:12px; }
  button:disabled{ opacity:.4; cursor:not-allowed; box-shadow:none; }
  button:focus-visible{ outline:none; box-shadow:0 0 0 3px var(--accent-weak); }

  .hint{ font-size:11.5px; color:var(--ink-dim); line-height:1.5; margin-top:8px; min-height:14px; }
  .hint b{ color:var(--ink); font-weight:600; }

  /* Run banner */
  .runbar{ background:var(--surface); border:1px solid var(--line); border-radius:var(--radius);
    padding:14px 16px; margin-bottom:18px; box-shadow:var(--shadow); }
  .runbar-msg{ font-size:13px; color:var(--ink-dim); margin-bottom:10px; min-height:18px; }
  .track{ height:8px; background:#0a0d14; border:1px solid var(--line-soft); border-radius:6px; overflow:hidden; }
  .track > i{ display:block; height:100%; width:0; border-radius:6px;
    background:linear-gradient(90deg,var(--accent-press),var(--accent-hi)); transition:width .5s ease; }

  /* KPI tiles */
  .kpis{ display:grid; grid-template-columns:repeat(5,1fr); gap:12px; margin-bottom:20px; }
  .kpi{ background:linear-gradient(180deg,var(--surface-2),var(--surface)); border:1px solid var(--line);
    border-radius:var(--radius); padding:14px 16px; position:relative; overflow:hidden; box-shadow:var(--shadow); }
  .kpi::before{ content:""; position:absolute; left:0; top:0; bottom:0; width:3px; background:var(--accent); opacity:.7; }
  .kpi b{ display:block; font-size:26px; font-weight:750; font-variant-numeric:tabular-nums; letter-spacing:-.02em; line-height:1.1; }
  .kpi span{ display:block; font-size:11px; color:var(--ink-faint); text-transform:uppercase; letter-spacing:.08em; margin-top:4px; }

  /* Tabs */
  .tabs{ display:flex; gap:2px; border-bottom:1px solid var(--line); margin-bottom:18px; overflow-x:auto; }
  .tab{ padding:11px 15px; cursor:pointer; color:var(--ink-dim); font-weight:600; font-size:13px; white-space:nowrap;
    border-bottom:2px solid transparent; margin-bottom:-1px; transition:color .15s; display:flex; align-items:center; gap:7px; }
  .tab:hover{ color:var(--ink); }
  .tab.active{ color:var(--ink); border-bottom-color:var(--accent); }

  /* Toolbar */
  .toolbar{ display:flex; gap:10px; align-items:center; margin-bottom:14px; flex-wrap:wrap; }
  .toolbar select,.toolbar input[type=text]{ width:auto; min-width:170px; }
  .toolbar .spacer{ flex:1; }
  .note{ font-size:12px; color:var(--ink-faint); }

  /* Tables */
  .tablewrap{ border:1px solid var(--line); border-radius:var(--radius); overflow:hidden; background:var(--surface); box-shadow:var(--shadow); }
  table{ width:100%; border-collapse:collapse; font-size:13px; }
  thead th{ position:sticky; top:0; background:var(--surface-3); color:var(--ink-faint); z-index:1;
    font-size:10.5px; font-weight:700; text-transform:uppercase; letter-spacing:.06em;
    text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); }
  tbody td{ padding:10px 12px; border-bottom:1px solid var(--line-soft); }
  tbody tr:last-child td{ border-bottom:0; }
  tbody tr:nth-child(even){ background:rgba(255,255,255,.014); }
  tbody tr:hover{ background:var(--accent-weak); }
  .num{ text-align:right; font-variant-numeric:tabular-nums; }
  .muted{ color:var(--ink-faint); }

  /* Pills & badges */
  .pill{ display:inline-flex; align-items:center; gap:6px; padding:3px 9px; border-radius:20px;
    font-size:11px; font-weight:700; white-space:nowrap; border:1px solid transparent; }
  .pill::before{ content:""; width:6px; height:6px; border-radius:50%; background:currentColor; }
  .s-vehicles_found{ background:var(--good-bg); color:var(--good); border-color:var(--good-bd); }
  .s-no_vehicles{ background:var(--warn-bg); color:var(--warn); border-color:var(--warn-bd); }
  .s-blocked,.s-error{ background:var(--bad-bg); color:var(--bad); border-color:var(--bad-bd); }
  .s-pending,.s-cancelled{ background:var(--idle-bg); color:var(--idle); border-color:var(--line); }
  .badge{ display:inline-grid; place-items:center; min-width:18px; height:18px; padding:0 5px; border-radius:9px;
    background:var(--bad-bg); color:var(--bad); font-size:10.5px; font-weight:800; border:1px solid var(--bad-bd); }
  .badge:empty{ display:none; }

  /* Settings */
  .settings-bar{ margin-top:auto; position:sticky; bottom:0; padding:14px 18px; background:var(--bg-elev); border-top:1px solid var(--line); }
  #settingsToggle{ width:100%; text-align:left; }
  #settingsPanel{ margin-top:10px; padding:14px; background:var(--surface); border:1px solid var(--line); border-radius:var(--radius); }

  /* Responsive */
  @media (max-width:900px){
    .app{ grid-template-columns:1fr; height:auto; }
    .rail{ border-right:0; border-bottom:1px solid var(--line); }
    .kpis{ grid-template-columns:repeat(2,1fr); }
    .settings-bar{ position:static; }
  }
  @media (prefers-reduced-motion:reduce){ *{ transition:none!important; } }
</style></head>
<body>
<header class="topbar">
  <div class="brand">
    <div class="mark">P</div>
    <div>
      <div class="name">Progressive <span>Automation</span></div>
      <div class="tag">Quote &amp; vehicle console</div>
    </div>
  </div>
  <div class="topbar-actions">
    <button class="ghost dl" id="dlfile">Download results (.json)</button>
  </div>
</header>

<div class="app">
  <aside class="rail">
    <div class="rail-inner">

      <div class="group">
        <div class="group-title">Records</div>
        <label>Upload a file (.json / .csv)</label>
        <input type="file" id="records" accept=".json,.csv,.txt,application/json,text/csv">
        <label>&hellip; or paste rows (CSV / Excel, with a header)</label>
        <textarea id="records_text" placeholder="phone,first_name,last_name,address1,city,state,zip,car_make,car_model
7724611704,Hailin,Swanson,5610 Pinetree Dr,Fort Pierce,FL,34982,KIA,SORENTO"></textarea>
        <div class="hint">Any column order. Only <b>name, address and zip</b> are required &mdash; extra columns are fine.</div>
      </div>

      <div class="group">
        <div class="group-title">Proxies</div>
        <label>One per line &mdash; host:port:user:pass or user:pass@host:port</label>
        <textarea id="proxies" placeholder="user:pass_country-US@thehub.proxy-cheap.com:8080"></textarea>
        <label class="check"><input type="checkbox" id="use_proxies"><span>Use proxies<span class="sub">Route each record through a proxy</span></span></label>
        <div class="btns"><button class="ghost" id="check">Check proxies &mdash; keep only working</button></div>
        <div class="hint" id="checkmsg"></div>
      </div>

      <div class="group">
        <div class="group-title">Options</div>
        <label class="check"><input type="checkbox" id="save_bandwidth" checked><span>Save bandwidth<span class="sub">Block images, fonts &amp; trackers</span></span></label>
        <label class="check"><input type="checkbox" id="headless"><span>Headless<span class="sub">Hide the Chrome windows</span></span></label>
        <label>Parallel Chrome windows</label>
        <input type="number" id="concurrency" value="10" min="1" max="30">
      </div>

      <div class="group">
        <div class="group-title">Run</div>
        <div class="btns">
          <button class="btn-primary" id="start">Start run</button>
          <button class="btn-danger" id="stop" disabled>Stop</button>
        </div>
        <div class="btns"><button class="ghost" id="clear">Clear results &mdash; re-run all</button></div>
      </div>

    </div>

    <div class="settings-bar">
      <button class="ghost" id="settingsToggle">&#9881;&nbsp; Settings</button>
      <div id="settingsPanel" style="display:none">
        <label>Username</label>
        <input type="text" id="set_user" placeholder="new username">
        <label>New password (4+ chars)</label>
        <input type="password" id="set_pass" placeholder="new password">
        <label>Current password</label>
        <input type="password" id="set_current" placeholder="confirm with current password">
        <div class="btns"><button class="ghost" id="saveCreds">Save login</button></div>
        <div class="hint" id="setMsg"></div>
        <div class="btns"><button class="btn-danger" id="logoutBtn">Log out</button></div>
      </div>
    </div>
  </aside>

  <main class="main">
    <div class="runbar">
      <div class="runbar-msg" id="msg">Idle &mdash; load records and start a run.</div>
      <div class="track"><i id="prog"></i></div>
    </div>

    <div class="kpis">
      <div class="kpi"><b id="st-total">0</b><span>Total</span></div>
      <div class="kpi"><b id="st-done">0</b><span>Processed</span></div>
      <div class="kpi"><b id="st-skip">0</b><span>Skipped</span></div>
      <div class="kpi"><b id="st-work">0</b><span>Workers</span></div>
      <div class="kpi"><b id="st-bw">0</b><span>MB used</span></div>
    </div>

    <div class="tabs">
      <div class="tab active" data-tab="records">Records</div>
      <div class="tab" data-tab="errors">Errors <span id="errBadge" class="badge"></span></div>
      <div class="tab" data-tab="status">By status</div>
      <div class="tab" data-tab="vehicles">By vehicle count</div>
      <div class="tab" data-tab="year">By year</div>
    </div>

    <div id="tab-records">
      <div class="toolbar">
        <select id="recFilter">
          <option value="all">All statuses</option>
          <option value="vehicles_found">vehicles_found</option>
          <option value="no_vehicles">no_vehicles</option>
          <option value="blocked">blocked</option>
          <option value="error">error</option>
          <option value="pending">pending</option>
        </select>
        <input type="text" id="recSearch" placeholder="Search name, phone or ZIP">
        <span class="spacer"></span>
        <button class="ghost dl" id="dlRecCsv">Download CSV</button>
        <button class="ghost dl" id="dlRecJson">Download JSON</button>
      </div>
      <div class="tablewrap"><table><thead><tr>
        <th>#</th><th>Name</th><th>Phone</th><th>ZIP</th><th>Status</th><th>Vehicles</th>
      </tr></thead><tbody id="recRows"></tbody></table></div>
    </div>

    <div id="tab-errors" style="display:none">
      <div class="toolbar">
        <span class="note">Records that errored or were blocked. Fix settings if needed, then retry just these.</span>
        <span class="spacer"></span>
        <button class="ghost dl" id="retryBtn">Retry failed</button>
        <button class="ghost dl" id="dlErrCsv">Download CSV</button>
      </div>
      <div class="tablewrap"><table><thead><tr>
        <th>#</th><th>Name</th><th>ZIP</th><th>Status</th><th>Reason</th>
      </tr></thead><tbody id="errRows"></tbody></table></div>
    </div>

    <div id="tab-status" style="display:none">
      <div class="toolbar"><span class="spacer"></span>
        <button class="ghost dl" id="dlStatusCsv">Download CSV</button></div>
      <div class="tablewrap"><table><thead><tr><th>Status</th><th class="num">Count</th><th class="num">% of total</th><th></th></tr></thead>
        <tbody id="statusRows"></tbody></table></div>
    </div>

    <div id="tab-vehicles" style="display:none">
      <div class="toolbar"><span class="note">How many records have 0, 1, 2&hellip; vehicles on file.</span>
        <span class="spacer"></span><button class="ghost dl" id="dlVehCsv">Download CSV</button></div>
      <div class="tablewrap"><table><thead><tr><th># Vehicles</th><th class="num">Records</th><th></th></tr></thead>
        <tbody id="vehRows"></tbody></table></div>
    </div>

    <div id="tab-year" style="display:none">
      <div class="toolbar"><span class="note">Vehicles grouped by model year across all records.</span>
        <span class="spacer"></span><button class="ghost dl" id="dlYearCsv">Download CSV</button></div>
      <div class="tablewrap"><table><thead><tr><th>Year</th><th class="num">Vehicles</th><th class="num">Records</th><th></th></tr></thead>
        <tbody id="yearRows"></tbody></table></div>
    </div>
  </main>
</div>

<script>
const $ = id => document.getElementById(id);
let DATA = [];         // latest results from /status
let TAB = "records";

/* ---------- helpers ---------- */
function esc(s){ return String(s==null?"":s).replace(/[<>&]/g,c=>({"<":"&lt;",">":"&gt;","&":"&amp;"}[c])); }
function pill(s){ return `<span class="pill s-${s}">${s}</span>`; }
function vehList(r){ return (r.address_recognized_vehicles||[]).map(x=>`${x.year} ${x.make} ${x.model}`); }
function vehStr(r){ return vehList(r).join(", "); }

function csvEscape(v){ v=String(v==null?"":v); return /[",\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v; }
function toCsv(rows){ return rows.map(r=>r.map(csvEscape).join(",")).join("\n"); }
function download(name, text, mime){
  const b=new Blob([text],{type:mime}); const u=URL.createObjectURL(b);
  const a=document.createElement("a"); a.href=u; a.download=name; a.click();
  setTimeout(()=>URL.revokeObjectURL(u),1000);
}

/* ---------- controls ---------- */
async function start(){
  const file=$("records").files[0];
  const pasted=$("records_text").value.trim();
  if(!file && !pasted){ alert("Upload a file or paste records first."); return; }
  const fd=new FormData();
  if(file) fd.append("records",file);
  fd.append("records_text",pasted);
  fd.append("proxies",$("proxies").value);
  fd.append("use_proxies",$("use_proxies").checked);
  fd.append("save_bandwidth",$("save_bandwidth").checked);
  fd.append("headless",$("headless").checked);
  fd.append("concurrency",$("concurrency").value);
  const r=await fetch("/start",{method:"POST",body:fd});
  if(!r.ok){ const e=await r.json(); alert(e.error||"Start failed"); }
}
async function stop(){ await fetch("/stop",{method:"POST"}); }
async function checkProxies(){
  const btn=$("check"); btn.disabled=true; $("checkmsg").textContent="Testing proxies... (up to ~10s)";
  try{
    const fd=new FormData(); fd.append("proxies",$("proxies").value);
    const r=await fetch("/check",{method:"POST",body:fd}); const j=await r.json();
    if(!r.ok){ $("checkmsg").textContent=j.error||"Check failed"; return; }
    $("proxies").value=j.working;
    $("checkmsg").textContent=`${j.alive} of ${j.total} working - kept only those.`;
    if(j.alive===0) $("checkmsg").textContent+=" Free list is dead; use residential.";
  } finally { btn.disabled=false; }
}
async function clearResults(){
  if(!confirm("Delete all saved results? Every record will run again next Start.")) return;
  const r=await fetch("/reset",{method:"POST"});
  if(!r.ok){ const e=await r.json(); alert(e.error||"Clear failed"); return; }
  poll();
}

/* ---------- tab rendering ---------- */
function setTab(name){
  TAB=name;
  document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("active",t.dataset.tab===name));
  ["records","errors","status","vehicles","year"].forEach(t=>$("tab-"+t).style.display=(t===name?"":"none"));
  render();
}

function failedRecords(){ return DATA.filter(r=>["error","blocked","cancelled"].includes(r.status)); }
function reason(r){
  if(r.error) return r.error;
  if(r.blocked_errors && r.blocked_errors.length) return r.blocked_errors.join("; ");
  return r.status;
}
function renderErrors(){
  const rows=failedRecords();
  $("errRows").innerHTML = rows.map((r,i)=>`<tr>
    <td class="muted">${i+1}</td>
    <td>${esc(r.first_name)} ${esc(r.last_name)}</td>
    <td>${esc(r.zip||"")}</td>
    <td>${pill(r.status)}</td>
    <td class="muted">${esc(reason(r))}</td></tr>`).join("")
    || `<tr><td colspan="5" class="muted">No errors. </td></tr>`;
}

function filteredRecords(){
  const f=$("recFilter").value, q=$("recSearch").value.toLowerCase().trim();
  return DATA.filter(r=>{
    if(f!=="all" && r.status!==f) return false;
    if(q){
      const hay=`${r.first_name} ${r.last_name} ${r.phone||""} ${r.zip||""}`.toLowerCase();
      if(!hay.includes(q)) return false;
    }
    return true;
  });
}

const TABLE_CAP = 1000;  // only render this many rows; keeps the browser snappy
function renderRecords(){
  const rows=filteredRecords();
  const shown=rows.slice(0, TABLE_CAP);
  let html = shown.map((r,i)=>`<tr>
    <td class="muted">${i+1}</td>
    <td>${esc(r.first_name)} ${esc(r.last_name)}</td>
    <td>${esc(r.phone||"")}</td>
    <td>${esc(r.zip||"")}</td>
    <td>${pill(r.status)}</td>
    <td>${esc(vehStr(r))}</td></tr>`).join("");
  if(rows.length>TABLE_CAP)
    html += `<tr><td colspan="6" class="muted">Showing first ${TABLE_CAP} of ${rows.length}. Use Download CSV for all.</td></tr>`;
  $("recRows").innerHTML = html || `<tr><td colspan="6" class="muted">No records match.</td></tr>`;
}

function statusCounts(){
  const c={}; DATA.forEach(r=>c[r.status]=(c[r.status]||0)+1); return c;
}
function renderStatus(){
  const c=statusCounts(), total=DATA.length||1;
  const order=["vehicles_found","no_vehicles","blocked","error","pending","cancelled"];
  const keys=[...new Set([...order.filter(k=>c[k]),...Object.keys(c)])];
  $("statusRows").innerHTML=keys.map(k=>`<tr>
    <td>${pill(k)}</td><td class="num">${c[k]}</td>
    <td class="num">${(c[k]/total*100).toFixed(1)}%</td>
    <td class="num"><button class="ghost dl" onclick="dlStatus('${k}')">CSV</button></td></tr>`).join("")
    || `<tr><td colspan="4" class="muted">No data yet.</td></tr>`;
}

function vehCountDist(){
  const d={}; DATA.forEach(r=>{ const n=(r.address_recognized_vehicles||[]).length; d[n]=(d[n]||0)+1; });
  return d;
}
function renderVehicles(){
  const d=vehCountDist();
  const keys=Object.keys(d).map(Number).sort((a,b)=>a-b);
  $("vehRows").innerHTML=keys.map(n=>`<tr>
    <td>${n} vehicle${n===1?"":"s"}</td><td class="num">${d[n]}</td>
    <td class="num"><button class="ghost dl" onclick="dlVehGroup(${n})">CSV</button></td></tr>`).join("")
    || `<tr><td colspan="3" class="muted">No data yet.</td></tr>`;
}

function yearDist(){
  const veh={}, rec={};
  DATA.forEach(r=>{
    const years=new Set();
    (r.address_recognized_vehicles||[]).forEach(v=>{
      veh[v.year]=(veh[v.year]||0)+1; years.add(v.year);
    });
    years.forEach(y=>rec[y]=(rec[y]||0)+1);
  });
  return {veh,rec};
}
function renderYear(){
  const {veh,rec}=yearDist();
  const keys=Object.keys(veh).sort((a,b)=>b.localeCompare(a));
  $("yearRows").innerHTML=keys.map(y=>`<tr>
    <td>${esc(y)}</td><td class="num">${veh[y]}</td><td class="num">${rec[y]}</td>
    <td class="num"><button class="ghost dl" onclick="dlYear('${y}')">CSV</button></td></tr>`).join("")
    || `<tr><td colspan="4" class="muted">No vehicles found yet.</td></tr>`;
}

function render(){
  const nErr=failedRecords().length;
  $("errBadge").textContent = nErr ? nErr : "";
  if(TAB==="records") renderRecords();
  else if(TAB==="errors") renderErrors();
  else if(TAB==="status") renderStatus();
  else if(TAB==="vehicles") renderVehicles();
  else if(TAB==="year") renderYear();
}

async function retryFailed(){
  const fd=new FormData();
  fd.append("proxies",$("proxies").value);
  fd.append("use_proxies",$("use_proxies").checked);
  fd.append("save_bandwidth",$("save_bandwidth").checked);
  fd.append("headless",$("headless").checked);
  fd.append("concurrency",$("concurrency").value);
  const r=await fetch("/retry",{method:"POST",body:fd});
  const j=await r.json();
  if(!r.ok){ alert(j.error||"Retry failed"); return; }
}
function dlErrCsv(){ download("errors.csv", recordsCsv(failedRecords()), "text/csv"); }

async function saveCreds(){
  const fd=new FormData();
  fd.append("new_user",$("set_user").value.trim());
  fd.append("new_pass",$("set_pass").value);
  fd.append("current_pass",$("set_current").value);
  const r=await fetch("/settings/credentials",{method:"POST",body:fd});
  const j=await r.json();
  $("setMsg").textContent = r.ok ? "Login updated." : (j.error||"Failed");
  if(r.ok){ $("set_pass").value=""; $("set_current").value=""; }
}
async function logout(){ await fetch("/logout",{method:"POST"}); location.href="/login"; }

/* ---------- downloads ---------- */
// Exact export layout requested: one row per vehicle. A record with 3 vehicles
// becomes 3 rows sharing phone/name/address, differing only in the car_* cols.
// A record with no vehicles still gets one row (blank car_* cols) so it is not lost.
const REC_COLS=["phone","first_name","middle_name","last_name","address1","city","state","zip",
                "car_make","car_model","car_year","email"];
function recToRows(r){
  const a=r.address_used||{};
  let address1=a.street||"";
  if(a.unit) address1=(address1+" "+a.unit).trim();
  const base=[r.phone||"", r.first_name||"", r.middle_initial||"", r.last_name||"",
              address1, a.city||"", a.state||"", a.zip||r.zip||""];
  const veh=r.address_recognized_vehicles||[];
  if(veh.length) return veh.map(v=>[...base, v.make||"", v.model||"", v.year||"", r.email||""]);
  return [[...base, "", "", "", r.email||""]];
}
function recordsCsv(list){ return toCsv([REC_COLS, ...list.flatMap(recToRows)]); }

function dlRecCsv(){ download("records.csv", recordsCsv(filteredRecords()), "text/csv"); }
function dlRecJson(){ download("records.json", JSON.stringify(filteredRecords(),null,2), "application/json"); }
function dlStatus(k){ download(`status_${k}.csv`, recordsCsv(DATA.filter(r=>r.status===k)), "text/csv"); }
function dlVehGroup(n){ download(`vehicles_${n}.csv`, recordsCsv(DATA.filter(r=>(r.address_recognized_vehicles||[]).length===n)), "text/csv"); }
function dlYear(y){
  const list=DATA.filter(r=>(r.address_recognized_vehicles||[]).some(v=>v.year===y));
  download(`year_${y}.csv`, recordsCsv(list), "text/csv");
}
function dlStatusCsv(){
  const c=statusCounts(); download("by_status.csv", toCsv([["status","count"],...Object.entries(c)]), "text/csv");
}
function dlVehCsv(){
  const d=vehCountDist();
  download("by_vehicle_count.csv", toCsv([["num_vehicles","records"],...Object.entries(d)]), "text/csv");
}
function dlYearCsv(){
  const {veh,rec}=yearDist();
  const keys=Object.keys(veh).sort((a,b)=>b.localeCompare(a));
  download("by_year.csv", toCsv([["year","vehicles","records"],...keys.map(y=>[y,veh[y],rec[y]])]), "text/csv");
}

/* ---------- polling ---------- */
async function poll(){
  try{
    const s=await (await fetch("/status")).json();
    $("st-total").textContent=s.total;
    $("st-done").textContent=s.processed;
    $("st-skip").textContent=s.skipped;
    $("st-work").textContent=s.workers;
    $("st-bw").textContent=(s.bandwidth_kb/1024).toFixed(1);
    $("msg").textContent=s.message;
    $("prog").style.width=(s.total? (s.processed/s.total*100):0)+"%";
    $("start").disabled=s.running;
    $("stop").disabled=!s.running;
    $("clear").disabled=s.running;
    $("retryBtn").disabled=s.running;
    DATA=s.results||[];
    render();
  } catch(e){ /* not ready */ }
}

/* ---------- wire up ---------- */
$("start").onclick=start;
$("stop").onclick=stop;
$("clear").onclick=clearResults;
$("check").onclick=checkProxies;
$("dlfile").onclick=()=>{ window.location="/download"; };
$("dlRecCsv").onclick=dlRecCsv;
$("dlRecJson").onclick=dlRecJson;
$("dlStatusCsv").onclick=dlStatusCsv;
$("dlVehCsv").onclick=dlVehCsv;
$("dlYearCsv").onclick=dlYearCsv;
$("retryBtn").onclick=retryFailed;
$("dlErrCsv").onclick=dlErrCsv;
$("settingsToggle").onclick=()=>{ const p=$("settingsPanel"); p.style.display=p.style.display==="none"?"":"none"; };
$("saveCreds").onclick=saveCreds;
$("logoutBtn").onclick=logout;
$("set_user").addEventListener("focus",async()=>{ if(!$("set_user").value){ try{ const j=await(await fetch("/me")).json(); $("set_user").value=j.user||""; }catch(e){} } });
$("recFilter").onchange=renderRecords;
$("recSearch").oninput=renderRecords;
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>setTab(t.dataset.tab));
setInterval(poll,1500);
poll();
</script>
</body></html>"""


LOGIN_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Sign in</title>
<style>
  :root{ --accent:#6d8bff; --accent-hi:#88a0ff; --line:#232a3a; color-scheme:dark; }
  *{ box-sizing:border-box; }
  body{ font-family:-apple-system,'Segoe UI',system-ui,Arial,sans-serif; margin:0; min-height:100vh;
        display:flex; align-items:center; justify-content:center; color:#e9edf6;
        background:radial-gradient(900px 500px at 50% -10%, #182135 0%, #0b0e14 60%) fixed; }
  .card{ background:#11151f; border:1px solid var(--line); border-radius:16px; padding:34px 32px; width:340px;
         box-shadow:0 30px 70px -30px rgba(0,0,0,.8); }
  .brand{ display:flex; align-items:center; gap:12px; margin-bottom:22px; }
  .brand .mark{ width:38px; height:38px; border-radius:10px; display:grid; place-items:center;
    background:linear-gradient(145deg,var(--accent),#3d4a86); color:#fff; font-weight:800; font-size:18px;
    box-shadow:0 10px 22px -8px var(--accent); }
  .brand .name{ font-size:16px; font-weight:700; } .brand .name span{ color:var(--accent-hi); }
  .brand .tag{ font-size:10.5px; color:#616c85; text-transform:uppercase; letter-spacing:.14em; margin-top:2px; }
  label{ display:block; font-size:11px; font-weight:600; letter-spacing:.02em; color:#9aa3b8; margin:16px 0 6px; }
  input{ width:100%; background:#0e121b; color:#e9edf6; border:1px solid var(--line); border-radius:9px;
         padding:11px; font-size:14px; transition:border-color .15s, box-shadow .15s; }
  input:focus{ outline:none; border-color:var(--accent); box-shadow:0 0 0 3px rgba(109,139,255,.16); }
  button{ width:100%; margin-top:22px; padding:12px; border:0; border-radius:9px; background:var(--accent); color:#fff;
          font-weight:700; font-size:14px; cursor:pointer; transition:.15s; }
  button:hover{ background:var(--accent-hi); }
  .err{ color:#ff8f8f; font-size:13px; min-height:18px; margin-top:14px; }
</style></head>
<body>
  <form class="card" id="f">
    <div class="brand">
      <div class="mark">P</div>
      <div>
        <div class="name">Progressive <span>Automation</span></div>
        <div class="tag">Sign in to continue</div>
      </div>
    </div>
    <label>Username</label>
    <input id="u" autocomplete="username" autofocus>
    <label>Password</label>
    <input id="p" type="password" autocomplete="current-password">
    <button type="submit">Sign in</button>
    <div class="err" id="e"></div>
  </form>
<script>
  document.getElementById("f").addEventListener("submit", async(ev)=>{
    ev.preventDefault();
    const fd=new FormData();
    fd.append("username",document.getElementById("u").value.trim());
    fd.append("password",document.getElementById("p").value);
    const r=await fetch("/login",{method:"POST",body:fd});
    if(r.ok){ location.href="/"; }
    else{ const j=await r.json(); document.getElementById("e").textContent=j.error||"Sign in failed"; }
  });
</script>
</body></html>"""


# Seed credentials + session secret on import so the guard works immediately.
_init_auth()


if __name__ == "__main__":
    print(f"Dashboard on {HOST}:{PORT}  (default login: admin / admin)")
    app.run(host=HOST, port=PORT, threaded=True)
