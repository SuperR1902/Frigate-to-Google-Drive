"""
Tests for the --check mode (run_checks / print_check_report) in main.py.
Uses mocked Frigate/Drive responses - no real network calls.
"""
import importlib
import io
import os
import sys
import time
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

_tmp = "test_check_tmp"
os.environ["DB_PATH"] = f"{_tmp}/db/events.db"
os.environ["TMP_DIR"] = f"{_tmp}/tmp"
os.environ["LOG_FILE"] = f"{_tmp}/logs/app.log"
os.environ["DRIVE_ROOT_FOLDER_ID"] = ""  # start unset; individual tests override
os.environ["TZ"] = "Europe/Amsterdam"
os.environ["FRIGATE_URL"] = "http://192.0.2.1:5000"  # TEST-NET-1, never resolves

import main as m


class FakeResponse:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json


def find(results, name):
    for r in results:
        if r["name"] == name:
            return r
    raise AssertionError(f"No check named {name!r} in results: {[r['name'] for r in results]}")


# --- Test 1: missing DRIVE_ROOT_FOLDER_ID is caught ---
m.DRIVE_ROOT_FOLDER_ID = ""
results = m.run_checks()
check = find(results, "DRIVE_ROOT_FOLDER_ID")
assert check["ok"] is False
print("PASS: missing DRIVE_ROOT_FOLDER_ID is correctly flagged as failing")

# --- Test 2: invalid timezone is caught ---
m.TZ_NAME = "Not/A_Real_Zone"
m.LOCAL_TZ = None
results = m.run_checks()
check = find(results, "Timezone")
assert check["ok"] is False
assert "Not/A_Real_Zone" in check["message"]
print("PASS: invalid timezone name is correctly flagged as failing")

# --- Test 3: valid, explicitly-set timezone passes cleanly ---
m.TZ_NAME = "Europe/Amsterdam"
from zoneinfo import ZoneInfo
m.LOCAL_TZ = ZoneInfo("Europe/Amsterdam")
results = m.run_checks()
check = find(results, "Timezone")
assert check["ok"] is True
assert "Europe/Amsterdam" in check["message"]
print("PASS: a valid, explicitly-set timezone passes")

# --- Test 4: default UTC timezone passes but with an advisory note ---
m.TZ_NAME = "UTC"
m.LOCAL_TZ = ZoneInfo("UTC")
results = m.run_checks()
check = find(results, "Timezone")
assert check["ok"] is True  # UTC is technically valid, just probably not what they want
assert "UTC" in check["message"] and "not your local time" in check["message"]
print("PASS: default UTC timezone passes but includes an advisory note")

m.TZ_NAME = "Europe/Amsterdam"
m.LOCAL_TZ = ZoneInfo("Europe/Amsterdam")

# --- Test 5: missing credentials file is caught, and Drive checks are
#             skipped (not attempted) rather than cascading into confusing
#             secondary errors ---
m.DRIVE_AUTH_MODE = "oauth_user"
m.OAUTH_TOKEN_FILE = f"{_tmp}/does_not_exist_token.json"
results = m.run_checks()
cred_check = find(results, "Credentials file")
assert cred_check["ok"] is False
auth_check = find(results, "Drive authentication")
assert auth_check["ok"] is False
assert "skipped" in auth_check["message"]
folder_check = find(results, "Drive folder access")
assert folder_check["ok"] is False
assert "skipped" in folder_check["message"]
print("PASS: missing credentials file is caught, and dependent Drive checks are cleanly skipped, not cascaded")

# --- Test 6: Frigate connection refused gives the specific port hint ---
def raise_conn_error(*a, **kw):
    raise m.requests.exceptions.ConnectionError("refused")

with mock.patch.object(m.requests, "get", side_effect=raise_conn_error):
    results = m.run_checks()
check = find(results, "Frigate connection")
assert check["ok"] is False
assert "8971" in check["message"] and "5000" in check["message"]
print("PASS: Frigate connection-refused gives the specific 8971-vs-5000 hint")

# --- Test 7: Frigate reachable passes ---
def fake_get_frigate_ok(url, params=None, timeout=None, **kw):
    return FakeResponse(200, [])

with mock.patch.object(m.requests, "get", side_effect=fake_get_frigate_ok):
    results = m.run_checks()
check = find(results, "Frigate connection")
assert check["ok"] is True
print("PASS: a reachable Frigate instance passes cleanly")

# --- Test 8: full happy path - credentials exist, Drive auth works, folder
#             is accessible, Frigate reachable ---
os.makedirs(_tmp, exist_ok=True)
fake_token_path = f"{_tmp}/fake_token.json"
with open(fake_token_path, "w") as f:
    f.write("{}")
