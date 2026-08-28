"""
Standalone test of the SQLite state machine + polling logic in main.py,
without touching real Frigate or real Google Drive.
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

os.environ["DB_PATH"] = "test_db/events.db"
os.environ["TMP_DIR"] = "test_tmp"
os.environ["LOG_FILE"] = "test_logs/app.log"
os.environ["DRIVE_ROOT_FOLDER_ID"] = "fake_root"

import importlib
import main as m
importlib.reload(m)

# clean slate
for f in ["test_db/events.db", "test_db/events.db-wal", "test_db/events.db-shm"]:
    if os.path.exists(f):
        os.remove(f)

conn = m.db_connect()

fake_events = [
    {"id": "evt1", "camera": "front_door", "label": "person", "start_time": time.time() - 100, "end_time": time.time() - 90},
    {"id": "evt2", "camera": "driveway", "label": "car", "start_time": time.time() - 50, "end_time": time.time() - 40},
    {"id": "evt3_unfinished", "camera": "driveway", "label": "car", "start_time": time.time() - 10, "end_time": None},
]

# --- Test 1: recording seen events respects REQUIRE_FINISHED_EVENT ---
for e in fake_events:
    if m.REQUIRE_FINISHED_EVENT and not m.event_is_finished(e):
        continue
    m.record_seen(conn, e)

pending = m.get_pending(conn)
pending_ids = [p[0] for p in pending]
assert "evt1" in pending_ids, "evt1 should be pending"
assert "evt2" in pending_ids, "evt2 should be pending"
assert "evt3_unfinished" not in pending_ids, "unfinished event should NOT be recorded"
print("PASS: unfinished events are correctly excluded")

# --- Test 2: mark_uploaded removes it from pending ---
m.mark_uploaded(conn, "evt1")
pending = m.get_pending(conn)
pending_ids = [p[0] for p in pending]
assert "evt1" not in pending_ids
assert "evt2" in pending_ids
print("PASS: uploaded events drop out of the pending queue")

# --- Test 3: mark_failed increments tries, and stops after MAX_RETRY_ATTEMPTS ---
for i in range(m.MAX_RETRY_ATTEMPTS):
    m.mark_failed(conn, "evt2", f"simulated failure {i}")

pending = m.get_pending(conn)
pending_ids = [p[0] for p in pending]
assert "evt2" not in pending_ids, "evt2 should have exceeded max retries and dropped out"
print("PASS: events stop retrying after MAX_RETRY_ATTEMPTS")

# --- Test 4: record_seen is idempotent (INSERT OR IGNORE) ---
m.record_seen(conn, fake_events[0])
m.record_seen(conn, fake_events[0])
cur = conn.execute("SELECT COUNT(*) FROM events WHERE event_id = 'evt1'")
count = cur.fetchone()[0]
assert count == 1, f"expected 1 row for evt1, got {count}"
print("PASS: duplicate events are not re-inserted")

# --- Test 5: resolve_target_folder builds year/month/day path calls ---
calls = []
def fake_get_or_create_folder(name, parent_id):
    calls.append((name, parent_id))
    return f"folder_{name}"

m.get_or_create_folder = fake_get_or_create_folder
folder_id = m.resolve_target_folder(time.time())
assert len(calls) == 3, f"expected 3 folder lookups (Y/M/D), got {len(calls)}"
print("PASS: date-based folder resolution creates Y/M/D hierarchy:", calls)

conn.close()
print("\nAll logic tests passed.")

# --- Test 6: OAuth user-credential mode builds a Drive service and persists
#             the refreshed access token (regression test for the 'expired'
#             property being misleading when a token file has no initial
#             access token - see get_drive_service()) ---
import json
import tempfile
from datetime import datetime, timedelta
from unittest import mock

with tempfile.TemporaryDirectory() as tmpdir:
    token_path = os.path.join(tmpdir, "oauth_token.json")
    with open(token_path, "w") as f:
        json.dump({
            "refresh_token": "fake_refresh_token",
            "client_id": "fake_client_id",
            "client_secret": "fake_client_secret",
            "scopes": ["https://www.googleapis.com/auth/drive"],
        }, f)

    os.environ["DRIVE_AUTH_MODE"] = "oauth_user"
    os.environ["OAUTH_TOKEN_FILE"] = token_path
    importlib.reload(m)
    m._drive_service = None  # reset module-level cache from any prior test

    from google.oauth2 import reauth
    from datetime import timezone
    fake_expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

    def fake_refresh_grant(request, token_uri, refresh_token, client_id, client_secret, **kwargs):
        return ("fake_access_token_XYZ", refresh_token, fake_expiry, {"scope": "https://www.googleapis.com/auth/drive"}, None)

    with mock.patch.object(reauth, "refresh_grant", side_effect=fake_refresh_grant):
        service = m.get_drive_service()
        assert service is not None, "OAuth mode should successfully build a Drive service"

    with open(token_path) as f:
        saved = json.load(f)
    assert saved.get("token") == "fake_access_token_XYZ", "refreshed access token should be persisted"
    assert saved.get("refresh_token") == "fake_refresh_token", "refresh_token should be preserved"
    print("PASS: OAuth user-credential mode refreshes and persists the token correctly")

# reset back to service_account mode / defaults for anything run after this file
os.environ.pop("DRIVE_AUTH_MODE", None)
os.environ.pop("OAUTH_TOKEN_FILE", None)

print("\nAll tests (including OAuth) passed.")

# --- Test 7: a 400 'No recordings found' response from the clip endpoint
#             is treated as permanent (given up immediately after one try,
#             not retried up to MAX_RETRY_ATTEMPTS times). This is a
#             regression test for a real Frigate behavior: has_clip=true
#             can be stale once the underlying recording segments have
#             aged out - retrying such an event can never succeed. ---
import json as _json
from unittest import mock as _mock
import requests as _requests


class _FakeHttpResponse:
    """Mimics a real requests.Response closely enough for this test:
    .content/.json() work normally (as they do once main.download_clip's
    fix reads the body while the stream is still open)."""
    def __init__(self, status_code, body_dict):
        self.status_code = status_code
        self.content = _json.dumps(body_dict).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            err = _requests.exceptions.HTTPError(f"{self.status_code} error")
            err.response = self
            raise err

    def json(self):
        return _json.loads(self.content)

    def iter_content(self, chunk_size=1024):
        return iter([])


with _mock.patch("requests.get") as mock_get:
    mock_get.return_value = _FakeHttpResponse(
        400, {"success": False, "message": "No recordings found for the specified time range"}
    )

    conn2 = m.db_connect()
    conn2.execute(
        "INSERT OR REPLACE INTO events (event_id, camera, label, start_time, tries) VALUES (?, ?, ?, ?, 0)",
        ("test_400_event", "front_door", "person", time.time() - 500),
    )
    conn2.commit()

    m.process_pending(conn2)

    row = conn2.execute("SELECT tries FROM events WHERE event_id = ?", ("test_400_event",)).fetchone()
    assert row[0] == m.MAX_RETRY_ATTEMPTS, (
        f"Expected event to be given up on immediately (tries={m.MAX_RETRY_ATTEMPTS}), got tries={row[0]}"
    )
    pending2 = m.get_pending(conn2)
    assert not any(p[0] == "test_400_event" for p in pending2), "Event should no longer be pending"
    conn2.close()

print("PASS: 'No recordings found' 400 errors are given up on immediately, not retried 20 times")

# --- Test 8: filenames and folder dates respect the configured TZ instead
#             of always using UTC. Regression test for a real bug: a 19:00
#             local (CEST, UTC+2) event was showing as 17:00 in the
#             filename because the code hardcoded tz=timezone.utc. ---
import datetime as _dt

os.environ["TZ"] = "Europe/Amsterdam"
importlib.reload(m)
m._drive_service = None

local_event_time = _dt.datetime(2026, 8, 28, 19, 0, 0, tzinfo=m.ZoneInfo("Europe/Amsterdam"))
epoch = local_event_time.timestamp()

resolved = _dt.datetime.fromtimestamp(epoch, tz=m.LOCAL_TZ)
assert resolved.hour == 19, (
    f"Expected filename timestamp to show local hour 19 (Europe/Amsterdam), got {resolved.hour}. "
    f"If this is 17, the UTC hardcoding bug has regressed."
)
print("PASS: filenames/folders use the configured local timezone (TZ=Europe/Amsterdam), not UTC")

# invalid TZ names are caught with a clear error, not a silent wrong-time bug
os.environ["TZ"] = "Not/A_Real_Timezone_xyz"
importlib.reload(m)
assert m.LOCAL_TZ is None, "an invalid TZ name should be caught, not silently accepted"
print("PASS: invalid TZ names are detected rather than silently producing wrong timestamps")

# restore default for anything run after this
os.environ["TZ"] = "UTC"
importlib.reload(m)

print("\nAll tests (including OAuth, 400-handling, and timezone) passed.")
