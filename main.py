#!/usr/bin/env python3
"""
Frigate -> Google Drive uploader.

Polls the Frigate API for new events, downloads finished clips, and uploads
them to Google Drive using a service account. Tracks state in a local
SQLite database so restarts don't lose track of what's been uploaded and
failed uploads are retried automatically.

Configuration is read from environment variables (see .env.example).
"""

import io
import json
import logging
import logging.handlers
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

load_dotenv()

# --------------------------------------------------------------------------
# Configuration (all via environment variables / .env)
# --------------------------------------------------------------------------

def env(name, default=None, cast=str):
    val = os.environ.get(name, default)
    if val is None:
        return None
    if cast is bool:
        return str(val).strip().lower() in ("1", "true", "yes", "on")
    return cast(val)


FRIGATE_URL = env("FRIGATE_URL", "http://localhost:5000").rstrip("/")
POLL_INTERVAL_SECONDS = env("POLL_INTERVAL_SECONDS", 30, int)
STARTUP_LOOKBACK_SECONDS = env("STARTUP_LOOKBACK_SECONDS", 6 * 3600, int)
ONLY_CAMERAS = [c.strip() for c in env("ONLY_CAMERAS", "").split(",") if c.strip()]
MIN_EVENT_DURATION_SECONDS = env("MIN_EVENT_DURATION_SECONDS", 0, int)
REQUIRE_FINISHED_EVENT = env("REQUIRE_FINISHED_EVENT", True, bool)

SERVICE_ACCOUNT_FILE = env("SERVICE_ACCOUNT_FILE", "credentials/service_account.json")
DRIVE_ROOT_FOLDER_ID = env("DRIVE_ROOT_FOLDER_ID")  # required
DATE_SUBFOLDERS = env("DATE_SUBFOLDERS", True, bool)  # YYYY/MM/DD under root

# Personal Gmail accounts can't use a service account to upload files (service
# accounts have no storage quota of their own, and Shared Drives / domain-wide
# delegation both require Google Workspace). Set DRIVE_AUTH_MODE=oauth_user to
# instead upload using your own Google account's quota via OAuth.
DRIVE_AUTH_MODE = env("DRIVE_AUTH_MODE", "service_account")  # "service_account" or "oauth_user"
OAUTH_TOKEN_FILE = env("OAUTH_TOKEN_FILE", "credentials/oauth_token.json")

DB_PATH = env("DB_PATH", "db/events.db")
TMP_DIR = env("TMP_DIR", "tmp")
MAX_RETRY_ATTEMPTS = env("MAX_RETRY_ATTEMPTS", 20, int)
DB_RETENTION_DAYS = env("DB_RETENTION_DAYS", 30, int)
DELETE_LOCAL_RECORDINGS_AFTER_UPLOAD = env("DELETE_LOCAL_RECORDINGS_AFTER_UPLOAD", False, bool)

LOG_LEVEL = env("LOG_LEVEL", "INFO")
LOG_FILE = env("LOG_FILE", "logs/app.log")
LOG_MAX_BYTES = env("LOG_MAX_BYTES", 10 * 1024 * 1024, int)  # 10MB per file
LOG_BACKUP_COUNT = env("LOG_BACKUP_COUNT", 5, int)

# Heartbeat file: updated every poll cycle so external tools (e.g. the
# status dashboard) can tell "process is alive" from "process is actually
# still doing its job" - a hung/deadlocked process would still show as
# systemd-active but would stop updating this.
HEARTBEAT_FILE = env("HEARTBEAT_FILE", "db/heartbeat.txt")

# Drive retention: automatically delete uploaded files from Drive once
# they're older than N days and/or once total tracked storage exceeds a
# size cap (oldest first). Both are OFF by default - this is a
# destructive feature and requires explicit opt-in. Only files this tool
# itself uploaded and recorded locally are ever touched; there is no
# folder-wide sweep, so a lost/reset local DB simply disables retention
# rather than risking deleting anything unexpected.
DRIVE_RETENTION_MAX_AGE_DAYS = env("DRIVE_RETENTION_MAX_AGE_DAYS", 0, int)  # 0 = disabled
DRIVE_RETENTION_MAX_SIZE_GB = env("DRIVE_RETENTION_MAX_SIZE_GB", 0, float)  # 0 = disabled
DRIVE_RETENTION_DRY_RUN = env("DRIVE_RETENTION_DRY_RUN", False, bool)
RETENTION_CHECK_INTERVAL_SECONDS = env("RETENTION_CHECK_INTERVAL_SECONDS", 3600, int)

