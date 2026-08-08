"""Progressive dynamic automation - v2.

What v2 fixes, and why
----------------------
In v1, records whose mailing-address autocomplete did not fire produced
"No 'We found these vehicles at your address' popup appeared" and moved on.
Inspecting the live AddressEdit page showed those records were never getting
past the address page at all.

The page does NOT have one free-text address box. It has separate fields:

    AddressEdit_embedded_questions_list_MailingAddress   "Street number and name" (maxlength 45)
    AddressEdit_embedded_questions_list_ApartmentUnit    "Apt./Unit #"
    AddressEdit_embedded_questions_list_City             "City"      * required, starts EMPTY
    AddressEdit_embedded_questions_list_State            "State"     prefilled from the ZIP, disabled
    AddressEdit_embedded_questions_list_ZipCode          "ZIP Code"  prefilled from the ZIP

State and ZIP arrive prefilled from the homepage ZIP, but City is required and
starts empty. The street autocomplete's real job is to fill City for you. When
it does not fire, City stays blank and "Ok, start my quote" is rejected with
"Required to continue - City *" - the page never advances, so of course no
vehicle popup ever appears. Verified live on the Rios and Condrey records.

So v2:
  1. Resolves City from the ZIP with the free Zippopotam.us API and types it
     into the dedicated City field whenever the autocomplete left it empty.
  2. Types City with real keystrokes. City is a controlled input - setting its
     value programmatically is silently reverted to "" (same class of problem
     as the masked date-of-birth field).
  3. Keeps the street field street-only. It is "Street number and name", so
     appending ", City, ST" to it is wrong and breaks the match.
  4. Splits any apartment/unit part of the address into the Apt./Unit # field.
  5. Verifies the page actually advanced after submitting, so a blocked record
     is reported as blocked instead of being mistaken for "no vehicles found".
"""

import json
import re
import time
import sys
import os
import random
import string
import datetime
import sqlite3
import threading
import urllib.request
import urllib.error
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

# ===========================================================================
# PROXIES - paste your Webshare list here, one per line, as host:port:user:pass
# (that is exactly the format Webshare's "Copy" / export gives you).
# To change proxies later: replace the lines below and re-run. Leave the block
# empty to run with no proxy.
# Each record uses the next proxy in the list (round-robin).
# Set USE_PROXIES = False to run with no proxy (keeps the list below for later).
# ===========================================================================
USE_PROXIES = True

# Proxy-Cheap residential (base US line). With PASSWORD_SESSIONS on, ONE line is
# enough - each record auto-gets a unique _session so it holds its own US IP.
PROXIES = """
Kmx4HxTRKh8f4Yb:IYLvQlCpYbUoK5T_country-US@thehub.proxy-cheap.com:8080
"""


# Auto sticky-per-record. Proxy-Cheap's 'thehub' gateway holds one IP when the
# password carries "_session-<number>_ttl-<minutes>" (verified live from the
# dashboard generator - note it is _ttl-, NOT _lifetime-, and the session is
# numeric; that's why the earlier guess 500'd). We append a UNIQUE session per
# record, so each applicant's whole quote runs on ONE held US IP and the next
# record gets a fresh one. Set False to fall back to plain rotating.
PASSWORD_SESSIONS = True
STICKY_TTL_MIN = 2   # minutes one IP is held. Keep SHORT: a quote takes <1 min,
                     # and each record makes a new session - a long TTL keeps old
                     # sessions reserving proxy connection slots and can pin your
                     # Proxy-Cheap concurrency at 100/100, which then errors records.


