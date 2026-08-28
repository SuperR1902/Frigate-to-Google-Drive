#!/usr/bin/env python3
"""
Frigate -> Google Drive uploader: setup wizard.

Run this ON YOUR OWN COMPUTER (the one with a web browser) - NOT inside
the Proxmox LXC container. It walks you through Google OAuth setup with
a simple local web page, collects and validates the rest of your config
(Frigate URL, timezone, Drive folder), and writes out both
`oauth_token.json` and a ready-to-use `.env` file.

Usage:
    pip install -r requirements-wizard.txt
    python3 oauth_setup_wizard.py

Then open http://localhost:5000 in your browser.

What it checks before declaring success:
    - The OAuth token actually works (a real Drive API call)
    - The Drive folder ID (if given) exists and is actually a folder
    - Frigate is reachable at the given URL (catches the common
      "only port 8971 is published, not 5000" mistake directly)
    - The timezone name is a real IANA timezone

Why this is simpler than the OAuth Playground method:
    OAuth Playground requires a "Web application" OAuth client with an
    exact redirect URI registered in Google Cloud Console (a common
    source of "redirect_uri_mismatch" errors). This wizard uses a real
    http://localhost redirect, which "Desktop app" OAuth clients support
    automatically - no redirect URI configuration needed in Google Cloud
    Console at all.
"""
import json
import os
import secrets
import sys
import webbrowser
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from flask import Flask, redirect, request, session, url_for

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

PORT = int(os.environ.get("WIZARD_PORT", "5000"))
REDIRECT_URI = f"http://localhost:{PORT}/callback"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"

OUTPUT_PATH = os.environ.get("OAUTH_TOKEN_OUTPUT", "oauth_token.json")
ENV_OUTPUT_PATH = os.environ.get("ENV_OUTPUT", ".env")
ENV_EXAMPLE_PATH = os.environ.get("ENV_EXAMPLE", ".env.example")

PAGE_STYLE = """
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 640px;
         margin: 60px auto; padding: 0 20px; color: #1a1a1a; line-height: 1.5; }
  h1 { font-size: 1.4em; }
  input[type=text] { width: 100%; padding: 8px; margin: 6px 0 16px; box-sizing: border-box;
                      border: 1px solid #ccc; border-radius: 4px; font-size: 14px; }
  label { font-weight: 600; font-size: 14px; }
  button, .btn { background: #1a73e8; color: white; border: none; padding: 10px 20px;
        border-radius: 4px; font-size: 15px; cursor: pointer; text-decoration: none;
        display: inline-block; }
  button:hover, .btn:hover { background: #1558b0; }
  .hint { color: #666; font-size: 13px; margin-top: -10px; margin-bottom: 16px; }
  .check { padding: 10px 14px; border-radius: 4px; margin: 8px 0; font-size: 14px; }
  .pass { background: #e6f4ea; color: #137333; }
  .warn { background: #fef7e0; color: #b06000; }
  .fail { background: #fce8e6; color: #c5221f; }
  code { background: #f1f3f4; padding: 2px 6px; border-radius: 3px; font-size: 13px; }
  pre { background: #f1f3f4; padding: 12px; border-radius: 4px; overflow-x: auto; font-size: 13px; }
</style>
"""


