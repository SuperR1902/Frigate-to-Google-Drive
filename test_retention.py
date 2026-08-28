"""
Tests for Drive retention logic in main.py: age-based deletion,
size-based deletion (oldest first), dry-run mode, and safe handling of
already-deleted (404) files. Uses a mocked Drive service - no real
Google API calls.
"""
import importlib
import os
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

os.environ["DB_PATH"] = "test_retention_db/events.db"
os.environ["TMP_DIR"] = "test_retention_tmp"
os.environ["LOG_FILE"] = "test_retention_logs/app.log"
os.environ["DRIVE_ROOT_FOLDER_ID"] = "fake_root"

for f in ["test_retention_db/events.db"]:
    if os.path.exists(f):
        os.remove(f)

import main as m
importlib.reload(m)


def insert_event(conn, event_id, start_time, uploaded=1, drive_file_id="fid", file_size_bytes=1000, drive_deleted=0):
    conn.execute(
        "INSERT INTO events (event_id, camera, label, start_time, uploaded, drive_file_id, file_size_bytes, drive_deleted) "
        "VALUES (?, 'cam', 'person', ?, ?, ?, ?, ?)",
        (event_id, start_time, uploaded, drive_file_id, file_size_bytes, drive_deleted),
    )
    conn.commit()


class FakeDeleteRequest:
    def __init__(self, should_fail_404=False, should_fail_other=False):
        self.should_fail_404 = should_fail_404
        self.should_fail_other = should_fail_other

    def execute(self):
        if self.should_fail_404:
            class FakeResp:
                status = 404
            err = Exception("not found")
            err.resp = FakeResp()
            raise err
        if self.should_fail_other:
            class FakeResp:
                status = 500
            err = Exception("server error")
            err.resp = FakeResp()
            raise err
        return {}


class FakeFilesService:
    def __init__(self, fail_ids=None, notfound_ids=None):
        self.fail_ids = fail_ids or set()
        self.notfound_ids = notfound_ids or set()
        self.deleted_ids = []

    def delete(self, fileId, supportsAllDrives=True):
        self.deleted_ids.append(fileId)
        return FakeDeleteRequest(
            should_fail_404=fileId in self.notfound_ids,
            should_fail_other=fileId in self.fail_ids,
        )


class FakeDriveService:
    def __init__(self, fail_ids=None, notfound_ids=None):
        self._files = FakeFilesService(fail_ids, notfound_ids)

    def files(self):
        return self._files


# --- Test 1: age-based retention deletes only events older than the cutoff ---
conn = m.db_connect()
now = time.time()
insert_event(conn, "old_event", now - 40 * 86400)   # 40 days old
insert_event(conn, "recent_event", now - 5 * 86400)  # 5 days old

fake_service = FakeDriveService()
m.DRIVE_RETENTION_MAX_AGE_DAYS = 30
m.DRIVE_RETENTION_MAX_SIZE_GB = 0
m.DRIVE_RETENTION_DRY_RUN = False

with mock.patch.object(m, "get_drive_service", return_value=fake_service):
    m.enforce_retention(conn)

assert "fid" in fake_service._files.deleted_ids or len(fake_service._files.deleted_ids) == 1
row_old = conn.execute("SELECT drive_deleted FROM events WHERE event_id='old_event'").fetchone()
row_recent = conn.execute("SELECT drive_deleted FROM events WHERE event_id='recent_event'").fetchone()
assert row_old[0] == 1, "event older than max age should be marked deleted"
assert row_recent[0] == 0, "event within max age should NOT be touched"
print("PASS: age-based retention deletes only events past the age cutoff, leaves recent ones alone")

# --- Test 2: size-based retention deletes oldest-first until under budget ---
conn2 = m.db_connect()
os.remove("test_retention_db/events.db")
conn2 = m.db_connect()
# 3 events, each 1GB, oldest first - with a 2GB cap, the oldest one should go
one_gb = 1024 ** 3
insert_event(conn2, "e1_oldest", now - 300, drive_file_id="f1", file_size_bytes=one_gb)
insert_event(conn2, "e2_middle", now - 200, drive_file_id="f2", file_size_bytes=one_gb)
insert_event(conn2, "e3_newest", now - 100, drive_file_id="f3", file_size_bytes=one_gb)

fake_service2 = FakeDriveService()
m.DRIVE_RETENTION_MAX_AGE_DAYS = 0
m.DRIVE_RETENTION_MAX_SIZE_GB = 2.0
m.DRIVE_RETENTION_DRY_RUN = False

with mock.patch.object(m, "get_drive_service", return_value=fake_service2):
    m.enforce_retention(conn2)