def parse_proxy_line(line):
    """Parse ONE proxy line into a Playwright proxy dict, or None if unusable.

    Accepts both shapes we deal with:
      host:port                       (open, no auth)
      host:port:user:pass             (Webshare / Decodo copy format)
      user:pass@host:port             (Proxy-Cheap 'thehub' copy format)
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "@" in line:
        cred, _, hostport = line.partition("@")
        user, _, pwd = cred.partition(":")
        host, _, port = hostport.partition(":")
        if not host or not port:
            return None
        return {"server": f"http://{host}:{port}", "username": user, "password": pwd}
    parts = line.split(":")
    if len(parts) < 2:
        return None
    host, port = parts[0], parts[1]
    proxy = {"server": f"http://{host}:{port}"}
    if len(parts) >= 4:
        proxy["username"] = parts[2]
        proxy["password"] = parts[3]
    return proxy


def parse_proxies(raw):
    """Turn the pasted PROXIES block into Playwright proxy dicts."""
    proxies = []
    for line in raw.strip().splitlines():
        proxy = parse_proxy_line(line)
        if proxy is None:
            if line.strip() and not line.strip().startswith("#"):
                print(f"  Skipping bad proxy line: {line.strip()!r}")
            continue
        proxies.append(proxy)
    return proxies


def with_session(proxy):
    """Pin a fresh sticky IP for this one call, for password-parameter gateways.

    Proxy-Cheap's 'thehub' keeps targeting in the password (e.g. '..._country-US').
    A unique '_session-XXXXXXXX' makes every request in this record exit the SAME
    residential IP, while a different session next record gives a different IP -
    exactly what Progressive needs (one clean IP per applicant, no mid-quote hop).
    For any other provider the proxy is returned unchanged.
    """
    if not proxy or not PASSWORD_SESSIONS:
        return proxy
    server = proxy.get("server", "")
    pwd = proxy.get("password", "")
    is_proxycheap = "proxy-cheap.com" in server or "_country-" in pwd
    if not is_proxycheap or "_session-" in pwd:
        return proxy
    sid = "".join(random.choices(string.digits, k=8))  # numeric session id
    out = dict(proxy)
    out["password"] = f"{pwd}_session-{sid}_ttl-{STICKY_TTL_MIN}"
    return out


# ===========================================================================
# BANDWIDTH SAVING
# Progressive pulls a lot of weight the quote form never needs: images, web
# fonts, video/media, and third-party marketing/analytics tags. On a metered
# proxy every one of those bytes costs you money. We abort them before they
# download. Set SAVE_BANDWIDTH = False to load the page normally.
# ===========================================================================
SAVE_BANDWIDTH = True

# Resource types that are pure weight for a form-fill flow. Aborted on request.
# NOTE: xhr/fetch/script/document are NOT here - the address autocomplete and
# vehicle lookup ride on those, so blocking them would break the flow.
BLOCK_RESOURCE_TYPES = {"image", "media", "font"}

# Third-party marketing / analytics / tag hosts Progressive loads. Not needed
# for the quote; each is extra requests and bytes billed to your proxy. Matched
# as a substring of the URL, so progressive.com's own scripts always pass.
BLOCK_HOST_SUBSTRINGS = (
    "monetate", "doubleclick", "google-analytics", "googletagmanager",
    "googlesyndication", "adservice", "adobedtm", "demdex", "omtrdc",
    "facebook.net", "facebook.com", "bing.com", "clarity.ms", "tealium",
    "quantummetric", "scorecardresearch", "krxd", "hotjar", "newrelic",
    "nr-data", "adsrvr", "branch.io", "fullstory",
)


def _should_block(request):
    # Never block the main-frame navigation/document. Progressive's redirect
    # URLs carry params like "monetateid=..."; matching those and aborting the
    # navigation lands you on chrome-error with no page at all.
    if request.is_navigation_request() or request.resource_type == "document":
        return False
    if request.resource_type in BLOCK_RESOURCE_TYPES:
        return True
    # Match against the HOST only, not the full URL, so a tracker name that
    # appears in a query string of a needed request is not mistaken for a host.
    host = (urlparse(request.url).hostname or "").lower()
    return any(h in host for h in BLOCK_HOST_SUBSTRINGS)


def install_bandwidth_controls(context, stats):
    """Block heavy/unneeded requests and tally received bytes by type.

    `stats` is filled in as the page loads so the caller can print a per-record
    breakdown - your own 'network tab' without opening one.
    """
    def on_route(route):
        if SAVE_BANDWIDTH and _should_block(route.request):
            stats["blocked"] += 1
            route.abort()
        else:
            route.continue_()

    def on_response(resp):
        try:
            cl = resp.headers.get("content-length")
            n = int(cl) if cl else 0
        except Exception:
            n = 0
        rt = resp.request.resource_type
        stats["by_type"][rt] = stats["by_type"].get(rt, 0) + n
        stats["total"] += n

    context.route("**/*", on_route)
    context.on("response", on_response)


def print_bandwidth_report(stats):
    """Show received KB per resource type and how many requests were blocked."""
    kb = stats["total"] / 1024
    print(f"  Bandwidth this record: ~{kb:.0f} KB received, {stats['blocked']} requests blocked")
    top = sorted(stats["by_type"].items(), key=lambda kv: -kv[1])[:5]
    for rt, b in top:
        if b:
            print(f"    {rt:>10}: {b/1024:.0f} KB")


ZIPPOPOTAM_URL = "https://api.zippopotam.us/us/{zip5}"

# Field ids on the AddressEdit page.
F_STREET = 'input[id="AddressEdit_embedded_questions_list_MailingAddress"]'
F_UNIT = 'input[id="AddressEdit_embedded_questions_list_ApartmentUnit"]'
F_CITY = 'input[id="AddressEdit_embedded_questions_list_City"]'
F_STATE = 'select[id="AddressEdit_embedded_questions_list_State"]'
F_ZIP = 'input[id="AddressEdit_embedded_questions_list_ZipCode"]'

# Trailing secondary-address designators, e.g. "... Apt 1043", "... Ste 200".
UNIT_RE = re.compile(
    r'\s+((?:apt|apartment|unit|ste|suite|bldg|building|fl|floor|rm|room|trlr|lot|#)\.?\s*[\w\-]+)\s*$',
    re.IGNORECASE,
)

# ZIP -> (city, state_abbr) so the same ZIP is only fetched once per run.
_zip_cache = {}


def normalize_zip5(zip_code):
    """Zippopotam.us only accepts the 5-digit form, so '70438-5891' -> '70438'."""
    digits = re.sub(r'\D', '', zip_code or '')
    return digits[:5] if len(digits) >= 5 else ''


def lookup_city_state(zip_code):
    """Return (city, state_abbr) for a ZIP via Zippopotam.us, or (None, None).

    Free and key-less. Any failure (bad ZIP, network, unexpected payload) is
    non-fatal - the caller just proceeds without the city, exactly like v1.
    """
    zip5 = normalize_zip5(zip_code)
    if not zip5:
        return None, None
    if zip5 in _zip_cache:
        return _zip_cache[zip5]

    city = state = None
    try:
        req = urllib.request.Request(
            ZIPPOPOTAM_URL.format(zip5=zip5),
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.load(resp)
        places = payload.get("places") or []
        if places:
            city = places[0].get("place name")
            state = places[0].get("state abbreviation")
    except urllib.error.HTTPError as e:
        # 404 just means the ZIP is not in their dataset.
        print(f"  ZIP lookup failed for {zip5}: HTTP {e.code}")
    except Exception as e:
        print(f"  ZIP lookup failed for {zip5}: {type(e).__name__} {e}")

    _zip_cache[zip5] = (city, state)
    return city, state


def split_unit(address):
    """'2601 Bill Owens PKWY Apt 1043' -> ('2601 Bill Owens PKWY', 'Apt 1043')."""
    m = UNIT_RE.search(address or '')
    if m:
        return address[:m.start()].strip(), m.group(1).strip()
    return (address or '').strip(), ''


def type_into(page, selector, text):
    """Replace a field's contents using real keystrokes.

    These are controlled inputs: page.fill sets .value without firing the key
    events the page listens for, and the page reverts the value to empty.
    """
    field = page.locator(selector)
    field.click()
    # Only clear when there is something to clear. Pressing Control+A/Delete on an
    # already-empty street box appears to leave the autocomplete widget in a state
    # where it never issues its lookup, which costs us the city it would have
    # filled in for free.
    if field.input_value():
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
    field.type(text, delay=60)


# --- Date of birth normalization --------------------------------------------
# Progressive's masked DOB field wants MMDDYYYY digits, so every DOB is coerced
# to MM/DD/YYYY. Rules:
#   * empty / no column  -> a synthetic DOB for someone 29+ years old,
#   * a bare age like "55" -> today's year minus 55,
#   * a real date in almost any format/order -> reformatted.
# Synthetic and age-derived DOBs are seeded by the record's identity so the
# SAME record gets the SAME DOB every run - otherwise resume matching (which
# keys partly on the record) would treat it as a new person each time.
_DOB_FORMATS = [
    "%m/%d/%Y", "%m-%d-%Y", "%m.%d.%Y", "%Y-%m-%d", "%Y/%m/%d",
    "%m/%d/%y", "%m-%d-%y", "%b %d %Y", "%b %d, %Y", "%B %d %Y",
    "%B %d, %Y", "%d %b %Y", "%d-%b-%Y", "%Y%m%d", "%m%d%Y",
    "%d/%m/%Y", "%d-%m-%Y",  # day-first last, so US month-first wins ties
]


def normalize_email(email, first_name, last_name):
    """Use the provided email, or build firstnamelastname@gmail.com when none
    is given so the quote form always has a valid-looking address to submit."""
    e = (email or "").strip()
    if e:
        return e
    fn = re.sub(r"[^a-z0-9]", "", (first_name or "").lower())
    ln = re.sub(r"[^a-z0-9]", "", (last_name or "").lower())
    base = (fn + ln) or "user"
    return f"{base}@gmail.com"


def _synthetic_dob(seed_key, min_age=29, max_age=70):
    rng = random.Random(seed_key or "seed")
    year = datetime.date.today().year - rng.randint(min_age, max_age)
    return f"{rng.randint(1, 12):02d}/{rng.randint(1, 28):02d}/{year:04d}"


def normalize_dob(dob, seed_key=""):
    """Return a MM/DD/YYYY DOB from whatever was (or wasn't) provided."""
    s = (dob or "").strip()
    if not s:
        return _synthetic_dob(seed_key)

    # A bare number is an age (e.g. "55"), not a date.
    if s.isdigit() and len(s) <= 3:
        age = int(s)
        if 16 <= age <= 100:
            rng = random.Random(seed_key or "seed")
            year = datetime.date.today().year - age
            return f"{rng.randint(1, 12):02d}/{rng.randint(1, 28):02d}/{year:04d}"

    this_year = datetime.date.today().year
    for fmt in _DOB_FORMATS:
        try:
            d = datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        if 1900 <= d.year <= this_year:
            return d.strftime("%m/%d/%Y")

    # Last resort: 8 bare digits could be MMDDYYYY or YYYYMMDD.
    digits = re.sub(r"\D", "", s)
    if len(digits) == 8:
        for fmt in ("%m%d%Y", "%Y%m%d"):
            try:
                d = datetime.datetime.strptime(digits, fmt).date()
                if 1900 <= d.year <= this_year:
                    return d.strftime("%m/%d/%Y")
            except ValueError:
                pass

    # Unparseable - fall back to a valid synthetic DOB so the record still runs.
    return _synthetic_dob(seed_key)


def fill_masked_date(page, selector, dob):
    """Progressive's DOB field is a masked/controlled input: setting .value
    directly (what page.fill does) only fills the year. It needs real keystrokes."""
    digits = re.sub(r'\D', '', dob)
    field = page.locator(selector)
    field.click()
    page.keyboard.press("Control+A")
    field.type(digits, delay=50)


def fill_address(page, address, zip_code, result):
    """Fill street / unit / city, using the ZIP lookup to supply the city."""
    page.locator(F_STREET).wait_for(timeout=60000)

    street, unit = split_unit(address)
    print(f"Filling Address: {street}" + (f"  |  Unit: {unit}" if unit else ""))

    # Street. Typing it may pop the autocomplete, which fills City for us.
    type_into(page, F_STREET, street)
    autocompleted = False
    try:
        page.wait_for_selector('[role="option"]', timeout=5000)
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")
        autocompleted = True
        print("  Autocomplete suggestion accepted")
    except Exception:
        print("  No autocomplete suggestion appeared")

    if unit:
        type_into(page, F_UNIT, unit)

    # City is required and starts empty. If the autocomplete did not fill it,
    # resolve it from the ZIP and type it in - this is the whole v2 fix.
    city_value = page.locator(F_CITY).input_value().strip()
    if city_value:
        result["city_source"] = "autocomplete"
        print(f"  City already filled by autocomplete: {city_value}")
    else:
        city, state = lookup_city_state(zip_code)
        if city:
            print(f"  City empty - ZIP {normalize_zip5(zip_code)} resolved to {city}, {state}")
            type_into(page, F_CITY, city)
            city_value = page.locator(F_CITY).input_value().strip()
            result["city_source"] = "zippopotam"
        else:
            print("  City empty and ZIP lookup gave nothing - submission will likely be rejected")
            result["city_source"] = "unresolved"

    result["address_used"] = {
        "street": street,
        "unit": unit,
        "city": city_value,
        "state": page.locator(F_STATE).input_value(),
        "zip": page.locator(F_ZIP).input_value(),
    }
    result["autocompleted"] = autocompleted
    print(f"  Address fields: {result['address_used']}")


def submit_address_and_read_popup(page, result):
    """Click through the address page and capture the recognized-vehicles popup.

    Progressive sometimes recognizes the mailing address and shows a "We found
    these vehicles at your address" popup listing vehicles already on file.
    Capturing that is the whole point of this run - we do not fill in the
    vehicle form or continue the quote past this point.

    Three outcomes are distinguished so a rejected address is never silently
    reported as "no vehicles found":
      vehicles_found - popup appeared, vehicles captured
      no_vehicles    - page advanced to the vehicle form, nothing on file
      blocked        - the address page rejected the input and never advanced
    """
    page.click('button:has-text("Ok, start my quote")')

    deadline = time.time() + 25
    while time.time() < deadline:
        # Popup with vehicles on file.
        if page.locator('text=We found these vehicles at your address').count():
            labels = [
                c.strip() for c in
                page.locator('[role="dialog"] label, [role="dialog"] li').all_inner_texts()
                if c.strip()
            ]
            seen = set()
            for label in labels:
                m = re.match(r'(\d{4})\s+(\S+)\s+(.+)', label)
                if not m:
                    continue
                # Each vehicle matches both the dialog's label and its li, so the
                # same car comes back twice - keep the first occurrence only.
                key = (m.group(1), m.group(2), m.group(3))
                if key in seen:
                    continue
                seen.add(key)
                result["address_recognized_vehicles"].append(
                    {"year": key[0], "make": key[1], "model": key[2]}
                )
            result["status"] = "vehicles_found"
            print(f"  Vehicles found at address: {result['address_recognized_vehicles']}")
            return

        # Advanced to the vehicle form - address accepted. Give the popup a few
        # more seconds in case it renders just after the navigation, then treat
        # the record as genuinely having nothing on file.
        if "VehiclesAllEdit" in page.url or "VehiclesNew" in page.url:
            for _ in range(6):
                page.wait_for_timeout(500)
                if page.locator('text=We found these vehicles at your address').count():
                    break
            else:
                result["status"] = "no_vehicles"
                print("  Address accepted - no vehicles on file at this address")
                return
            continue

        page.wait_for_timeout(500)

    # Never advanced. Surface why.
    errors = [
        t.strip() for t in page.locator('[role="alert"]').all_inner_texts() if t.strip()
    ]
    result["status"] = "blocked"
    result["blocked_errors"] = errors[:5]
    print(f"  BLOCKED on the address page: {errors[:3]}")


def process_entry(page, entry, result):
    """Run one record end-to-end on a page belonging to a freshly launched browser."""
    first_name = entry.get('first_name', '')
    middle_initial = entry.get('middle_initial', '')
    last_name = entry.get('last_name', '')
    address = entry.get('address', '')
    zip_code = entry.get('zip', '')
    # Fill in a firstnamelastname@gmail.com when no email was given, and record it.
    email = normalize_email(entry.get('email', ''), first_name, last_name)
    result["email"] = email
    # Coerce whatever DOB we got (a date, an age, or nothing) into MM/DD/YYYY,
    # seeded by the record so it is stable across runs. Record what we used.
    dob = normalize_dob(entry.get('dob', ''),
                        seed_key=f"{first_name}|{last_name}|{zip_code}|{address}")
    result["dob"] = dob

    # 1. Navigate to Progressive. The homepage ZIP box has maxlength=5, so a
    # ZIP+4 like "08081-1321" is truncated to "08081" as it is typed.
    print(f"Navigating to Progressive with ZIP: {zip_code}")
    page.goto("https://www.progressive.com/")
    page.fill('input[name="ZipCode"]', normalize_zip5(zip_code))
    # The homepage "Get a quote" control is an <input type="submit" id="qsButton_mma">,
    # not a <button id="get-a-quote-btn"> (that id does not exist on the live page).
    page.click('input#qsButton_mma')

    # 2. NameEdit Page
    print("Filling Personal Details...")
    page.wait_for_selector('input[id="NameEdit_embedded_questions_list_FirstName"]', timeout=60000)
    page.fill('input[id="NameEdit_embedded_questions_list_FirstName"]', first_name)
    if middle_initial:
        page.fill('input[id="NameEdit_embedded_questions_list_MiddleInitial"]', middle_initial)
    page.fill('input[id="NameEdit_embedded_questions_list_LastName"]', last_name)
    fill_masked_date(page, 'input[id="NameEdit_embedded_questions_list_DateOfBirth"]', dob)
    # Real field id is "...PrimaryEmailAddress", not "...EmailAddress".
    page.fill('input[id="NameEdit_embedded_questions_list_PrimaryEmailAddress"]', email)
    page.click('button:has-text("Continue")')

    # 3. AddressEdit Page
    fill_address(page, address, zip_code, result)
    submit_address_and_read_popup(page, result)


# One fixed results file that every run appends to / resumes from, instead of
# a new timestamped file per run.
OUTPUT_PATH = "progressive_results_v2.json"

# A record is "done" (skip on the next run) only if it actually reached a
# conclusion. blocked / error records are retried so a bad proxy or a hiccup
# does not permanently skip someone.
DONE_STATUSES = {"vehicles_found", "no_vehicles"}


def record_key(d):
    """Stable identity for a record, so a run can tell if it is already done.

    Works on both an input entry and a saved result (results carry zip/dob for
    exactly this reason)."""
    return (
        str(d.get("first_name", "")).strip().lower(),
        str(d.get("last_name", "")).strip().lower(),
        normalize_zip5(d.get("zip", "")),
        str(d.get("address", "")).strip().lower(),
    )


def load_prior_results(output_path):
    """Return {record_key: result} for whatever is already in the results file."""
    prior = {}
    if not os.path.exists(output_path):
        return prior
    try:
        with open(output_path, 'r') as f:
            for r in json.load(f):
                prior[record_key(r)] = r
    except Exception as e:
        print(f"Could not read existing {output_path} ({e}); starting a fresh file.")
    return prior


# ===========================================================================
# RESULTS DATABASE (SQLite)
# One row per record, upserted by identity - no full-file rewrite per record,
# so it stays fast at tens of thousands of records. The whole result dict is
# stored as JSON in `data`; `status` and `ord` are pulled out for querying and
# stable display order. Safe for the parallel workers via one lock.
# ===========================================================================
# DB location. Override with the DB_PATH env var so a deployment (e.g. Coolify)
# can point it at a persistent volume like /data/progressive_results_v2.db.
DB_PATH = os.environ.get("DB_PATH", "progressive_results_v2.db")
_KEYSEP = "\x1f"  # unit separator - never appears in names/addresses
_db_conn = None
_db_lock = threading.Lock()


def _rkey(d):
    return _KEYSEP.join(record_key(d))


def _db():
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        # WAL = concurrent readers while a worker writes; big throughput win.
        _db_conn.execute("PRAGMA journal_mode=WAL")
        _db_conn.execute("PRAGMA synchronous=NORMAL")
        _db_conn.execute(
            "CREATE TABLE IF NOT EXISTS results ("
            "rkey TEXT PRIMARY KEY, ord INTEGER, status TEXT, data TEXT)"
        )
        # Index status so resume/summary stay fast at millions of rows.
        _db_conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON results(status)")
        # Small key/value store for app settings (login credentials, secret).
        _db_conn.execute("CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT)")
        _db_conn.commit()
        _migrate_json_if_needed()
    return _db_conn


def get_setting(key, default=None):
    """Read a value from the settings key/value store."""
    with _db_lock:
        row = _db().execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key, value):
    """Write a value to the settings key/value store."""
    with _db_lock:
        c = _db()
        c.execute(
            "INSERT INTO settings(k, v) VALUES(?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, value),
        )
        c.commit()