@app.route("/")
def index():
    return f"""
<!doctype html>
<html><head><title>Frigate -> Drive: Setup Wizard</title>{PAGE_STYLE}</head>
<body>
<h1>Frigate &rarr; Google Drive: Setup Wizard</h1>
<p>First, create a <b>Desktop app</b> OAuth client in the
<a href="https://console.cloud.google.com/apis/credentials" target="_blank">Google Cloud Console</a>
(APIs &amp; Services &rarr; Credentials &rarr; Create Credentials &rarr; OAuth client ID
&rarr; Application type: <b>Desktop app</b>). No redirect URI setup needed for this type.</p>

<form action="/start" method="post">
  <label>Frigate URL</label>
  <input type="text" name="frigate_url" placeholder="http://192.168.1.10:5000">
  <div class="hint">Include the port. Many Frigate setups only expose port 8971
  (authenticated) by default &mdash; this needs port 5000 (plain API) published too.
  Leave blank to skip and set it manually later.</div>

  <label>Timezone</label>
  <input type="text" id="tz" name="timezone" placeholder="Europe/Amsterdam">
  <div class="hint">Auto-detected from your browser below &mdash; change it if it's wrong.
  Without this, filenames/folders default to UTC time, not your local time.</div>

  <label>Client ID</label>
  <input type="text" name="client_id" required placeholder="123456-abc.apps.googleusercontent.com">

  <label>Client Secret</label>
  <input type="text" name="client_secret" required placeholder="GOCSPX-...">

  <label>Drive Folder ID</label>
  <input type="text" name="folder_id" placeholder="1AbCdEfGhIjKlMnOpQrSt">
  <div class="hint">The folder ID from your Drive folder's URL. Leave blank to skip
  validation and set it manually later.</div>

  <button type="submit">Authorize with Google &rarr;</button>
</form>

<script>
  document.getElementById('tz').value = Intl.DateTimeFormat().resolvedOptions().timeZone;
</script>
</body></html>
"""


@app.route("/start", methods=["POST"])
def start():
    client_id = request.form["client_id"].strip()
    client_secret = request.form["client_secret"].strip()
    folder_id = request.form.get("folder_id", "").strip()
    frigate_url = request.form.get("frigate_url", "").strip().rstrip("/")
    timezone_name = request.form.get("timezone", "").strip()

    if not client_id or not client_secret:
        return "Client ID and Client Secret are required. <a href='/'>Go back</a>", 400

    state = secrets.token_urlsafe(24)
    session["state"] = state
    session["client_id"] = client_id
    session["client_secret"] = client_secret
    session["folder_id"] = folder_id
    session["frigate_url"] = frigate_url
    session["timezone"] = timezone_name

    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": DRIVE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",  # ensures a refresh_token is returned even on re-auth
        "state": state,
    }
    auth_url = GOOGLE_AUTH_URL + "?" + "&".join(f"{k}={requests.utils.quote(v)}" for k, v in params.items())
    return redirect(auth_url)


@app.route("/callback")
def callback():
    error = request.args.get("error")
    if error:
        return render_result(ok=False, message=f"Google returned an error: {error}")

    if request.args.get("state") != session.get("state"):
        return render_result(ok=False, message="State mismatch - possible CSRF, or you opened a stale link. Start over from /"), 400

    code = request.args.get("code")
    client_id = session.get("client_id")
    client_secret = session.get("client_secret")
    folder_id = session.get("folder_id")
    frigate_url = session.get("frigate_url", "")
    timezone_name = session.get("timezone", "")

    if not code or not client_id or not client_secret:
        return render_result(ok=False, message="Missing code or session data. Start over from /"), 400

    token_resp = requests.post(GOOGLE_TOKEN_URL, data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=30)

    if token_resp.status_code != 200:
        return render_result(ok=False, message=f"Token exchange failed ({token_resp.status_code}): {token_resp.text}")

    token_data = token_resp.json()
    refresh_token = token_data.get("refresh_token")
    access_token = token_data.get("access_token")

    if not refresh_token:
        return render_result(ok=False, message=(
            "Google didn't return a refresh_token. This usually happens if you've already "
            "authorized this app before without revoking access. Go to "
            "<a href='https://myaccount.google.com/permissions' target='_blank'>Google Account "
            "permissions</a>, remove access for this app, then start over from /."
        ))

    checks = run_checks(access_token, folder_id, frigate_url, timezone_name)

    token_output = {
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "scopes": [DRIVE_SCOPE],
    }
    saved_path = None
    save_error = None
    try:
        with open(OUTPUT_PATH, "w") as f:
            json.dump(token_output, f, indent=2)
        saved_path = os.path.abspath(OUTPUT_PATH)
    except Exception as e:
        save_error = str(e)

    env_path, env_error = write_env_file(frigate_url, timezone_name, folder_id)

    return render_result(
        ok=all(c["ok"] for c in checks) and saved_path is not None,
        message=None,
        checks=checks,
        saved_path=saved_path,
        save_error=save_error,
        token_json=json.dumps(token_output, indent=2),
        env_path=env_path,
        env_error=env_error,
    )