deleted = {r[0] for r in conn2.execute("SELECT event_id FROM events WHERE drive_deleted = 1").fetchall()}
assert deleted == {"e1_oldest"}, f"expected only the oldest event deleted to get under 2GB, got: {deleted}"
print("PASS: size-based retention deletes oldest files first, stops once under the size cap")

# --- Test 3: dry-run mode makes no actual delete calls and marks nothing deleted ---
os.remove("test_retention_db/events.db")
conn3 = m.db_connect()
insert_event(conn3, "dry_run_event", now - 40 * 86400, drive_file_id="f_dry")

fake_service3 = FakeDriveService()
m.DRIVE_RETENTION_MAX_AGE_DAYS = 30
m.DRIVE_RETENTION_MAX_SIZE_GB = 0
m.DRIVE_RETENTION_DRY_RUN = True

with mock.patch.object(m, "get_drive_service", return_value=fake_service3):
    m.enforce_retention(conn3)

assert len(fake_service3._files.deleted_ids) == 0, "dry run must not call Drive's delete API at all"
row = conn3.execute("SELECT drive_deleted FROM events WHERE event_id='dry_run_event'").fetchone()
assert row[0] == 0, "dry run must not mark anything as deleted"
print("PASS: dry-run mode makes zero actual API calls and marks nothing as deleted")

# --- Test 4: a file already gone from Drive (404) is treated as successfully cleaned up ---
os.remove("test_retention_db/events.db")
conn4 = m.db_connect()
insert_event(conn4, "already_gone", now - 40 * 86400, drive_file_id="f_gone")

fake_service4 = FakeDriveService(notfound_ids={"f_gone"})
m.DRIVE_RETENTION_MAX_AGE_DAYS = 30
m.DRIVE_RETENTION_MAX_SIZE_GB = 0
m.DRIVE_RETENTION_DRY_RUN = False

with mock.patch.object(m, "get_drive_service", return_value=fake_service4):
    m.enforce_retention(conn4)

row = conn4.execute("SELECT drive_deleted FROM events WHERE event_id='already_gone'").fetchone()
assert row[0] == 1, "a 404 on delete should still be treated as cleaned up (idempotent)"
print("PASS: a file already missing from Drive (404) is treated as successfully cleaned up, not an error loop")

# --- Test 5: a real delete failure (not 404) is NOT marked deleted, so it retries next cycle ---
os.remove("test_retention_db/events.db")
conn5 = m.db_connect()
insert_event(conn5, "delete_fails", now - 40 * 86400, drive_file_id="f_fail")

fake_service5 = FakeDriveService(fail_ids={"f_fail"})
m.DRIVE_RETENTION_MAX_AGE_DAYS = 30
m.DRIVE_RETENTION_MAX_SIZE_GB = 0
m.DRIVE_RETENTION_DRY_RUN = False

with mock.patch.object(m, "get_drive_service", return_value=fake_service5):
    m.enforce_retention(conn5)

row = conn5.execute("SELECT drive_deleted FROM events WHERE event_id='delete_fails'").fetchone()
assert row[0] == 0, "a genuine delete failure should NOT be marked deleted, so it's retried later"
print("PASS: a genuine delete failure (not 404) leaves the record untouched for retry next cycle")

# --- Test 6: retention disabled entirely (both settings at 0) does nothing, no Drive calls ---
os.remove("test_retention_db/events.db")
conn6 = m.db_connect()
insert_event(conn6, "should_not_be_touched", now - 999 * 86400, drive_file_id="f_untouched")

fake_service6 = FakeDriveService()
m.DRIVE_RETENTION_MAX_AGE_DAYS = 0
m.DRIVE_RETENTION_MAX_SIZE_GB = 0
m.DRIVE_RETENTION_DRY_RUN = False

with mock.patch.object(m, "get_drive_service", return_value=fake_service6):
    m.enforce_retention(conn6)

assert len(fake_service6._files.deleted_ids) == 0, "retention disabled should make zero Drive calls"
row = conn6.execute("SELECT drive_deleted FROM events WHERE event_id='should_not_be_touched'").fetchone()
assert row[0] == 0
print("PASS: retention disabled (both settings 0, the default) touches nothing, even for very old files")

# --- Test 7: heartbeat file is actually written ---
heartbeat_path = "test_retention_db/heartbeat_test.txt"
if os.path.exists(heartbeat_path):
    os.remove(heartbeat_path)
m.HEARTBEAT_FILE = heartbeat_path
m.write_heartbeat()
assert os.path.exists(heartbeat_path)
with open(heartbeat_path) as f:
    ts = float(f.read())
assert abs(ts - time.time()) < 5
print("PASS: heartbeat file is written with a current timestamp")

# cleanup
import shutil
for d in ["test_retention_db", "test_retention_tmp", "test_retention_logs"]:
    if os.path.exists(d):
        shutil.rmtree(d)

print("\nAll retention tests passed.")
