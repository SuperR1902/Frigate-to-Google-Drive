"""
Tests for oauth_setup_wizard.py - exercises the full flow using Flask's
test client and mocked HTTP calls, without touching real Google servers
or a real Frigate instance.
"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

_tmp_dir = tempfile.mkdtemp()
os.environ["OAUTH_TOKEN_OUTPUT"] = os.path.join(_tmp_dir, "oauth_token.json")
os.environ["ENV_OUTPUT"] = os.path.join(_tmp_dir, ".env")
os.environ["ENV_EXAMPLE"] = os.path.join(_tmp_dir, ".env.example")

# A realistic .env.example so we can verify the wizard preserves unrelated
# defaults and only overrides the specific keys it collected.
with open(os.environ["ENV_EXAMPLE"], "w") as f:
    f.write(
        "FRIGATE_URL=http://192.168.1.10:5000\n"
        "ONLY_CAMERAS=\n"
        "TZ=UTC\n"
        "DRIVE_AUTH_MODE=service_account\n"
        "SERVICE_ACCOUNT_FILE=credentials/service_account.json\n"
        "OAUTH_TOKEN_FILE=credentials/oauth_token.json\n"
        "DRIVE_ROOT_FOLDER_ID=\n"
        "DB_PATH=db/events.db\n"
        "MAX_RETRY_ATTEMPTS=20\n"
    )

import oauth_setup_wizard as wiz

wiz.app.testing = True
client = wiz.app.test_client()


class FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text or (json.dumps(json_data) if json_data is not None else "")

    def json(self):
        if self._json is None:
            raise ValueError("no JSON body")
        return self._json


# --- Test 1: index page loads with all expected fields ---
resp = client.get("/")
assert resp.status_code == 200
for field in ["Client ID", "Client Secret", "Frigate URL", "Timezone", "Drive Folder ID"]:
    assert field.encode() in resp.data, f"Missing field: {field}"
assert b"Intl.DateTimeFormat" in resp.data, "Timezone auto-detect JS should be present"
print("PASS: index page loads with all expected fields, including timezone auto-detect JS")

# --- Test 2: /start redirects to Google with correct params and stores new fields ---
resp = client.post("/start", data={
    "client_id": "test-client-id.apps.googleusercontent.com",
    "client_secret": "test-secret-GOCSPX",
    "folder_id": "test-folder-123",
    "frigate_url": "http://192.168.68.144:5000/",  # trailing slash should be stripped
    "timezone": "Europe/Amsterdam",
})
assert resp.status_code == 302
location = resp.headers["Location"]
assert "accounts.google.com" in location
assert "access_type=offline" in location
assert "prompt=consent" in location

with client.session_transaction() as sess:
    assert sess["frigate_url"] == "http://192.168.68.144:5000", "trailing slash should be stripped"
    assert sess["timezone"] == "Europe/Amsterdam"
print("PASS: /start builds a correct Google auth URL and stores Frigate URL (trailing slash stripped) + timezone")

# --- Test 3: missing required fields is rejected ---
resp = client.post("/start", data={"client_id": "", "client_secret": ""})
assert resp.status_code == 400
print("PASS: missing client_id/client_secret is rejected with 400")


def fake_post(url, data=None, timeout=None, **kwargs):
    assert url == wiz.GOOGLE_TOKEN_URL
    assert data["grant_type"] == "authorization_code"
    return FakeResponse(200, {
        "access_token": "fake-access-token",
        "refresh_token": "fake-refresh-token-xyz",
        "expires_in": 3599,
    })


def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
    if url.endswith("/about"):
        assert headers["Authorization"] == "Bearer fake-access-token"
        return FakeResponse(200, {"user": {"emailAddress": "test@example.com"}})
    elif "/files/" in url:
        return FakeResponse(200, {"id": "test-folder-123", "name": "MyFolder", "mimeType": "application/vnd.google-apps.folder"})
    elif "/api/events" in url:
        return FakeResponse(200, [{"id": "evt1"}, {"id": "evt2"}])  # simulated Frigate response
    raise AssertionError(f"Unexpected GET to {url}")


# --- Test 4: full callback flow, including Frigate + timezone checks ---
with client.session_transaction() as sess:
    sess["state"] = "test-state-123"
    sess["client_id"] = "test-client-id"
    sess["client_secret"] = "test-secret"
    sess["folder_id"] = "test-folder-123"
    sess["frigate_url"] = "http://192.168.68.144:5000"
    sess["timezone"] = "Europe/Amsterdam"

with mock.patch.object(wiz.requests, "post", side_effect=fake_post), \
     mock.patch.object(wiz.requests, "get", side_effect=fake_get):
    resp = client.get("/callback?code=fake-auth-code&state=test-state-123")

assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.data[:800]}"
assert b"Drive API access confirmed" in resp.data
assert b"Folder ID is valid" in resp.data
assert b"Frigate is reachable" in resp.data and b"2 events found" in resp.data
assert b"Europe/Amsterdam" in resp.data and b"is valid" in resp.data
assert b"Setup complete" in resp.data
print("PASS: full callback flow checks Drive, folder, Frigate reachability, and timezone all correctly")

# --- Test 5: the saved token file matches exactly what main.py expects ---
with open(os.environ["OAUTH_TOKEN_OUTPUT"]) as f:
    saved = json.load(f)
assert saved["refresh_token"] == "fake-refresh-token-xyz"
assert saved["client_id"] == "test-client-id"
assert saved["client_secret"] == "test-secret"
assert saved["scopes"] == ["https://www.googleapis.com/auth/drive"]
print("PASS: saved oauth_token.json has exactly the fields/format main.py's get_drive_service() expects")

# --- Test 6: the generated .env correctly overrides collected fields while
#             preserving everything else from .env.example untouched ---
with open(os.environ["ENV_OUTPUT"]) as f:
    env_lines = dict(
        line.split("=", 1) for line in f.read().splitlines() if "=" in line and not line.startswith("#")
    )
assert env_lines["FRIGATE_URL"] == "http://192.168.68.144:5000", env_lines
assert env_lines["TZ"] == "Europe/Amsterdam", env_lines
assert env_lines["DRIVE_ROOT_FOLDER_ID"] == "test-folder-123", env_lines
assert env_lines["DRIVE_AUTH_MODE"] == "oauth_user", "wizard should force oauth_user mode, got: " + env_lines["DRIVE_AUTH_MODE"]
assert env_lines["OAUTH_TOKEN_FILE"] == "credentials/oauth_token.json", env_lines
# Untouched settings from the template should survive exactly as-is
assert env_lines["DB_PATH"] == "db/events.db", "unrelated settings should be preserved from .env.example"
assert env_lines["MAX_RETRY_ATTEMPTS"] == "20"
print("PASS: generated .env correctly overrides collected fields and preserves all unrelated template defaults")

# --- Test 7: state mismatch is rejected (CSRF protection) ---
with client.session_transaction() as sess:
    sess["state"] = "real-state"
    sess["client_id"] = "x"
    sess["client_secret"] = "y"
resp = client.get("/callback?code=abc&state=WRONG-state")
assert resp.status_code == 400
assert b"State mismatch" in resp.data
print("PASS: mismatched state parameter is rejected (CSRF protection working)")

# --- Test 8: Google returning an error param is handled gracefully ---
resp = client.get("/callback?error=access_denied")
assert resp.status_code == 200
assert b"Google returned an error" in resp.data
assert b"access_denied" in resp.data
print("PASS: Google error responses (e.g. user declined) are shown clearly, not a crash")

# --- Test 9: missing refresh_token (re-auth without consent) gives a clear explanation ---
with client.session_transaction() as sess:
    sess["state"] = "s2"
    sess["client_id"] = "x"
    sess["client_secret"] = "y"
    sess["folder_id"] = ""
    sess["frigate_url"] = ""
    sess["timezone"] = ""


def fake_post_no_refresh(url, data=None, timeout=None, **kwargs):
    return FakeResponse(200, {"access_token": "tok", "expires_in": 3599})


with mock.patch.object(wiz.requests, "post", side_effect=fake_post_no_refresh):
    resp = client.get("/callback?code=abc&state=s2")
assert resp.status_code == 200
assert b"didn't return a refresh_token" in resp.data
assert b"myaccount.google.com/permissions" in resp.data
print("PASS: missing refresh_token case gives an actionable explanation, not a silent failure")

# --- Test 10: Frigate connection-refused gives the specific 8971-vs-5000 hint ---
def fake_get_connrefused(url, params=None, headers=None, timeout=None, **kwargs):
    if "/api/events" in url:
        raise wiz.requests.exceptions.ConnectionError("Connection refused")
    if url.endswith("/about"):
        return FakeResponse(200, {"user": {"emailAddress": "t@example.com"}})
    return FakeResponse(200, {})


with client.session_transaction() as sess:
    sess["state"] = "s3"
    sess["client_id"] = "x"
    sess["client_secret"] = "y"
    sess["folder_id"] = ""
    sess["frigate_url"] = "http://10.0.0.5:5000"
    sess["timezone"] = ""

with mock.patch.object(wiz.requests, "post", side_effect=fake_post), \
     mock.patch.object(wiz.requests, "get", side_effect=fake_get_connrefused):
    resp = client.get("/callback?code=abc&state=s3")
assert resp.status_code == 200
assert b"8971" in resp.data and b"5000" in resp.data, "should surface the specific port hint"
print("PASS: Frigate connection-refused shows the specific port 8971-vs-5000 hint, not a generic error")

# --- Test 11: missing timezone/folder/frigate is a warning, not a hard failure ---
with client.session_transaction() as sess:
    sess["state"] = "s4"
    sess["client_id"] = "x"
    sess["client_secret"] = "y"
    sess["folder_id"] = ""
    sess["frigate_url"] = ""
    sess["timezone"] = ""

with mock.patch.object(wiz.requests, "post", side_effect=fake_post), \
     mock.patch.object(wiz.requests, "get", side_effect=lambda url, **kw: FakeResponse(200, {"user": {"emailAddress": "t@example.com"}})):
    resp = client.get("/callback?code=abc&state=s4")
assert resp.status_code == 200
assert b"Setup complete" in resp.data, "leaving optional fields blank should still be an overall success"
assert b"No folder ID provided" in resp.data
assert b"No Frigate URL provided" in resp.data
assert b"No timezone provided" in resp.data
print("PASS: leaving Frigate URL/timezone/folder blank is treated as a skippable warning, not a blocking failure")

print("\nAll oauth_setup_wizard tests passed.")