def write_env_file(frigate_url, timezone_name, folder_id):
    """Write a complete .env file, using .env.example as a base template
    (to stay in sync with all its other defaults) and overriding the
    values collected in this wizard. Falls back to a minimal standalone
    .env if .env.example isn't found alongside this script."""
    overrides = {
        "FRIGATE_URL": frigate_url,
        "TZ": timezone_name,
        "DRIVE_ROOT_FOLDER_ID": folder_id,
        "DRIVE_AUTH_MODE": "oauth_user",
        "OAUTH_TOKEN_FILE": "credentials/oauth_token.json",
    }
    # Don't overwrite settings the user left blank in the wizard - keep
    # whatever the template already has for those instead.
    overrides = {k: v for k, v in overrides.items() if v}

    lines = []
    seen_keys = set()
    if os.path.exists(ENV_EXAMPLE_PATH):
        with open(ENV_EXAMPLE_PATH) as f:
            for line in f:
                stripped = line.rstrip("\n")
                if "=" in stripped and not stripped.strip().startswith("#"):
                    key = stripped.split("=", 1)[0]
                    if key in overrides:
                        lines.append(f"{key}={overrides[key]}")
                        seen_keys.add(key)
                        continue
                lines.append(stripped)
    # Add any override keys that weren't already present in the template
    for key, value in overrides.items():
        if key not in seen_keys:
            lines.append(f"{key}={value}")

    try:
        with open(ENV_OUTPUT_PATH, "w") as f:
            f.write("\n".join(lines) + "\n")
        return os.path.abspath(ENV_OUTPUT_PATH), None
    except Exception as e:
        return None, str(e)