def _migrate_json_if_needed():
    """One-time: if the old JSON file exists and the DB is empty, import it so
    no previously scraped result is lost when switching to the database."""
    try:
        count = _db_conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
        if count or not os.path.exists(OUTPUT_PATH):
            return
        with open(OUTPUT_PATH, "r") as f:
            rows = json.load(f)
        for i, r in enumerate(rows):
            _db_conn.execute(
                "INSERT OR REPLACE INTO results(rkey, ord, status, data) VALUES(?,?,?,?)",
                (_rkey(r), i, r.get("status"), json.dumps(r)),
            )
        _db_conn.commit()
        print(f"Migrated {len(rows)} record(s) from {OUTPUT_PATH} into {DB_PATH}.")
    except Exception as e:
        print(f"JSON->DB migration skipped ({e}).")


def save_result(result):
    """Insert or update one record's result (keyed by identity)."""
    rk = _rkey(result)
    with _db_lock:
        c = _db()
        row = c.execute("SELECT ord FROM results WHERE rkey=?", (rk,)).fetchone()
        order = row[0] if row else (
            c.execute("SELECT COALESCE(MAX(ord), -1) + 1 FROM results").fetchone()[0])
        c.execute(
            "INSERT INTO results(rkey, ord, status, data) VALUES(?,?,?,?) "
            "ON CONFLICT(rkey) DO UPDATE SET status=excluded.status, data=excluded.data",
            (rk, order, result.get("status"), json.dumps(result)),
        )
        c.commit()