m.OAUTH_TOKEN_FILE = fake_token_path
m.DRIVE_ROOT_FOLDER_ID = "fake_folder_id"

class FakeAboutRequest:
    def execute(self):
        return {"user": {"emailAddress": "test@example.com"}}


class FakeAboutResource:
    def get(self, fields=None):
        return FakeAboutRequest()


class FakeFilesGetRequest:
    def execute(self):
        return {"id": "fake_folder_id", "name": "MyBackupFolder", "mimeType": "application/vnd.google-apps.folder"}

class FakeDriveService:
    def about(self):
        return FakeAboutResource()

    def files(self):
        class F:
            def get(self, fileId, fields, supportsAllDrives=True):
                return FakeFilesGetRequest()
        return F()

with mock.patch.object(m, "get_drive_service", return_value=FakeDriveService()), \
     mock.patch.object(m.requests, "get", side_effect=fake_get_frigate_ok):
    results = m.run_checks()

assert find(results, "DRIVE_ROOT_FOLDER_ID")["ok"] is True
assert find(results, "Timezone")["ok"] is True
assert find(results, "Credentials file")["ok"] is True
assert find(results, "Frigate connection")["ok"] is True
assert find(results, "Drive authentication")["ok"] is True
assert "test@example.com" in find(results, "Drive authentication")["message"]
assert find(results, "Drive folder access")["ok"] is True
assert "MyBackupFolder" in find(results, "Drive folder access")["message"]
print("PASS: full happy path - every check passes with correct detail messages")

# --- Test 9: folder ID pointing to a non-folder is caught ---
class FakeFilesGetRequestNotAFolder:
    def execute(self):
        return {"id": "fake_folder_id", "name": "not_a_folder.txt", "mimeType": "text/plain"}

class FakeDriveServiceNotAFolder(FakeDriveService):
    def files(self):
        class F:
            def get(self, fileId, fields, supportsAllDrives=True):
                return FakeFilesGetRequestNotAFolder()
        return F()

with mock.patch.object(m, "get_drive_service", return_value=FakeDriveServiceNotAFolder()), \
     mock.patch.object(m.requests, "get", side_effect=fake_get_frigate_ok):
    results = m.run_checks()
check = find(results, "Drive folder access")
assert check["ok"] is False
assert "not a folder" in check["message"]
print("PASS: DRIVE_ROOT_FOLDER_ID pointing to a non-folder is caught with a clear message")

# --- Test 10: Drive authentication failure is caught and folder check is
#              skipped rather than attempted with broken credentials ---
class FakeAboutRequestFails:
    def execute(self):
        raise Exception("invalid_grant: Token has been expired or revoked")


class FakeAboutResourceFails:
    def get(self, fields=None):
        return FakeAboutRequestFails()


class FakeDriveServiceAuthFails:
    def about(self):
        return FakeAboutResourceFails()

with mock.patch.object(m, "get_drive_service", return_value=FakeDriveServiceAuthFails()), \
     mock.patch.object(m.requests, "get", side_effect=fake_get_frigate_ok):
    results = m.run_checks()
auth_check = find(results, "Drive authentication")
assert auth_check["ok"] is False
assert "expired" in auth_check["message"] or "invalid_grant" in auth_check["message"]
folder_check = find(results, "Drive folder access")
assert folder_check["ok"] is False
assert "skipped" in folder_check["message"]
print("PASS: Drive auth failure is caught, and folder check is skipped rather than attempted with broken creds")

# --- Test 11: print_check_report formats correctly and returns overall status ---
buf = io.StringIO()
with redirect_stdout(buf):
    all_ok = m.print_check_report([
        {"name": "Thing A", "ok": True, "message": "fine"},
        {"name": "Thing B", "ok": False, "message": "broken"},
    ])
output = buf.getvalue()
assert all_ok is False
assert "Thing A" in output and "Thing B" in output
assert "\u2713" in output  # checkmark for passing
assert "\u2717" in output  # cross for failing
assert "Some checks failed" in output
print("PASS: print_check_report formats output correctly and returns False when any check fails")

buf2 = io.StringIO()
with redirect_stdout(buf2):
    all_ok2 = m.print_check_report([{"name": "Thing A", "ok": True, "message": "fine"}])
assert all_ok2 is True
assert "All checks passed" in buf2.getvalue()
print("PASS: print_check_report returns True and reports success when everything passes")

# cleanup
import shutil
if os.path.exists(_tmp):
    shutil.rmtree(_tmp)

print("\nAll --check mode tests passed.")