# Timezone used for filenames and YYYY/MM/DD Drive folders. Frigate event
# timestamps are Unix epoch (absolute, timezone-agnostic) - without this,
# filenames would show UTC time regardless of where you actually are,
# which is wrong by several hours for most timezones. Use an IANA name,
# e.g. "Europe/Amsterdam", "America/New_York". Defaults to UTC only if
# unset - set this to your actual timezone in .env.
TZ_NAME = env("TZ", "UTC")
try:
    LOCAL_TZ = ZoneInfo(TZ_NAME)
except ZoneInfoNotFoundError:
    LOCAL_TZ = None  # validated properly in main(); avoids crashing at import time

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

Path(os.path.dirname(LOG_FILE) or ".").mkdir(parents=True, exist_ok=True)
Path(os.path.dirname(DB_PATH) or ".").mkdir(parents=True, exist_ok=True)
Path(os.path.dirname(HEARTBEAT_FILE) or ".").mkdir(parents=True, exist_ok=True)
Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
        ),
    ],
)
log = logging.getLogger("frigate-gdrive")

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    log.info("Received signal %s, shutting down after current cycle...", signum)
    _shutdown = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# --------------------------------------------------------------------------
# SQLite state
# --------------------------------------------------------------------------

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            camera TEXT,
            label TEXT,
            start_time REAL,
            uploaded INTEGER DEFAULT 0,
            tries INTEGER DEFAULT 0,
            last_error TEXT,
            created REAL DEFAULT (strftime('%s','now'))
        )
        """
    )
    # Additive schema migration for retention tracking. Wrapped so this is
    # safe to run against a database created by an earlier version of this
    # script that doesn't have these columns yet, and safe to run again on
    # every startup once they already exist.
    for ddl in (
        "ALTER TABLE events ADD COLUMN drive_file_id TEXT",
        "ALTER TABLE events ADD COLUMN file_size_bytes INTEGER",
        "ALTER TABLE events ADD COLUMN drive_deleted INTEGER DEFAULT 0",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
    conn.commit()
    return conn


def record_seen(conn, event):
    conn.execute(
        "INSERT OR IGNORE INTO events (event_id, camera, label, start_time) VALUES (?, ?, ?, ?)",
        (event["id"], event.get("camera"), event.get("label"), event.get("start_time")),
    )
    conn.commit()


def mark_uploaded(conn, event_id, drive_file_id=None, file_size_bytes=None):
    conn.execute(
        "UPDATE events SET uploaded = 1, last_error = NULL, drive_file_id = ?, file_size_bytes = ? "
        "WHERE event_id = ?",
        (drive_file_id, file_size_bytes, event_id),
    )
    conn.commit()


def mark_failed(conn, event_id, error):
    conn.execute(
        "UPDATE events SET tries = tries + 1, last_error = ? WHERE event_id = ?",
        (str(error)[:500], event_id),
    )
    conn.commit()


def get_pending(conn):
    cur = conn.execute(
        f"SELECT event_id, camera, label, start_time, tries FROM events "
        f"WHERE uploaded = 0 AND tries < ? ORDER BY start_time ASC",
        (MAX_RETRY_ATTEMPTS,),
    )
    return cur.fetchall()


def cleanup_old_rows(conn):
    cutoff = time.time() - DB_RETENTION_DAYS * 86400
    conn.execute("DELETE FROM events WHERE created < ?", (cutoff,))
    conn.commit()

# --------------------------------------------------------------------------
# Google Drive
# --------------------------------------------------------------------------

_drive_service = None
_folder_cache = {}  # (parent_id, name) -> folder_id


def get_drive_service():
    global _drive_service
    if _drive_service is None:
        if DRIVE_AUTH_MODE == "oauth_user":
            creds = UserCredentials.from_authorized_user_file(OAUTH_TOKEN_FILE, scopes=DRIVE_SCOPES)
            if not creds.valid:
                if creds.refresh_token:
                    creds.refresh(GoogleAuthRequest())
                    # Persist the refreshed access token so we don't have to
                    # re-refresh on every restart before it naturally expires.
                    with open(OAUTH_TOKEN_FILE, "w") as f:
                        f.write(creds.to_json())
                else:
                    raise RuntimeError(
                        f"OAuth token at {OAUTH_TOKEN_FILE} is invalid and has no refresh_token. "
                        "Re-run the one-time authorization step to generate a new token file."
                    )
        else:
            creds = service_account.Credentials.from_service_account_file(
                SERVICE_ACCOUNT_FILE, scopes=DRIVE_SCOPES
            )
        _drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive_service


def get_or_create_folder(name, parent_id):
    key = (parent_id, name)
    if key in _folder_cache:
        return _folder_cache[key]

    service = get_drive_service()
    query = (
        f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents and trashed = false"
    )
    resp = service.files().list(
        q=query,
        fields="files(id, name)",
        spaces="drive",
        corpora="allDrives",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
    ).execute()
    files = resp.get("files", [])
    if files:
        folder_id = files[0]["id"]
    else:
        metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = service.files().create(
            body=metadata, fields="id", supportsAllDrives=True
        ).execute()
        folder_id = folder["id"]
        log.info("Created Drive folder '%s' under %s", name, parent_id)

    _folder_cache[key] = folder_id
    return folder_id


def resolve_target_folder(start_time_epoch):
    if not DATE_SUBFOLDERS:
        return DRIVE_ROOT_FOLDER_ID

    dt = datetime.fromtimestamp(start_time_epoch, tz=LOCAL_TZ)
    year_id = get_or_create_folder(dt.strftime("%Y"), DRIVE_ROOT_FOLDER_ID)
    month_id = get_or_create_folder(dt.strftime("%m"), year_id)
    day_id = get_or_create_folder(dt.strftime("%d"), month_id)
    return day_id


def upload_file_to_drive(local_path, drive_filename, folder_id):
    service = get_drive_service()
    metadata = {"name": drive_filename, "parents": [folder_id]}
    media = MediaFileUpload(local_path, mimetype="video/mp4", resumable=True)
    request = service.files().create(
        body=metadata, media_body=media, fields="id,size", supportsAllDrives=True
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log.debug("Upload progress: %d%%", int(status.progress() * 100))
    file_id = response.get("id")
    # Prefer Drive's own reported size; fall back to the local file size if
    # Drive didn't return one for some reason.
    try:
        size = int(response.get("size"))
    except (TypeError, ValueError):
        size = os.path.getsize(local_path) if os.path.exists(local_path) else None
    return file_id, size

# --------------------------------------------------------------------------
# Drive retention (optional, off by default)
# --------------------------------------------------------------------------

def delete_drive_file(file_id):
    """Delete a file from Drive. Returns True if deleted (or already gone),
    False if the delete failed for a reason worth retrying later."""
    service = get_drive_service()
    try:
        service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
        return True
    except Exception as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        if status == 404:
            # Already gone (manually deleted, or deleted in a prior run that
            # crashed before we recorded it) - treat as success either way.
            return True
        log.error("Failed to delete Drive file %s: %s", file_id, e)
        return False


def enforce_retention(conn):
    """Delete uploaded files from Drive per DRIVE_RETENTION_MAX_AGE_DAYS
    and/or DRIVE_RETENTION_MAX_SIZE_GB, oldest first. Only ever acts on
    files this tool uploaded and recorded locally - never a folder-wide
    sweep - so a lost/reset local DB just disables retention rather than
    risking deleting anything unexpected. Both settings default to 0
    (disabled); nothing happens unless explicitly configured."""
    if not DRIVE_RETENTION_MAX_AGE_DAYS and not DRIVE_RETENTION_MAX_SIZE_GB:
        return

    dry_run_prefix = "[DRY RUN] Would delete" if DRIVE_RETENTION_DRY_RUN else "Deleting"
    deleted_count = 0
    freed_bytes = 0

    if DRIVE_RETENTION_MAX_AGE_DAYS:
        cutoff = time.time() - DRIVE_RETENTION_MAX_AGE_DAYS * 86400
        rows = conn.execute(
            "SELECT event_id, drive_file_id, file_size_bytes, start_time FROM events "
            "WHERE uploaded = 1 AND drive_deleted = 0 AND drive_file_id IS NOT NULL "
            "AND start_time < ? ORDER BY start_time ASC",
            (cutoff,),
        ).fetchall()
        for event_id, drive_file_id, file_size_bytes, start_time in rows:
            age_days = (time.time() - start_time) / 86400
            log.info(
                "%s event %s (Drive file %s, %.1f days old, past max age of %d days)",
                dry_run_prefix, event_id, drive_file_id, age_days, DRIVE_RETENTION_MAX_AGE_DAYS,
            )
            if not DRIVE_RETENTION_DRY_RUN:
                if delete_drive_file(drive_file_id):
                    conn.execute("UPDATE events SET drive_deleted = 1 WHERE event_id = ?", (event_id,))
                    conn.commit()
                    deleted_count += 1
                    freed_bytes += file_size_bytes or 0

    if DRIVE_RETENTION_MAX_SIZE_GB:
        max_bytes = DRIVE_RETENTION_MAX_SIZE_GB * (1024 ** 3)
        rows = conn.execute(
            "SELECT event_id, drive_file_id, file_size_bytes, start_time FROM events "
            "WHERE uploaded = 1 AND drive_deleted = 0 AND drive_file_id IS NOT NULL "
            "ORDER BY start_time ASC"
        ).fetchall()
        total_bytes = sum(r[2] or 0 for r in rows)

        if total_bytes > max_bytes:
            log.info(
                "Tracked Drive usage is %.2f GB, over the %.2f GB limit - trimming oldest files first",
                total_bytes / (1024 ** 3), DRIVE_RETENTION_MAX_SIZE_GB,
            )
        for event_id, drive_file_id, file_size_bytes, start_time in rows:
            if total_bytes <= max_bytes:
                break
            log.info(
                "%s event %s (Drive file %s, %s bytes) to get under the %.2f GB size limit",
                dry_run_prefix, event_id, drive_file_id, file_size_bytes, DRIVE_RETENTION_MAX_SIZE_GB,
            )
            if not DRIVE_RETENTION_DRY_RUN:
                if delete_drive_file(drive_file_id):
                    conn.execute("UPDATE events SET drive_deleted = 1 WHERE event_id = ?", (event_id,))
                    conn.commit()
                    deleted_count += 1
                    freed_bytes += file_size_bytes or 0
            total_bytes -= file_size_bytes or 0

    if deleted_count:
        log.info("Retention: deleted %d file(s), freed %.2f MB", deleted_count, freed_bytes / (1024 ** 2))

# --------------------------------------------------------------------------
# Frigate API
# --------------------------------------------------------------------------

def fetch_events(after_epoch):
    params = {"after": int(after_epoch), "has_clip": 1, "limit": 200}
    resp = requests.get(f"{FRIGATE_URL}/api/events", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def event_is_finished(event):
    # Frigate sets end_time once the event/clip is finalized. While an event
    # is ongoing, end_time is null and the clip isn't ready to download yet.
    return event.get("end_time") is not None


def download_clip(event_id, dest_path):
    url = f"{FRIGATE_URL}/api/events/{event_id}/clip.mp4"
    with requests.get(url, stream=True, timeout=(10, 600)) as r:
        if r.status_code >= 400:
            # Force-read the (small) error body now, while the connection
            # is still open. With stream=True, exiting this 'with' block
            # closes the connection before the body is read, so without
            # this, callers inspecting r.json()/r.content after an
            # HTTPError would always get an empty response.
            _ = r.content
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def give_up_on_event(conn, event_id, reason):
    """Mark an event as permanently failed (won't be retried again) without
    counting it as a normal failure. Used for cases we know retrying can
    never fix, e.g. the underlying recording data no longer exists."""
    log.warning("Giving up on event %s: %s", event_id, reason)
    conn.execute(
        "UPDATE events SET tries = ? WHERE event_id = ?",
        (MAX_RETRY_ATTEMPTS, event_id),
    )
    conn.commit()


def process_pending(conn):
    pending = get_pending(conn)
    if not pending:
        return

    log.info("Processing %d pending upload(s)...", len(pending))
    for event_id, camera, label, start_time, tries in pending:
        if _shutdown:
            return

        dt = datetime.fromtimestamp(start_time, tz=LOCAL_TZ)
        filename = f"{dt.strftime('%Y-%m-%d_%H-%M-%S')}__{camera}__{label}__{event_id}.mp4"
        local_path = os.path.join(TMP_DIR, filename)

        try:
            log.info("Downloading event %s (%s / %s)...", event_id, camera, label)
            download_clip(event_id, local_path)

            folder_id = resolve_target_folder(start_time)
            log.info("Uploading %s to Drive folder %s...", filename, folder_id)
            file_id, file_size = upload_file_to_drive(local_path, filename, folder_id)

            mark_uploaded(conn, event_id, drive_file_id=file_id, file_size_bytes=file_size)
            log.info("Uploaded event %s -> Drive file %s (%s bytes)", event_id, file_id, file_size)

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 404:
                # Event no longer exists on Frigate (retention expired, etc.)
                give_up_on_event(conn, event_id, "event returned 404")
            elif status == 400:
                # Frigate returns 400 (not 404) when the event's metadata
                # still exists but the underlying recording segments have
                # already aged out - "has_clip: true" can be stale. This is
                # permanent; retrying the same event will never succeed.
                try:
                    detail = e.response.json().get("message", "")
                except Exception:
                    detail = ""
                if "no recordings found" in detail.lower():
                    give_up_on_event(conn, event_id, f"no recordings found for this time range")
                else:
                    log.error("HTTP 400 on event %s: %s", event_id, detail or e)
                    mark_failed(conn, event_id, e)
            else:
                log.error("HTTP error on event %s: %s", event_id, e)
                mark_failed(conn, event_id, e)
        except Exception as e:
            log.error("Failed to process event %s: %s", event_id, e)
            mark_failed(conn, event_id, e)
        finally:
            if os.path.exists(local_path):
                os.remove(local_path)


def poll_new_events(conn, since_epoch):
    try:
        events = fetch_events(since_epoch)
    except Exception as e:
        log.error("Failed to fetch events from Frigate: %s", e)
        return since_epoch

    latest_seen = since_epoch
    for event in events:
        if ONLY_CAMERAS and event.get("camera") not in ONLY_CAMERAS:
            continue
        if REQUIRE_FINISHED_EVENT and not event_is_finished(event):
            continue
        duration = (event.get("end_time") or time.time()) - event.get("start_time", 0)
        if duration < MIN_EVENT_DURATION_SECONDS:
            continue

        record_seen(conn, event)
        latest_seen = max(latest_seen, event.get("start_time", latest_seen))

    return latest_seen


def write_heartbeat():
    """Write the current time to HEARTBEAT_FILE. Lets external tools (the
    status dashboard) distinguish 'process alive' from 'process actually
    still doing its job' - a hung process stays systemd-active but stops
    updating this."""
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception as e:
        log.warning("Failed to write heartbeat file: %s", e)


def main():
    if not DRIVE_ROOT_FOLDER_ID:
        log.error("DRIVE_ROOT_FOLDER_ID is not set. Aborting.")
        sys.exit(1)
    if LOCAL_TZ is None:
        log.error(
            "TZ=%r is not a valid IANA timezone name. Aborting. "
            "Examples: 'Europe/Amsterdam', 'America/New_York', 'UTC'.",
            TZ_NAME,
        )
        sys.exit(1)
    if TZ_NAME == "UTC":
        log.warning(
            "TZ is not set (defaulting to UTC) - filenames and Drive folder "
            "dates will use UTC time, not your local time. Set TZ in .env "
            "to your IANA timezone name if this isn't what you want."
        )
    if DRIVE_AUTH_MODE == "oauth_user":
        if not os.path.exists(OAUTH_TOKEN_FILE):
            log.error("OAuth token file not found at %s. Aborting.", OAUTH_TOKEN_FILE)
            sys.exit(1)
    else:
        if not os.path.exists(SERVICE_ACCOUNT_FILE):
            log.error("Service account file not found at %s. Aborting.", SERVICE_ACCOUNT_FILE)
            sys.exit(1)

    conn = db_connect()
    since_epoch = time.time() - STARTUP_LOOKBACK_SECONDS
    log.info(
        "Starting Frigate->Drive uploader. Frigate=%s, lookback=%ds, poll every %ds",
        FRIGATE_URL, STARTUP_LOOKBACK_SECONDS, POLL_INTERVAL_SECONDS,
    )

    last_cleanup = 0
    last_retention_check = 0
    while not _shutdown:
        since_epoch = poll_new_events(conn, since_epoch)
        process_pending(conn)
        write_heartbeat()

        if time.time() - last_cleanup > 3600:
            cleanup_old_rows(conn)
            last_cleanup = time.time()

        if time.time() - last_retention_check > RETENTION_CHECK_INTERVAL_SECONDS:
            try:
                enforce_retention(conn)
            except Exception as e:
                log.error("Retention check failed: %s", e)
            last_retention_check = time.time()

        for _ in range(POLL_INTERVAL_SECONDS):
            if _shutdown:
                break
            time.sleep(1)

    log.info("Shut down cleanly.")
    conn.close()


if __name__ == "__main__":
    main()