def get_result(record):
    """Return the stored result for one record (by identity), or None.

    A single indexed primary-key lookup - this is what resume uses per record,
    so memory stays flat no matter how many millions of rows the DB holds."""
    with _db_lock:
        row = _db().execute("SELECT data FROM results WHERE rkey=?",
                            (_rkey(record),)).fetchone()
    return json.loads(row[0]) if row else None


def load_prior_map():
    """Return {record_key: result} for every stored record (for resume).

    Loads everything into memory - fine for small sets, but for large batches
    prefer get_result() per record. Kept for callers that want the whole map."""
    with _db_lock:
        rows = _db().execute("SELECT data FROM results").fetchall()
    out = {}
    for (data,) in rows:
        r = json.loads(data)
        out[record_key(r)] = r
    return out


def load_all():
    """Return all stored results in stable (input) order."""
    with _db_lock:
        rows = _db().execute("SELECT data FROM results ORDER BY ord").fetchall()
    return [json.loads(data) for (data,) in rows]


def clear_results():
    """Delete every stored result (the dashboard's 'Clear results')."""
    with _db_lock:
        c = _db()
        c.execute("DELETE FROM results")
        c.commit()


def run_one_record(entry, proxy=None, headless=False):
    """Process a single record in its own fresh browser; return (result, bw_stats).

    Self-contained - it starts and stops its own Playwright instance - so it is
    safe to call from parallel worker threads: each call gets a fully isolated
    browser, exactly the clean slate Progressive needs per applicant.
    """
    first_name = entry.get('first_name', '')
    last_name = entry.get('last_name', '')
    result = {
        "first_name": first_name,
        "middle_initial": entry.get('middle_initial', ''),
        "last_name": last_name,
        # phone/email are carried straight through - Progressive never sees them,
        # they just travel into the results so the finished data reads
        # "this name + phone owns these vehicles".
        "phone": entry.get('phone', ''),
        "email": entry.get('email', ''),
        # address/zip identify the record for resume matching; dob is filled in
        # by process_entry with the normalized value actually used.
        "address": entry.get('address', ''),
        "zip": entry.get('zip', ''),
        "dob": entry.get('dob', ''),
        "status": "error",
        "city_source": "",
        "autocompleted": False,
        "address_used": {},
        "address_recognized_vehicles": [],
    }
    bw_stats = {"total": 0, "blocked": 0, "by_type": {}}
    try:
        with sync_playwright() as p:
            launch_kwargs = {
                "headless": headless,
                # Server-friendly flags: /dev/shm is tiny in containers, so send
                # Chrome's shared memory to disk (NVMe) instead of crashing.
                "args": ["--disable-dev-shm-usage", "--no-sandbox",
                         "--disable-gpu", "--disable-features=site-per-process"],
            }
            if proxy:
                launch_kwargs["proxy"] = proxy
            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context()
            install_bandwidth_controls(context, bw_stats)
            page = context.new_page()
            try:
                process_entry(page, entry, result)
            except Exception as e:
                print(f"Error processing entry {first_name} {last_name}: {str(e)}")
                result["error"] = str(e)
            finally:
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        print(f"Error launching browser for {first_name} {last_name}: {str(e)}")
        result["error"] = str(e)
    return result, bw_stats


