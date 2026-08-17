# Progressive Automation

A control-panel for running Progressive auto-insurance quote lookups at scale and
collecting the vehicles Progressive recognizes at each address.

## What it does

- Takes a list of people (name + address + ZIP is enough) from a CSV / paste / JSON.
- For each record, drives a fresh Chrome through the Progressive quote flow and reads
  the "vehicles found at your address" result.
- Saves everything to a SQLite database and shows it in a web dashboard with live
  progress, filtering, per-vehicle CSV export, an errors/retry view, and login.

## Pieces

| File | Role |
|------|------|
| `progressive_dynamic_automation_v2.py` | Core automation + SQLite results layer |
| `dashboard.py` | Flask web dashboard (login, run control, tabs, exports) |
| `nixpacks.toml` / `requirements.txt` | Deploy build (Coolify / Nixpacks) |

## Run locally

```bash
pip install -r requirements.txt
python -m playwright install chromium
python dashboard.py            # http://127.0.0.1:5050  (login: admin / admin)
```

## Deploy (Coolify + Nixpacks)

Runs headful Chrome inside Xvfb (Progressive blocks headless). Set env
`HOST=0.0.0.0`, `DB_PATH=/data/progressive_results_v2.db`, mount a persistent
volume at `/data`, point a domain at it, and Coolify handles HTTPS.

## Notes

- Proxies are optional (Proxy-Cheap residential, sticky IP per record).
- Bandwidth saving blocks images/fonts/trackers to keep proxy usage low.
- DOB and email auto-fill when not provided.
