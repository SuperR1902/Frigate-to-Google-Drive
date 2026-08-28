#!/usr/bin/env python3
"""
Frigate -> Google Drive uploader: status dashboard.

Runs continuously inside the same container as the uploader service.
Shows service health, upload statistics, recent activity, retention
config, and lets you download the log file for troubleshooting.

Reads the same .env as main.py, so run this from the same directory.

Usage:
    pip install -r requirements.txt   # flask is included there
    python3 status_dashboard.py

Then open http://<container-ip>:8080 (or DASHBOARD_PORT) in a browser.

Security note: this is read-only (no writes, no secrets ever shown -
tokens/passwords in .env are redacted), but it does expose event
metadata (camera names, timestamps) and log contents on your network.
Set DASHBOARD_USERNAME and DASHBOARD_PASSWORD in .env to require HTTP
Basic Auth if this matters for your setup; leave them blank to skip
auth entirely.
"""
import glob
import os
import sqlite3
import subprocess
import time
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_file

load_dotenv()


def env(name, default=None, cast=str):
    val = os.environ.get(name, default)
    if val is None:
        return None
    if cast is bool:
        return str(val).strip().lower() in ("1", "true", "yes", "on")
    return cast(val)


DB_PATH = env("DB_PATH", "db/events.db")
LOG_FILE = env("LOG_FILE", "logs/app.log")
HEARTBEAT_FILE = env("HEARTBEAT_FILE", "db/heartbeat.txt")
POLL_INTERVAL_SECONDS = env("POLL_INTERVAL_SECONDS", 30, int)
SERVICE_UNIT_NAME = env("SERVICE_UNIT_NAME", "frigate-gdrive-uploader")

DRIVE_RETENTION_MAX_AGE_DAYS = env("DRIVE_RETENTION_MAX_AGE_DAYS", 0, int)
DRIVE_RETENTION_MAX_SIZE_GB = env("DRIVE_RETENTION_MAX_SIZE_GB", 0, float)
DRIVE_RETENTION_DRY_RUN = env("DRIVE_RETENTION_DRY_RUN", False, bool)

DASHBOARD_PORT = env("DASHBOARD_PORT", 8080, int)
DASHBOARD_USERNAME = env("DASHBOARD_USERNAME", "")
DASHBOARD_PASSWORD = env("DASHBOARD_PASSWORD", "")

app = Flask(__name__)

PAGE_STYLE = """
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 900px;
         margin: 40px auto; padding: 0 20px; color: #1a1a1a; line-height: 1.5; }
  h1 { font-size: 1.4em; } h2 { font-size: 1.1em; margin-top: 32px; }
  .cards { display: flex; gap: 12px; flex-wrap: wrap; margin: 16px 0; }
  .card { background: #f8f9fa; border-radius: 8px; padding: 16px 20px; min-width: 140px; }
  .card .num { font-size: 1.8em; font-weight: 700; }
  .card .label { font-size: 13px; color: #666; }
  .status-badge { display: inline-block; padding: 4px 12px; border-radius: 12px; font-size: 13px; font-weight: 600; }
  .status-ok { background: #e6f4ea; color: #137333; }
  .status-warn { background: #fef7e0; color: #b06000; }
  .status-fail { background: #fce8e6; color: #c5221f; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; margin-top: 8px; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #eee; }
  th { color: #666; font-weight: 600; }
  .btn { background: #1a73e8; color: white; border: none; padding: 8px 16px;
        border-radius: 4px; font-size: 14px; cursor: pointer; text-decoration: none;
        display: inline-block; }
  .btn:hover { background: #1558b0; }
  pre { background: #1e1e1e; color: #d4d4d4; padding: 14px; border-radius: 6px;
        overflow-x: auto; font-size: 12px; max-height: 400px; overflow-y: auto; }
  .muted { color: #888; font-size: 13px; }
</style>
"""


def requires_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not DASHBOARD_USERNAME and not DASHBOARD_PASSWORD:
            return f(*args, **kwargs)  # auth not configured - open access
        auth = request.authorization
        if not auth or auth.username != DASHBOARD_USERNAME or auth.password != DASHBOARD_PASSWORD:
            return Response(
                "Authentication required", 401,
                {"WWW-Authenticate": 'Basic realm="Frigate Dashboard"'},
            )
        return f(*args, **kwargs)
    return wrapper


