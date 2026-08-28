# Frigate → Google Drive Uploader

Watches your [Frigate](https://frigate.video) NVR for new finished clips
and uploads them to Google Drive automatically, organized into
`YYYY/MM/DD` folders. Runs continuously as a background service and
survives restarts without losing track of what's already been uploaded.

This package is built from real-world trial and error — every gotcha
below was an actual failure encountered and fixed during setup, not a
theoretical edge case. Read the **"Before you start"** section; it will
save you the most common wrong turns. `CHANGELOG.md` has the full story
behind each fix, including exact error messages and root causes, in case
you hit something similar and want to understand why the code does what
it does.

---

## Before you start: 3 things that trip almost everyone up

### 1. Which Google account do you have?

This determines everything about how you configure Drive access.

| Account type | What to use | Why |
|---|---|---|
| Personal **@gmail.com** | `DRIVE_AUTH_MODE=oauth_user` | Service accounts have **zero storage quota of their own**. Sharing a folder with one does *not* let it use your quota — that only works inside a Shared Drive, which requires Workspace. |
| **Google Workspace** (paid business/school, custom domain) | `DRIVE_AUTH_MODE=service_account` | You can create a Shared Drive and grant the service account access to it. |

Guessing wrong here doesn't fail quietly — it fails with a
`storageQuotaExceeded` 403 error *after* everything else appears to be
working (Frigate connection fine, downloads fine, upload request sent,
then rejected). See the full setup for both paths in **Section 2**.

### 2. Is Frigate's unauthenticated API port actually reachable?

Many current Frigate Docker/Portainer setups only publish port **8971**
(the authenticated UI) and never expose port **5000** (the plain HTTP API
this tool uses) to the LAN. If port 5000 isn't published, you'll get
`Connection refused` — not a timeout — when this tool tries to reach
Frigate.

**Check this first, before anything else:**
```bash
curl -v http://<your-frigate-ip>:5000/api/events
```
You want real JSON back (even `[]` is fine). If you get "Connection
refused," go to your Frigate's Docker/Portainer config and make sure port
5000 is published (`- "5000:5000"` in `docker-compose.yml`, or add a port
mapping in Portainer's container settings), then retry.

### 3. Test scripts assume Frigate's API is stable — it might not exactly match yours

This tool sends `has_clip=1` (an integer) and `after=<integer epoch>` to
Frigate's `/api/events` endpoint. These are correct for recent Frigate
versions (confirmed working against 0.17.x), but Frigate's API has
changed parameter types before. **If you ever see a `422 Unprocessable
Entity` error**, the fastest diagnosis is to curl the *exact* failing URL
directly and read the JSON body — FastAPI (which Frigate uses) always
returns a `detail` array naming the exact parameter and why:
```bash
curl -s "http://<frigate-ip>:5000/api/events?after=1700000000&has_clip=1&limit=200"
```
If that returns real data, the bug (if any) is elsewhere. If it returns
a `detail` block, it'll tell you exactly which parameter and type it
wants — update `fetch_events()` in `main.py` accordingly.

---

## How it works

Every `POLL_INTERVAL_SECONDS` (default 30s), it asks Frigate for events
newer than the last check. Finished events (ones with a completed clip)
get queued in a local SQLite database. A second pass downloads and
uploads queued clips, retrying failures automatically (capped at
`MAX_RETRY_ATTEMPTS`) and skipping events Frigate no longer has (404s).

Tested end-to-end: `test_logic.py` covers dedupe, retry limits, date-based
folder resolution, and the OAuth token refresh/persistence cycle. Run it
yourself with `python3 test_logic.py`.

---

## 1. Prerequisites

- A Proxmox VE host (8.x), **or** any Debian/Ubuntu machine with Python 3.
- Frigate reachable over the network with port 5000 open (see above).
- A Google account (personal or Workspace) — see Section 2 for which.

## 2. Set up Google Drive access

### Option A: Personal Gmail account → OAuth user credentials

**Step 1: Create a Drive folder**

Create (or pick) any folder in your own Drive — no sharing step needed,
since you're uploading as yourself. Open it and copy the ID from the URL:
`https://drive.google.com/drive/folders/`**`THIS_PART_IS_THE_ID`**

**Step 2: Create an OAuth Client ID**
1. Go to the [Google Cloud Console](https://console.cloud.google.com/),
   create or select a project.
2. Enable the **Google Drive API** (APIs & Services → Library → search
   "Google Drive API" → Enable).
3. Go to **APIs & Services → Credentials → Create Credentials → OAuth
   client ID**.
4. If prompted to configure the consent screen first, choose **External**,
   fill in an app name and your email. You can leave it in "Testing" mode
   — no Google review needed for personal use.
5. For **Application type**, choose **Desktop app**. Give it any name.
   That's it — no redirect URI configuration needed for this type, since
   the wizard below uses a real `localhost` redirect, which Desktop app
   clients support automatically.
6. Click **Create**. Copy the **Client ID** and **Client secret** shown.

**Step 3: Run the setup wizard (recommended — covers everything below too)**

Run this **on your own computer** (the one with a web browser) — not
inside the Proxmox container:
```bash
pip install -r requirements-wizard.txt
python3 oauth_setup_wizard.py
```
This opens `http://localhost:5000` in your browser with a single form
covering **all** the settings this project needs, not just OAuth:
- **Frigate URL** — checked for real reachability once you authorize
  (catches the common "only port 8971 is published, not 5000" mistake
  directly, with that exact hint if it happens)
- **Timezone** — auto-filled from your browser's own timezone, validated
  against real IANA data
- **Drive Folder ID** — from Step 1 above
- **Client ID / Client Secret** — from Step 2 above

Click **Authorize with Google**, sign in, and approve access. The wizard
then automatically:
- exchanges the authorization code for a refresh token,
- runs all four checks above and shows exactly what passed/failed,
- writes **both** `oauth_token.json` and a complete, ready-to-use `.env`
  file to the current directory, and
- prints the exact `pct push` commands to get both files into your
  container.

Leaving Frigate URL, timezone, or folder ID blank just skips that
specific check and setting — you can fill them in manually in `.env`
later. Only Client ID/Secret are required to run the wizard at all.

If a check fails, the page tells you specifically what went wrong (e.g.
"Folder ID not found or not accessible with this account") rather than
a generic error — fix that and start over from `/`.

**Step 3, alternative: OAuth Playground (manual method)**

If you'd rather not run a local script, or the wizard doesn't work in
your environment, you can get the refresh token manually instead — this
requires re-creating the OAuth Client as "Web application" type with a
specific redirect URI, which is a more error-prone process (this is what
caused the `redirect_uri_mismatch` error some users hit — see
Troubleshooting):

1. Create a **second** OAuth Client, this time as **Web application**
   type, with `https://developers.google.com/oauthplayground` added
   under **Authorized redirect URIs** (must match exactly, no trailing
   slash).
2. Go to [Google OAuth Playground](https://developers.google.com/oauthplayground).
3. Gear icon (top right) → check **"Use your own OAuth credentials"** →
   paste in that Web application Client ID and secret.
4. In the left panel's scope box, enter
   `https://www.googleapis.com/auth/drive` → **Authorize APIs** → sign in
   → Allow.
5. Click **Exchange authorization code for tokens** → copy the
   **Refresh token** shown.
6. Build `credentials/oauth_token.json` yourself:
   ```json
   {
     "refresh_token": "PASTE_YOUR_REFRESH_TOKEN_HERE",
     "client_id": "PASTE_YOUR_WEB_APP_CLIENT_ID_HERE",
     "client_secret": "PASTE_YOUR_WEB_APP_CLIENT_SECRET_HERE",
     "scopes": ["https://www.googleapis.com/auth/drive"]
   }
   ```

**Step 4: Configure `.env`** (skip this if you used the wizard in Step 3
— it already wrote a complete `.env` for you)
```
DRIVE_AUTH_MODE=oauth_user
OAUTH_TOKEN_FILE=credentials/oauth_token.json
DRIVE_ROOT_FOLDER_ID=<the folder ID from Step 1>
```

### Option B: Google Workspace account → Service account + Shared Drive

1. Go to the [Google Cloud Console](https://console.cloud.google.com/),
   create or select a project.
2. Enable the **Google Drive API** (APIs & Services → Library → search
   "Google Drive API" → Enable).
3. Go to **APIs & Services → Credentials → Create Credentials → Service
   Account**. Give it any name (e.g. `frigate-uploader`).
4. Open the new service account → **Keys** tab → **Add Key → Create new
   key → JSON**. Save it as `credentials/service_account.json`.
5. Note the service account's email address (ends in
   `.iam.gserviceaccount.com`).
6. In Google Drive, create a **Shared Drive** (not a regular folder — the
   "New → Shared Drive" option; only appears on Workspace accounts).
7. Add the service account's email as a **Content Manager** on that
   Shared Drive.
8. Copy the Shared Drive's ID from its URL the same way as a folder ID.

In `.env`, set:
```
DRIVE_AUTH_MODE=service_account
SERVICE_ACCOUNT_FILE=credentials/service_account.json
DRIVE_ROOT_FOLDER_ID=<the shared drive ID>
```

## 3. Install as a Proxmox LXC container

`create-frigate-gdrive-ct.sh` automates the whole container build: it
downloads a Debian 13 template if needed, creates an unprivileged LXC
container, installs Python, pulls in the app files, sets up a venv, and
registers (but doesn't start) the systemd service.

**Note on Debian 13:** the script sets `--features nesting=1` on the
container, which is required for systemd to behave correctly in an
unprivileged Debian 13 container. This is already handled for you.

### Fully automated: install straight from GitHub

If you've pushed this code to your own repo, the Proxmox host doesn't
need any local files at all — one command does everything:

```bash
curl -fsSL https://raw.githubusercontent.com/SuperR1902/Frigate-to-Google-Drive/main/create-frigate-gdrive-ct.sh \
  | GIT_REPO_URL=https://github.com/SuperR1902/Frigate-to-Google-Drive.git bash
```

The script fetches itself from GitHub via `curl`, then has the
*container* clone the app files from the same repo. Nothing is copied
from your PC to the Proxmox host manually.

Pin a branch/tag with `GIT_REF` (default `main`). Override any other
setting with env vars, e.g.:
```bash
curl -fsSL https://raw.githubusercontent.com/SuperR1902/Frigate-to-Google-Drive/main/create-frigate-gdrive-ct.sh \
  | CTID=211 MEMORY_MB=1024 GIT_REPO_URL=https://github.com/SuperR1902/Frigate-to-Google-Drive.git bash
```

| Variable | Default | Description |
|---|---|---|
| `CTID` | `210` | Container ID |
| `HOSTNAME_CT` | `frigate-gdrive` | Container hostname |
| `STORAGE` | `local-lvm` | Storage for the container's rootfs |
| `TEMPLATE_STORAGE` | `local` | Where CT templates are cached |
| `TEMPLATE_OS` | `debian-13-standard` | OS template to use |
| `DISK_SIZE_GB` | `4` | Root disk size |
| `MEMORY_MB` / `SWAP_MB` | `512` / `512` | RAM / swap |
| `CORES` | `1` | vCPU count |
| `BRIDGE` | `vmbr0` | Network bridge |
| `IP_CONFIG` | `dhcp` | Or e.g. `10.0.0.50/24,gw=10.0.0.1` for static |
| `GIT_REPO_URL` | *(empty)* | Your repo URL — required for the curl\|bash flow |
| `GIT_REF` | `main` | Branch/tag to clone |
| `STARTUP_ORDER` | `order=2` | Proxmox boot order/delay for this container - see "Reboot resilience" below |

**Note:** if you pipe the script without setting `GIT_REPO_URL`, it fails
immediately with a clear error rather than silently trying (and failing)
to find local files — there's nothing "alongside" a script streamed over
stdin.

### Alternative: clone the repo first, run locally

```bash
git clone https://github.com/SuperR1902/Frigate-to-Google-Drive.git
cd Frigate-to-Google-Drive
chmod +x create-frigate-gdrive-ct.sh
GIT_REPO_URL=https://github.com/SuperR1902/Frigate-to-Google-Drive.git ./create-frigate-gdrive-ct.sh
```

### Alternative: push local files instead of using git

If you're not using GitHub at all, just don't set `GIT_REPO_URL` — the
script falls back to pushing `main.py`, `requirements.txt`, etc. from its
own folder into the container. Copy this whole folder to the Proxmox host
first and run the script from inside it.

## 4. Finish setup

The script prints the container's IP and ends without starting the
service (it can't run yet without your Frigate URL and Drive
credentials). Finish it:

**If you used `oauth_setup_wizard.py`** (Section "Set up Google Drive
access," Option A, Step 3), you already have both `oauth_token.json` and
a complete `.env` on your computer. Push both in:
```bash
pct push <CTID> /path/to/oauth_token.json /opt/frigate-gdrive-uploader/credentials/oauth_token.json
pct push <CTID> /path/to/.env /opt/frigate-gdrive-uploader/.env
pct exec <CTID> -- chown -R frigate-uploader:frigate-uploader /opt/frigate-gdrive-uploader/credentials /opt/frigate-gdrive-uploader/.env
pct exec <CTID> -- systemctl start frigate-gdrive-uploader
```
Skip straight to step 5 below (Confirm it's actually working).

**Otherwise** (manual setup, or Workspace/service account path):

Push your credentials in (whichever file matches your auth mode from
Section 2):
```bash
pct push <CTID> /path/to/oauth_token.json /opt/frigate-gdrive-uploader/credentials/oauth_token.json
# or:
pct push <CTID> /path/to/service_account.json /opt/frigate-gdrive-uploader/credentials/service_account.json
```

**Fix ownership** — pushed files land owned by root, but the service
runs as a dedicated unprivileged user:
```bash
pct exec <CTID> -- chown -R frigate-uploader:frigate-uploader /opt/frigate-gdrive-uploader/credentials
```

**Edit the config:**
```bash
pct exec <CTID> -- nano /opt/frigate-gdrive-uploader/.env
```
At minimum set `FRIGATE_URL`, `DRIVE_ROOT_FOLDER_ID`, and `DRIVE_AUTH_MODE`
(plus `OAUTH_TOKEN_FILE` or `SERVICE_ACCOUNT_FILE` to match).

**Start it:**
```bash
pct exec <CTID> -- systemctl start frigate-gdrive-uploader
pct exec <CTID> -- journalctl -u frigate-gdrive-uploader -f
```

## 5. Confirm it's actually working

You should see, with no errors in between:
```
[INFO] Processing N pending upload(s)...
[INFO] Downloading event <id> (<camera> / <label>)...
[INFO] Uploading <filename>.mp4 to Drive folder <id>...
[INFO] Uploaded event <id> -> Drive file <id>
```
Then check your Drive folder for a `YYYY/MM/DD` structure with `.mp4`
files landing in it.

**Check for stragglers** after it's had time to churn through any backlog:
```bash
pct exec <CTID> -- sqlite3 /opt/frigate-gdrive-uploader/db/events.db \
  "SELECT COUNT(*) FROM events WHERE uploaded = 0;"
```
Should trend toward 0.

## 6. Reboot resilience: fix the Proxmox host boot order

If Frigate runs in a **different** Proxmox guest on the same host, a
full host reboot can cause a race condition: both guests auto-start in
parallel, and if this container starts making requests before Frigate's
services have fully come up, you'll see `Connection refused` right after
boot (it self-heals within a minute since the service retries forever
without crashing, but it's cleaner to avoid entirely).

This container is already created with `--startup order=2` by default
(the install script sets this — override with `STARTUP_ORDER` if
needed). To complete the fix, set Frigate's own guest to start first
with a delay before the next wave:

```bash
pct set <frigate-guest-id> -startup order=1,up=120
```
(use `qm set` instead of `pct set` if Frigate runs in a VM, not an LXC
container)

**Verify after a reboot:**
```bash
pct status <CTID>
pct exec <CTID> -- systemctl status frigate-gdrive-uploader
pct exec <CTID> -- journalctl -u frigate-gdrive-uploader -b --no-pager | head -20
```
The `-b` flag shows only current-boot logs — you want to **not** see
`Connection refused` in the first few lines. Also confirm no data was
lost:
```bash
pct exec <CTID> -- sqlite3 /opt/frigate-gdrive-uploader/db/events.db \
  "SELECT SUM(uploaded=1), SUM(uploaded=0 AND tries>=20), SUM(uploaded=0 AND tries<20) FROM events;"
```
The uploaded count should be the same or higher than before the reboot,
never lower — state persists on disk regardless of restarts.

---

## Configuration reference (`.env`)

| Variable | Default | Description |
|---|---|---|
| `FRIGATE_URL` | — (required) | Base URL of your Frigate instance, e.g. `http://192.168.1.10:5000` |
| `ONLY_CAMERAS` | (all) | Comma-separated camera names to include only |
| `MIN_EVENT_DURATION_SECONDS` | `0` | Skip events shorter than this |
| `REQUIRE_FINISHED_EVENT` | `true` | Only upload once Frigate has finalized the clip |
| `POLL_INTERVAL_SECONDS` | `30` | How often to check Frigate for new events |
| `STARTUP_LOOKBACK_SECONDS` | `21600` (6h) | How far back to check on startup |
| `DRIVE_AUTH_MODE` | `service_account` | `service_account` (Workspace) or `oauth_user` (personal Gmail) |
| `SERVICE_ACCOUNT_FILE` | `credentials/service_account.json` | Used when `DRIVE_AUTH_MODE=service_account` |
| `OAUTH_TOKEN_FILE` | `credentials/oauth_token.json` | Used when `DRIVE_AUTH_MODE=oauth_user` |
| `DRIVE_ROOT_FOLDER_ID` | — (required) | Destination folder or Shared Drive ID |
| `DATE_SUBFOLDERS` | `true` | Organize uploads into `YYYY/MM/DD` subfolders |
| `TZ` | `UTC` | IANA timezone for filenames/folder dates, e.g. `Europe/Amsterdam`. **Set this** - defaulting to UTC will be off by several hours from your actual local time |
| `DB_PATH` | `db/events.db` | SQLite state file |
| `TMP_DIR` | `tmp` | Scratch space for in-progress downloads |
| `LOG_FILE` / `LOG_LEVEL` | `logs/app.log` / `INFO` | Logging destination and verbosity |
| `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT` | `10485760` (10MB) / `5` | Log rotation - prevents unbounded disk growth |
| `HEARTBEAT_FILE` | `db/heartbeat.txt` | Updated every poll cycle; used by the status dashboard to detect a hung process |
| `MAX_RETRY_ATTEMPTS` | `20` | Give up retrying a single event after this many failures |
| `DB_RETENTION_DAYS` | `30` | How long to keep old event rows locally (Drive files are never touched by this) |
| `DRIVE_RETENTION_MAX_AGE_DAYS` | `0` (disabled) | Delete uploaded files from Drive once older than this many days |
| `DRIVE_RETENTION_MAX_SIZE_GB` | `0` (disabled) | Delete oldest uploaded files once total tracked storage exceeds this |
| `DRIVE_RETENTION_DRY_RUN` | `false` | Log what would be deleted without actually deleting anything |
| `RETENTION_CHECK_INTERVAL_SECONDS` | `3600` | How often to check/enforce retention |
| `DASHBOARD_PORT` | `8080` | Port for the status dashboard |
| `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` | (blank) | Set both to require HTTP Basic Auth on the dashboard; leave blank for open access |
| `SERVICE_UNIT_NAME` | `frigate-gdrive-uploader` | Must match the actual systemd unit name; used by the dashboard to check service status |

## Status dashboard

A small read-only web page — runs as its own service (`frigate-gdrive-dashboard`), enabled and started automatically by the install script. Open `http://<container-ip>:8080` in a browser.

Shows:
- Service health (a badge: Running / Warning / Not running), based primarily on a heartbeat file the main service updates every poll cycle — more reliable than asking systemd alone, since a hung/deadlocked process would still show as "systemd active" while no longer doing anything
- Upload counts: uploaded, pending, permanently given up, currently retained on Drive
- Total tracked storage used
- Recent events table with per-event status and last error, if any
- Current retention policy summary
- A **Download full log file** button — the fastest way to grab logs for troubleshooting without SSHing into the container

It auto-refreshes every 30 seconds. It's read-only (no write actions available) and never displays secrets — but it does expose event metadata (camera names, timestamps) and log contents on your network. Set `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` in `.env` if that matters for your setup; leave both blank for open access (fine on a trusted home LAN, which is the default).

If it's ever missing after an install (e.g. you're on an older repo checkout that predates this feature), the install script logs nothing about it and the main service works exactly as before — the dashboard is optional and non-blocking.

## Home Assistant integration

The dashboard exposes a JSON endpoint at `/api/status`, built specifically to plug into Home Assistant's `rest:` integration — one HTTP request feeding several sensors, refreshed on whatever interval you choose. Example response:
```json
{
  "state": "ok",
  "detail": "active, last heartbeat 4s ago (systemd: active)",
  "heartbeat_age_seconds": 4.2,
  "uploaded": 189,
  "pending": 0,
  "given_up": 11,
  "retained": 189,
  "bytes_stored": 943718400,
  "gb_stored": 0.879,
  "retention_enabled": false,
  "retention_max_age_days": 0,
  "retention_max_size_gb": 0,
  "retention_dry_run": false,
  "timestamp": 1787950000.12
}
```

**Add this to Home Assistant's `configuration.yaml`** (replace `192.168.1.50` with your container's actual IP):
```yaml
rest:
  - resource: http://192.168.1.50:8080/api/status
    scan_interval: 60
    sensor:
      - name: "Frigate Uploader Status"
        value_template: "{{ value_json.state }}"
      - name: "Frigate Uploader Uploaded Count"
        value_template: "{{ value_json.uploaded }}"
        state_class: total_increasing
      - name: "Frigate Uploader Pending Count"
        value_template: "{{ value_json.pending }}"
      - name: "Frigate Uploader Storage Used"
        value_template: "{{ value_json.gb_stored }}"
        unit_of_measurement: "GB"
        state_class: measurement
    binary_sensor:
      - name: "Frigate Uploader Problem"
        device_class: problem
        value_template: "{{ value_json.state != 'ok' }}"
```

**If you set `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD`** in `.env`, add authentication to the same `rest:` block:
```yaml
rest:
  - resource: http://192.168.1.50:8080/api/status
    authentication: basic
    username: "your_dashboard_username"
    password: "your_dashboard_password"
    scan_interval: 60
    sensor:
      # ...same as above
```

**A simple Lovelace card** using the entities above:
```yaml
type: entities
title: Frigate Uploader
entities:
  - entity: sensor.frigate_uploader_status
  - entity: binary_sensor.frigate_uploader_problem
  - entity: sensor.frigate_uploader_uploaded_count
  - entity: sensor.frigate_uploader_pending_count
  - entity: sensor.frigate_uploader_storage_used
```

The `binary_sensor.frigate_uploader_problem` entity (device class `problem`) is what you'd want to add to an existing "Problems" or alerts dashboard/automation — it turns `on` whenever `state` isn't `"ok"` (covers both `warn` and `fail`), so a single automation trigger on that entity covers "something needs attention" without needing to parse the text state yourself.

## Drive retention (optional, off by default)

Automatically deletes uploaded files from Drive once they're older than a set number of days and/or once total tracked storage exceeds a size cap (oldest files deleted first). Both settings default to `0` (disabled) — this is a destructive feature and requires explicit opt-in.

**Important safety property:** retention only ever acts on files *this tool itself uploaded and recorded locally* — there is no folder-wide sweep of your Drive. If the local database is ever lost or reset, retention simply has nothing to act on and does nothing, rather than risking deleting anything unexpected.

**Before trusting this with real deletions**, set `DRIVE_RETENTION_DRY_RUN=true` and watch the logs (or the dashboard) for a while — it logs exactly what it *would* delete and why, without calling Drive's delete API at all. Once you've confirmed the behavior matches what you expect, set it back to `false`.

```
DRIVE_RETENTION_MAX_AGE_DAYS=90        # delete anything older than 90 days
DRIVE_RETENTION_MAX_SIZE_GB=100        # also cap total storage at 100GB
DRIVE_RETENTION_DRY_RUN=true           # verify first before trusting it
```

Both can be set together — age-based deletion runs first, then size-based trimming runs on whatever's left if it's still over budget. Checked once per `RETENTION_CHECK_INTERVAL_SECONDS` (default hourly), not every poll cycle, to avoid hammering Drive's API.

A file that's already gone from Drive (deleted manually, or in a prior run that crashed before recording it) is treated as a successful cleanup, not an error — retention won't get stuck retrying something that's already done.

## Alternative deployment: Docker / Docker Compose

If you're not using Proxmox LXC, `Dockerfile` and `docker-compose.yml`
are included:
```bash
docker compose up -d --build
docker logs -f frigate-gdrive-uploader
```

---

## Troubleshooting

**Dashboard shows "Not running" but I just started the service**
→ Give it one full poll cycle first (`POLL_INTERVAL_SECONDS`, default
30s) — the "Running" badge needs at least one successful heartbeat
write, which only happens after the first poll completes. If it's still
showing "Not running" after a minute, check
`pct exec <CTID> -- journalctl -u frigate-gdrive-uploader -n 30` for the
actual error.

**Dashboard shows "Warning: may be stuck"**
→ The service is systemd-active but hasn't written a heartbeat recently.
Check the logs (via the dashboard's download button, or
`journalctl -u frigate-gdrive-uploader -f`) for what it's stuck on —
usually a slow/hanging network call to Frigate or Drive.

**I set retention but nothing is getting deleted**
→ Check `DRIVE_RETENTION_DRY_RUN` — if it's `true`, that's expected;
dry-run only logs what it *would* delete. Also confirm at least one of
`DRIVE_RETENTION_MAX_AGE_DAYS` / `DRIVE_RETENTION_MAX_SIZE_GB` is
actually non-zero, and that enough time has passed for a retention check
to run (`RETENTION_CHECK_INTERVAL_SECONDS`, default hourly — it doesn't
run every poll cycle).

**`Connection refused` when reaching Frigate**
→ Port 5000 isn't published on your Frigate host. See "Before you start,"
item 2.

**`422 Unprocessable Entity`**
→ A query parameter type mismatch with Frigate's API. See "Before you
start," item 3, for how to diagnose it directly against your instance.

**`403 Forbidden: storageQuotaExceeded`**
→ You're using `DRIVE_AUTH_MODE=service_account` on a personal Gmail
account. Switch to `oauth_user` — see Section 2, Option A.

**`Error 400: redirect_uri_mismatch` in OAuth Playground**
→ This only happens with the manual OAuth Playground method (Option A,
Step 3 alternative) — your OAuth Client needs to be "Web application"
type with the exact redirect URI registered. Easier fix: use
`oauth_setup_wizard.py` instead (Option A, Step 3), which sidesteps this
entirely with a "Desktop app" client and a real localhost redirect.

**`oauth_setup_wizard.py` won't open / "connection refused" in browser**
→ Make sure you're running it on the same computer as the browser you're
using — it binds to `127.0.0.1` only, by design, so it can't be accessed
remotely (e.g. don't run it inside the Proxmox container and try to
reach it from your PC's browser).
→ If port 5000 is already used by something else on your machine, set
`WIZARD_PORT=5050 python3 oauth_setup_wizard.py` and open that port instead.

**Filenames/folder dates show the wrong time (off by several hours)**
→ `TZ` isn't set in `.env`, so it defaults to UTC. Frigate event
timestamps are absolute Unix epoch values with no timezone attached —
without `TZ` set to your actual IANA timezone name (e.g.
`Europe/Amsterdam`), filenames will show UTC time, not your local time.
A 19:00 local event during CEST (UTC+2) would show as `17:00` in the
filename, for example. Set `TZ` in `.env` and restart.

**Service starts but nothing uploads, no errors**
→ Check `REQUIRE_FINISHED_EVENT`. Very short or always-active events may
never get an `end_time`. Try setting it to `false` temporarily to
confirm this is the cause.

**Large/long events time out during download**
→ Frigate's internal nginx proxy has a 360s clip-assembly timeout by
default. For cameras with very long continuous events, raise
`proxy_read_timeout` in Frigate's own nginx config.

**`400 Bad Request` on some events, with message "No recordings found for
the specified time range"**
→ This is normal, not a bug — Frigate's `has_clip: true` flag can be
stale once the underlying video segments for that time window have
already aged out (e.g. from retention settings), even though the event's
metadata still exists. This is a **permanent** failure: retrying the same
event will never succeed. The app detects this exact message and gives
up on the event after one attempt instead of retrying it 20 times. If you
see it happening for *recent* events rather than old backlog ones, check
your Frigate recording retention settings — they may be shorter than
expected.

**Container ID already exists when re-running the install script**
→ Either `pct destroy <CTID>` the old one, or use a different `CTID=`
value.

**`git push` says "everything up-to-date" but you expected new changes**
→ Your local file wasn't actually updated before committing. Verify the
file's content matches what you expect (e.g. `findstr "some_string"
main.py` on Windows, or `grep` on Linux) before `git add`.

---

## Known API assumptions (update here if Frigate changes its API)

As of Frigate 0.17.x, `/api/events` requires:
- `after` — **integer** epoch seconds (a float like `1700000000.5` is
  rejected with a 422)
- `has_clip` — **integer** `1`/`0` (the string `"true"` is rejected with
  a 422, even though many APIs accept boolean strings)
- `/api/events/<id>/clip.mp4` can return a **400** with
  `{"message": "No recordings found for the specified time range"}` even
  when the event's `has_clip` field says `true` — treated as permanent,
  not retried.

All three are handled in `fetch_events()` / `download_clip()` /
`process_pending()` in `main.py`, and covered by `test_logic.py`. If a
future Frigate version changes these, the diagnostic technique in
"Before you start," item 3, will tell you exactly what changed.
