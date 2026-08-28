"""
Tests for status_dashboard.py - exercises real routes with a real
temporary SQLite DB, mocked systemctl calls, and no real Flask server
needed beyond the test client.
"""
import base64
import os
import sqlite3
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

_tmp_dir = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp_dir, "events.db")
os.environ["LOG_FILE"] = os.path.join(_tmp_dir, "app.log")
os.environ["HEARTBEAT_FILE"] = os.path.join(_tmp_dir, "heartbeat.txt")
os.environ["POLL_INTERVAL_SECONDS"] = "30"

import status_dashboard as dash

dash.app.testing = True
client = dash.app.test_client()


def make_test_db(path):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE events (
            event_id TEXT PRIMARY KEY, camera TEXT, label TEXT, start_time REAL,
            uploaded INTEGER DEFAULT 0, tries INTEGER DEFAULT 0, last_error TEXT,
            created REAL DEFAULT (strftime('%s','now')),
            drive_file_id TEXT, file_size_bytes INTEGER, drive_deleted INTEGER DEFAULT 0
        )
    """)
    now = time.time()
    conn.execute("INSERT INTO events (event_id, camera, label, start_time, uploaded, drive_file_id, file_size_bytes) VALUES (?,?,?,?,1,?,?)",
                 ("e1", "front_door", "person", now - 100, "fid1", 5_000_000))
    conn.execute("INSERT INTO events (event_id, camera, label, start_time, uploaded, tries) VALUES (?,?,?,?,0,3)",
                 ("e2", "front_door", "car", now - 50))
    conn.execute("INSERT INTO events (event_id, camera, label, start_time, uploaded, tries) VALUES (?,?,?,?,0,20)",
                 ("e3", "backyard", "person", now - 20))
    conn.execute("INSERT INTO events (event_id, camera, label, start_time, uploaded, drive_file_id, file_size_bytes, drive_deleted) VALUES (?,?,?,?,1,?,?,1)",
                 ("e4", "backyard", "cat", now - 900000, "fid4", 2_000_000))
    conn.commit()
    conn.close()


# --- Test 1: no DB yet -> dashboard still renders gracefully ---
def fake_run_no_db(*args, **kwargs):
    class R: stdout = "inactive\n"
    return R()

with mock.patch.object(dash.subprocess, "run", side_effect=fake_run_no_db):
    resp = client.get("/")
assert resp.status_code == 200
assert b"No database found yet" in resp.data
assert b"Not running" in resp.data
print("PASS: dashboard renders gracefully with no database yet (fresh install)")

# --- Test 2: with a real DB, stats are computed correctly ---
make_test_db(os.environ["DB_PATH"])

def fake_run_active(*args, **kwargs):
    class R: stdout = "active\n"
    return R()

with open(os.environ["HEARTBEAT_FILE"], "w") as f:
    f.write(str(time.time()))

with mock.patch.object(dash.subprocess, "run", side_effect=fake_run_active):
    resp = client.get("/")
assert resp.status_code == 200
assert b"Running" in resp.data
body = resp.data.decode()
assert ">1<" in body  # 1 uploaded (e1; e4 was retention-deleted so excluded from "retained")
print("PASS: dashboard correctly computes stats from a real database")

# --- Test 3: recent events table shows correct statuses ---
assert "Uploaded" in body
assert "Pending (try 3)" in body
assert "Given up" in body
assert "Deleted (retention)" in body
print("PASS: recent events table correctly labels uploaded/pending/given-up/retention-deleted states")

# --- Test 4: stale heartbeat triggers a warning, not silently shown as healthy ---
with open(os.environ["HEARTBEAT_FILE"], "w") as f:
    f.write(str(time.time() - 300))  # 5 minutes old, way past 3x30s threshold

with mock.patch.object(dash.subprocess, "run", side_effect=fake_run_active):
    resp = client.get("/")
assert b"Warning" in resp.data
assert b"may be stuck" in resp.data
print("PASS: a stale heartbeat (process alive but not progressing) shows a warning, not 'Running'")

# --- Test 5: systemd inactive AND stale heartbeat together means genuinely not running ---
with open(os.environ["HEARTBEAT_FILE"], "w") as f:
    f.write(str(time.time() - 300))  # stale heartbeat

def fake_run_failed(*args, **kwargs):
    class R: stdout = "failed\n"
    return R()

with mock.patch.object(dash.subprocess, "run", side_effect=fake_run_failed):
    resp = client.get("/")
assert b"Not running" in resp.data
print("PASS: systemd 'failed' + stale heartbeat together correctly shows as not running")

# --- Test 5b: a FRESH heartbeat overrides a stale/wrong systemd read - the
#              heartbeat is stronger ground truth than systemd's cached state ---
with open(os.environ["HEARTBEAT_FILE"], "w") as f:
    f.write(str(time.time()))  # fresh heartbeat, but systemd (mocked) says failed

with mock.patch.object(dash.subprocess, "run", side_effect=fake_run_failed):
    resp = client.get("/")
assert b"Running" in resp.data
print("PASS: a fresh heartbeat is trusted over a stale/incorrect systemd read (heartbeat is stronger evidence)")

# --- Test 6: log download serves the actual file content ---
with open(os.environ["LOG_FILE"], "w") as f:
    f.write("test log line 1\ntest log line 2\n")

with mock.patch.object(dash.subprocess, "run", side_effect=fake_run_active):
    resp = client.get("/logs/download")
assert resp.status_code == 200
assert b"test log line 1" in resp.data
assert "attachment" in resp.headers.get("Content-Disposition", "")
print("PASS: log download serves the real file content as an attachment")

# --- Test 7: log download when no log file exists yet ---
os.remove(os.environ["LOG_FILE"])
resp = client.get("/logs/download")
assert resp.status_code == 404
print("PASS: missing log file returns 404 instead of crashing")

# --- Test 8: log tail endpoint ---
with open(os.environ["LOG_FILE"], "w") as f:
    for i in range(300):
        f.write(f"line {i}\n")
resp = client.get("/logs/tail?lines=5")
assert resp.status_code == 200
lines = resp.data.decode().strip().split("\n")
assert len(lines) == 5
assert lines[-1] == "line 299"
print("PASS: log tail endpoint returns exactly the requested number of most-recent lines")

# --- Test 9: no auth configured -> open access ---
dash.DASHBOARD_USERNAME = ""
dash.DASHBOARD_PASSWORD = ""
with mock.patch.object(dash.subprocess, "run", side_effect=fake_run_active):
    resp = client.get("/")
assert resp.status_code == 200
print("PASS: with no DASHBOARD_USERNAME/PASSWORD set, access is open (no 401)")

# --- Test 10: auth configured -> blocks without credentials, allows with correct ones ---
dash.DASHBOARD_USERNAME = "admin"
dash.DASHBOARD_PASSWORD = "secret123"

with mock.patch.object(dash.subprocess, "run", side_effect=fake_run_active):
    resp_no_auth = client.get("/")
    assert resp_no_auth.status_code == 401, "should require auth once configured"

    creds = base64.b64encode(b"admin:secret123").decode()
    resp_good = client.get("/", headers={"Authorization": f"Basic {creds}"})
    assert resp_good.status_code == 200

    bad_creds = base64.b64encode(b"admin:wrongpass").decode()
    resp_bad = client.get("/", headers={"Authorization": f"Basic {bad_creds}"})
    assert resp_bad.status_code == 401

print("PASS: HTTP Basic Auth correctly blocks missing/wrong credentials and allows correct ones")

# reset for cleanliness
dash.DASHBOARD_USERNAME = ""
dash.DASHBOARD_PASSWORD = ""

# --- Test 11: retention policy summary reflects actual config ---
dash.DRIVE_RETENTION_MAX_AGE_DAYS = 30
dash.DRIVE_RETENTION_MAX_SIZE_GB = 50
dash.DRIVE_RETENTION_DRY_RUN = True
with mock.patch.object(dash.subprocess, "run", side_effect=fake_run_active):
    resp = client.get("/")
assert b"max age 30 days" in resp.data
assert b"max size 50" in resp.data
assert b"DRY RUN" in resp.data
print("PASS: retention policy summary correctly reflects active config including dry-run flag")

dash.DRIVE_RETENTION_MAX_AGE_DAYS = 0
dash.DRIVE_RETENTION_MAX_SIZE_GB = 0
with mock.patch.object(dash.subprocess, "run", side_effect=fake_run_active):
    resp = client.get("/")
assert b"Retention is disabled" in resp.data
print("PASS: retention shown as disabled when both settings are 0 (the default)")

print("\nAll status_dashboard tests passed.")