def get_service_status():
    """Returns (state, detail). Heartbeat freshness is the primary signal
    since it's self-contained (no permission/D-Bus dependencies); systemctl
    is corroborating context, not the sole gate - a systemctl query can
    fail for environmental reasons (e.g. D-Bus access in some unprivileged
    container setups) that have nothing to do with whether the uploader
    itself is actually working."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", SERVICE_UNIT_NAME],
            capture_output=True, text=True, timeout=5,
        )
        systemd_state = result.stdout.strip()
    except Exception as e:
        systemd_state = f"unknown ({e})"

    heartbeat_age = None
    if os.path.exists(HEARTBEAT_FILE):
        try:
            with open(HEARTBEAT_FILE) as f:
                heartbeat_age = time.time() - float(f.read().strip())
        except Exception:
            pass

    if heartbeat_age is not None and heartbeat_age <= POLL_INTERVAL_SECONDS * 3:
        return "ok", f"active, last heartbeat {int(heartbeat_age)}s ago (systemd: {systemd_state})"

    if systemd_state == "active":
        if heartbeat_age is None:
            return "warn", "service is active, but no heartbeat recorded yet"
        return "warn", f"service is active, but last heartbeat was {int(heartbeat_age)}s ago (expected every ~{POLL_INTERVAL_SECONDS}s) - may be stuck"

    if systemd_state in ("inactive", "failed"):
        return "fail", f"systemd reports: {systemd_state}"

    return "warn", f"could not determine status (systemd query: {systemd_state}, heartbeat: {'stale' if heartbeat_age else 'missing'})"


def get_stats():
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT "
        "SUM(uploaded = 1) AS uploaded, "
        "SUM(uploaded = 0 AND tries >= 20) AS given_up, "
        "SUM(uploaded = 0 AND tries < 20) AS pending, "
        "SUM(uploaded = 1 AND drive_deleted = 0) AS retained, "
        "SUM(uploaded = 1 AND drive_deleted = 1) AS retention_deleted, "
        "SUM(CASE WHEN uploaded = 1 AND drive_deleted = 0 THEN file_size_bytes ELSE 0 END) AS bytes_stored "
        "FROM events"
    ).fetchone()
    recent = conn.execute(
        "SELECT event_id, camera, label, start_time, uploaded, tries, drive_deleted, last_error "
        "FROM events ORDER BY created DESC LIMIT 25"
    ).fetchall()
    conn.close()
    return {"totals": dict(row), "recent": [dict(r) for r in recent]}


@app.route("/")
@requires_auth
def index():
    state, detail = get_service_status()
    stats = get_stats()

    badge_class = {"ok": "status-ok", "warn": "status-warn", "fail": "status-fail"}[state]
    badge_label = {"ok": "Running", "warn": "Warning", "fail": "Not running"}[state]

    if stats is None:
        cards_html = '<p class="muted">No database found yet at ' + DB_PATH + ' - service may not have started, or hasn\'t processed any events yet.</p>'
        table_html = ""
    else:
        t = stats["totals"]
        gb_stored = (t["bytes_stored"] or 0) / (1024 ** 3)
        cards_html = f"""
        <div class="cards">
          <div class="card"><div class="num">{t['uploaded'] or 0}</div><div class="label">Uploaded</div></div>
          <div class="card"><div class="num">{t['pending'] or 0}</div><div class="label">Pending</div></div>
          <div class="card"><div class="num">{t['given_up'] or 0}</div><div class="label">Given up (permanent)</div></div>
          <div class="card"><div class="num">{t['retained'] or 0}</div><div class="label">Currently on Drive</div></div>
          <div class="card"><div class="num">{gb_stored:.2f} GB</div><div class="label">Tracked storage used</div></div>
        </div>
        """
        rows_html = ""
        for r in stats["recent"]:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["start_time"])) if r["start_time"] else "?"
            if r["uploaded"]:
                status = "Deleted (retention)" if r["drive_deleted"] else "Uploaded"
            elif r["tries"] >= 20:
                status = "Given up"
            else:
                status = f"Pending (try {r['tries']})"
            error = (r["last_error"] or "")[:80]
            rows_html += f"<tr><td>{when}</td><td>{r['camera']}</td><td>{r['label']}</td><td>{status}</td><td class='muted'>{error}</td></tr>"
        table_html = f"""
        <h2>Recent events</h2>
        <table>
          <tr><th>Time</th><th>Camera</th><th>Label</th><th>Status</th><th>Last error</th></tr>
          {rows_html}
        </table>
        """

    retention_html = "<p class='muted'>Retention is disabled (default) - uploaded files are kept forever.</p>"
    if DRIVE_RETENTION_MAX_AGE_DAYS or DRIVE_RETENTION_MAX_SIZE_GB:
        parts = []
        if DRIVE_RETENTION_MAX_AGE_DAYS:
            parts.append(f"max age {DRIVE_RETENTION_MAX_AGE_DAYS} days")
        if DRIVE_RETENTION_MAX_SIZE_GB:
            parts.append(f"max size {DRIVE_RETENTION_MAX_SIZE_GB} GB")
        dry_run_note = " (DRY RUN - nothing is actually being deleted)" if DRIVE_RETENTION_DRY_RUN else ""
        retention_html = f"<p>Active: {', '.join(parts)}{dry_run_note}</p>"

    return f"""