def run_checks(access_token, folder_id, frigate_url="", timezone_name=""):
    """Validate the token actually works, and optionally that the given
    folder ID, Frigate URL, and timezone are all valid/reachable."""
    checks = []
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        resp = requests.get(f"{DRIVE_API_BASE}/about", params={"fields": "user"}, headers=headers, timeout=15)
        if resp.status_code == 200:
            email = resp.json().get("user", {}).get("emailAddress", "unknown")
            checks.append({"ok": True, "label": f"Drive API access confirmed (as {email})"})
        else:
            checks.append({"ok": False, "label": f"Drive API test call failed: {resp.status_code} {resp.text[:200]}"})
    except Exception as e:
        checks.append({"ok": False, "label": f"Drive API test call raised an exception: {e}"})

    if folder_id:
        try:
            resp = requests.get(f"{DRIVE_API_BASE}/files/{folder_id}", params={"fields": "id,name,mimeType"}, headers=headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("mimeType") == "application/vnd.google-apps.folder":
                    checks.append({"ok": True, "label": f"Folder ID is valid and accessible: '{data.get('name')}'"})
                else:
                    checks.append({"ok": False, "label": f"That ID exists but isn't a folder (mimeType: {data.get('mimeType')})"})
            elif resp.status_code == 404:
                checks.append({"ok": False, "label": "Folder ID not found or not accessible with this account"})
            else:
                checks.append({"ok": False, "label": f"Folder check failed: {resp.status_code} {resp.text[:200]}"})
        except Exception as e:
            checks.append({"ok": False, "label": f"Folder check raised an exception: {e}"})
    else:
        checks.append({"ok": True, "warn": True, "label": "No folder ID provided - skipped folder validation"})

    if frigate_url:
        try:
            resp = requests.get(f"{frigate_url}/api/events", timeout=10)
            if resp.status_code == 200:
                try:
                    n = len(resp.json())
                    checks.append({"ok": True, "label": f"Frigate is reachable at {frigate_url} ({n} events found)"})
                except ValueError:
                    checks.append({"ok": False, "label": f"Frigate responded but didn't return valid JSON - check the URL"})
            else:
                checks.append({"ok": False, "label": f"Frigate responded with {resp.status_code} - check the URL/port"})
        except requests.exceptions.ConnectionError:
            checks.append({"ok": False, "label": (
                f"Could not connect to {frigate_url} - Connection refused usually means the port "
                "isn't published. Many Frigate setups only expose 8971 (authenticated) by default; "
                "this needs port 5000 (plain API) published too."
            )})
        except Exception as e:
            checks.append({"ok": False, "label": f"Frigate check raised an exception: {e}"})
    else:
        checks.append({"ok": True, "warn": True, "label": "No Frigate URL provided - skipped reachability check"})

    if timezone_name:
        try:
            ZoneInfo(timezone_name)
            checks.append({"ok": True, "label": f"Timezone '{timezone_name}' is valid"})
        except ZoneInfoNotFoundError:
            checks.append({"ok": False, "label": f"'{timezone_name}' is not a recognized IANA timezone name"})
    else:
        checks.append({"ok": True, "warn": True, "label": "No timezone provided - filenames will default to UTC, likely wrong for you"})

    return checks


def render_result(ok, message=None, checks=None, saved_path=None, save_error=None, token_json=None, env_path=None, env_error=None):
    checks_html = ""
    if checks:
        for c in checks:
            if not c["ok"]:
                cls, symbol = "fail", "\u2717"
            elif c.get("warn"):
                cls, symbol = "warn", "\u26a0"
            else:
                cls, symbol = "pass", "\u2713"
            checks_html += f'<div class="check {cls}">{symbol} {c["label"]}</div>'

    body = ""
    if message:
        body += f'<div class="check fail">{message}</div>'
    body += checks_html

    if saved_path:
        body += f'<p>Token file saved to:<br><code>{saved_path}</code></p>'
    if env_path:
        body += f'<p>Config file saved to:<br><code>{env_path}</code></p>'
    if saved_path or env_path:
        body += "<p>Next step: copy both files to your Proxmox container:</p>"
        push_cmds = ""
        if saved_path:
            push_cmds += f'pct push &lt;CTID&gt; "{saved_path}" /opt/frigate-gdrive-uploader/credentials/oauth_token.json\n'
        if env_path:
            push_cmds += f'pct push &lt;CTID&gt; "{env_path}" /opt/frigate-gdrive-uploader/.env\n'
        push_cmds += "pct exec &lt;CTID&gt; -- chown -R frigate-uploader:frigate-uploader /opt/frigate-gdrive-uploader/credentials /opt/frigate-gdrive-uploader/.env\npct exec &lt;CTID&gt; -- systemctl restart frigate-gdrive-uploader"
        body += f'<pre>{push_cmds}</pre>'
    if save_error:
        body += f'<div class="check fail">Could not write token file: {save_error}</div>'
        if token_json:
            body += "<p>Copy this manually into <code>credentials/oauth_token.json</code>:</p>"
            body += f"<pre>{token_json}</pre>"
    if env_error:
        body += f'<div class="check fail">Could not write .env file: {env_error}</div>'

    heading = "Setup complete" if ok else "Setup incomplete"
    return f"""
<!doctype html>
<html><head><title>{heading}</title>{PAGE_STYLE}</head>
<body>
<h1>{heading}</h1>
{body}
<p><a href="/">&larr; Start over</a></p>
</body></html>
"""


if __name__ == "__main__":
    url = f"http://localhost:{PORT}"
    print(f"\nOAuth setup wizard running at {url}")
    print("Opening your browser...\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    app.run(host="127.0.0.1", port=PORT, debug=False)