def run_automation(json_file_path):
    if not os.path.exists(json_file_path):
        print(f"Error: File '{json_file_path}' not found.")
        return

    with open(json_file_path, 'r') as f:
        data_entries = json.load(f)

    if isinstance(data_entries, dict):
        data_entries = [data_entries]

    total = len(data_entries)
    print(f"Resuming from {DB_PATH}: finished records will be skipped.")
    results = []

    proxy_list = parse_proxies(PROXIES) if USE_PROXIES else []
    if proxy_list:
        print(f"Loaded {len(proxy_list)} proxy(ies); rotating one per record.")
    else:
        print("No proxies configured; running with direct connection.")

    for index, entry in enumerate(data_entries, start=1):
        first_name = entry.get('first_name', '')
        last_name = entry.get('last_name', '')

        # Already finished on a previous run? Keep the saved result and move on.
        prev = get_result(entry)
        if prev and prev.get("status") in DONE_STATUSES:
            print(f"\n=== Record {index}/{total}: {first_name} {last_name} - already done ({prev['status']}), skipping ===")
            results.append(prev)
            continue

        print(f"\n=== Record {index}/{total}: {first_name} {last_name} ===")

        # Pick this record's proxy (round-robin through the list). Each record
        # gets its own freshly launched browser inside run_one_record, so nothing
        # leaks between applicants - the clean slate Progressive requires.
        proxy = proxy_list[(index - 1) % len(proxy_list)] if proxy_list else None
        proxy = with_session(proxy)  # unique sticky IP for this record (Proxy-Cheap)
        if proxy:
            print(f"Using proxy: {proxy['server']}")

        result, bw_stats = run_one_record(entry, proxy=proxy, headless=False)
        print_bandwidth_report(bw_stats)

        results.append(result)
        save_result(result)  # single-row upsert into the SQLite DB

        # Small pause so the old Chrome process is fully gone before the next launch.
        if index < total:
            print("Closing browser and restarting fresh for the next record...")
            time.sleep(2)

    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"\nResults saved to {DB_PATH}")
    print(f"Summary of {total} records: {counts}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python progressive_dynamic_automation_v2.py <path_to_json_file>")
    else:
        run_automation(sys.argv[1])