<!doctype html>
<html><head><title>Frigate -> Drive: Status</title>{PAGE_STYLE}<meta http-equiv="refresh" content="30"></head>
<body>
<h1>Frigate &rarr; Google Drive: Status</h1>
<p><span class="status-badge {badge_class}">{badge_label}</span> <span class="muted">{detail}</span></p>

{cards_html}

<h2>Retention policy</h2>
{retention_html}

{table_html}

<h2>Logs</h2>
<p><a class="btn" href="/logs/download">Download full log file</a></p>

<h2>API</h2>
<p class="muted">Machine-readable status at <code>/api/status</code> - built for Home Assistant's <code>rest:</code> integration (see README), but it's plain JSON so anything can poll it.</p>

<p class="muted">Page auto-refreshes every 30 seconds.</p>
</body></html>
"""


@app.route("/api/status")
@requires_auth
def api_status():
    """Machine-readable status for external tools - built specifically to
    be easy to consume from Home Assistant's `rest:` integration, but it's
    plain JSON so anything can poll it. One request here covers everything
    (state, counts, storage, retention config) rather than needing several
    separate calls."""
    state, detail = get_service_status()
    stats = get_stats()

    heartbeat_age = None
    if os.path.exists(HEARTBEAT_FILE):
        try:
            with open(HEARTBEAT_FILE) as f:
                heartbeat_age = round(time.time() - float(f.read().strip()), 1)
        except Exception:
            pass

    totals = stats["totals"] if stats else {}
    bytes_stored = totals.get("bytes_stored") or 0

    return jsonify({
        "state": state,  # "ok" | "warn" | "fail"
        "detail": detail,
        "heartbeat_age_seconds": heartbeat_age,
        "uploaded": totals.get("uploaded") or 0,
        "pending": totals.get("pending") or 0,
        "given_up": totals.get("given_up") or 0,
        "retained": totals.get("retained") or 0,
        "retention_deleted": totals.get("retention_deleted") or 0,
        "bytes_stored": bytes_stored,
        "gb_stored": round(bytes_stored / (1024 ** 3), 3),
        "retention_enabled": bool(DRIVE_RETENTION_MAX_AGE_DAYS or DRIVE_RETENTION_MAX_SIZE_GB),
        "retention_max_age_days": DRIVE_RETENTION_MAX_AGE_DAYS,
        "retention_max_size_gb": DRIVE_RETENTION_MAX_SIZE_GB,
        "retention_dry_run": DRIVE_RETENTION_DRY_RUN,
        "timestamp": time.time(),
    })


@app.route("/logs/download")
@requires_auth
def download_log():
    if not os.path.exists(LOG_FILE):
        return "Log file not found.", 404
    return send_file(LOG_FILE, as_attachment=True, download_name=os.path.basename(LOG_FILE))


@app.route("/logs/tail")
@requires_auth
def tail_log():
    n = int(request.args.get("lines", 200))
    if not os.path.exists(LOG_FILE):
        return "Log file not found.", 404
    with open(LOG_FILE) as f:
        lines = f.readlines()[-n:]
    return Response("".join(lines), mimetype="text/plain")


if __name__ == "__main__":
    print(f"\nStatus dashboard running at http://0.0.0.0:{DASHBOARD_PORT}\n")
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False)
