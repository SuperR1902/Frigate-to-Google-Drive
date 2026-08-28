# Changelog

Every entry here was a real failure hit during actual setup, not a
theoretical fix. Kept for anyone maintaining or extending this project.

## Added a JSON status API for Home Assistant integration
**What was added:** a new `/api/status` endpoint on the status dashboard,
returning the same health/upload/retention data the HTML dashboard shows,
as plain JSON. Built specifically around Home Assistant's `rest:`
integration (verified against Home Assistant's current documented
config format), which lets multiple sensors share a single HTTP request
rather than each sensor polling separately. Respects the same optional
HTTP Basic Auth as the rest of the dashboard.

README includes a ready-to-adapt `configuration.yaml` snippet (sensors +
a `binary_sensor` with `device_class: problem` that's `on` whenever
`state != "ok"`, useful as a single trigger for "something needs
attention" automations) and a matching Lovelace entities card example.

Tested: correct JSON shape and values against a real database (including
the distinction between "uploaded" - total ever uploaded - and
"retained" - currently still on Drive, post-retention), retention config
fields reflecting live settings, Basic Auth enforcement matching the HTML
page, and graceful zeroed-out output when no database exists yet (fresh
install). Also smoke-tested against a live running server, not just
Flask's test client.

## Added status dashboard, log rotation, and Drive retention
**What was added:**
- **Status dashboard** (`status_dashboard.py`) — a small read-only Flask
  app, deployed as its own systemd service, showing upload stats, service
  health, and a log download button. Health detection deliberately trusts
  a heartbeat file (written by the main service every poll cycle) over
  `systemctl` state as the primary signal, since `systemctl` queries can
  fail for environmental reasons (e.g. D-Bus access) in some unprivileged
  container setups that have nothing to do with whether the uploader is
  actually working. Tested including the specific case where a fresh
  heartbeat and a stale/wrong systemd read disagree — the heartbeat wins.
- **Log rotation** — logs were previously unbounded; now capped via
  `LOG_MAX_BYTES`/`LOG_BACKUP_COUNT` using `RotatingFileHandler`.
- **Drive retention** (`DRIVE_RETENTION_MAX_AGE_DAYS`,
  `DRIVE_RETENTION_MAX_SIZE_GB`) — optional, off by default. Deletes
  uploaded files from Drive by age and/or to stay under a storage cap,
  oldest first. Required an additive, idempotent DB migration (tested
  against a simulated pre-existing database) to add `drive_file_id`,
  `file_size_bytes`, and `drive_deleted` tracking columns.

**Safety properties, tested explicitly:**
- Only ever acts on files this tool uploaded and recorded locally - no
  folder-wide sweep of Drive, so a lost/reset local database disables
  retention rather than risking an unexpected deletion.
- `DRIVE_RETENTION_DRY_RUN` mode makes zero actual Drive API calls,
  verified by asserting the mock delete function is never invoked.
- A file already missing from Drive (404 on delete) is treated as a
  successful cleanup, not an error loop.
- A genuine delete failure (not 404) leaves the record untouched so it's
  retried on the next check, rather than silently giving up.
- Both settings default to `0` (fully disabled) - this is a destructive
  feature and requires explicit opt-in.

Covered by 7 new retention tests and 13 new dashboard tests, on top of
the existing suite (38 tests total across all four test files).

## Setup wizard expanded to cover the full `.env`, not just OAuth
**Problem:** The wizard handled OAuth token generation, but every other
setting (Frigate URL, timezone, Drive folder) still had to be configured
manually by editing `.env` by hand inside the container — including the
two settings most likely to be wrong on a first attempt (the port
5000-vs-8971 mistake, and forgetting to set `TZ` at all).
**Fix:** The wizard now collects Frigate URL, timezone (auto-detected
from the browser via `Intl.DateTimeFormat`), and the Drive folder ID
alongside the OAuth fields, actively validates all of them (a real
reachability check against Frigate's `/api/events`, complete with the
specific 8971-vs-5000 hint if it fails; real IANA timezone validation),
and writes a complete, ready-to-use `.env` file alongside
`oauth_token.json` - built from `.env.example` as a template so every
other default stays intact, with a standalone fallback if the template
isn't found alongside the script. Optional fields left blank are treated
as skippable warnings, not blocking failures, so the wizard still
succeeds if you only want to configure a subset of settings right now. Covered by 11 test groups including the connection-refused/port
hint path and the "leave everything optional blank" path.

## Added a local OAuth setup wizard
**Problem:** The manual OAuth Playground method required creating a
"Web application" OAuth client with an exact redirect URI registered,
copying values between three different web pages, and manually
hand-assembling a JSON file — a genuinely error-prone process (see the
`redirect_uri_mismatch` entry below, which real users hit).
**Fix:** Added `oauth_setup_wizard.py`, a small local Flask app that runs
on your own computer and handles the entire flow: a simple form for
Client ID/Secret, redirect through Google's real consent screen, and
automatic token exchange. It also validates the result before you copy
anything — a real API call confirms the token works, and (if you provide
a folder ID) confirms that folder is actually accessible.
**Bonus simplification, not just UX:** this uses a genuine `localhost`
OAuth redirect, which "Desktop app" type clients support with zero
configuration in Google Cloud Console — completely avoiding the
redirect-URI-registration step that caused problems with the Playground
method. Tested with a full mocked-Google-server test suite (8 test
groups covering the happy path, CSRF state validation, missing
refresh_token, and Google-side errors) plus a live smoke test actually
serving HTTP requests, not just Flask's test client.

## Filenames/folder dates showed UTC time instead of local time
**Symptom:** A recording from 19:00 local time (Netherlands, CEST =
UTC+2) had a filename timestamp of 17:00 — exactly a 2-hour offset.
**Cause:** `resolve_target_folder()` and the filename-generation code in
`process_pending()` both hardcoded `tz=timezone.utc` when converting
Frigate's Unix epoch timestamps to a human-readable date/time. Epoch
timestamps are timezone-agnostic; converting them to a *specific*
timezone for display requires actually specifying one, and UTC was
hardcoded rather than configurable.
**Fix:** Added a `TZ` setting (IANA timezone name, e.g.
`Europe/Amsterdam`) that both call sites now use via Python's `zoneinfo`.
Startup validation rejects invalid timezone names with a clear error
instead of silently producing wrong timestamps, and logs a warning if
`TZ` is left unset (defaulting to UTC). Also added the `tzdata` PyPI
package as a fallback, since some minimal Docker base images (e.g.
`python:3.12-slim`) don't ship the OS-level IANA timezone database that
`zoneinfo` normally relies on.

## Boot-order race condition on Proxmox host reboot
**Symptom:** After a full Proxmox host reboot, the uploader container
starts fine and its systemd service starts fine, but the very first
Frigate request fails with `Connection refused` — even though Frigate
comes up successfully moments later.
**Cause:** Multiple Proxmox guests set to auto-start (`onboot: 1`) start
in parallel with no guaranteed ordering. If Frigate's guest takes longer
to fully boot its services than this container takes to start and make
its first request, the request loses the race.
**Fix:** `create-frigate-gdrive-ct.sh` now sets `--startup order=2` on
the container by default (configurable via `STARTUP_ORDER`). Pair this
with setting Frigate's own guest to `order=1,up=120` (start first, then
wait 120s before the next wave starts) — see README, "Reboot resilience."
**Note:** this isn't a code bug — the app already retries forever without
crashing, so it self-heals within a minute either way. The startup order
fix just avoids the noisy failure and shortens the gap to a working state.

## `400 Bad Request: "No recordings found for the specified time range"`
**Symptom:** Some events fail every time with a 400 error, even though
Frigate's own `has_clip` field says `true` for them.
**Cause:** Frigate's event metadata can outlive the underlying recording
segments once retention settings purge old video — `has_clip: true` can
be stale. This is permanent: retrying the same event can never succeed.
**Fix:** `process_pending()` now detects this specific message and marks
the event as permanently failed after one attempt, instead of retrying
it up to `MAX_RETRY_ATTEMPTS` (20) times.
**Related bug found while fixing this:** `download_clip()` used
`requests.get(..., stream=True)` inside a `with` block; exiting that
block via an exception (e.g. `raise_for_status()`) closed the connection
*before* the error body could be read, so `.json()` on the error always
came back empty. Fixed by force-reading `.content` while the connection
is still open, for any 4xx/5xx response.

## `403 Forbidden: storageQuotaExceeded`
**Symptom:** Frigate connection works, downloads work, but every upload
to Drive fails with this error.
**Cause:** Google service accounts have **zero storage quota of their
own**. Sharing a folder with one (even as Editor) does not let it spend
your personal quota — that only works inside a Shared Drive, which
requires Google Workspace. This wasn't discovered until well into setup
because everything else appeared to work first.
**Fix:** Added `DRIVE_AUTH_MODE=oauth_user` as an alternative to
`service_account` — uploads using the user's own Drive quota directly via
OAuth, the correct approach for personal `@gmail.com` accounts. Also
added `supportsAllDrives=True` throughout so Shared Drives work correctly
too, for anyone actually on Workspace.

## `Error 400: redirect_uri_mismatch` in OAuth Playground
**Symptom:** Following Google's own OAuth Playground flow to get a
refresh token fails immediately.
**Cause:** The OAuth Client was created as "Desktop app" type, which
doesn't allow configuring custom redirect URIs at all — but OAuth
Playground requires one to be explicitly registered.
**Fix:** Documentation now specifies "Web application" type with
`https://developers.google.com/oauthplayground` added as an authorized
redirect URI.

## `422 Unprocessable Entity` on `/api/events`
**Symptom:** Every request to fetch events fails with a 422, even though
the URL looks correct.
**Cause, part 1:** `after` was sent as a raw float
(`1787901196.2311497`); Frigate's API (FastAPI/Pydantic-based) requires
a plain integer and rejects decimals outright.
**Cause, part 2:** once part 1 was fixed, the *same* error persisted —
`has_clip` was sent as the string `"true"`, but Frigate's API expects an
integer `1`/`0`, not a boolean string (unlike many APIs that accept
either).
**Fix:** `fetch_events()` now sends `after` via `int()` and `has_clip` as
the literal integer `1`.
**Diagnostic technique that found both:** curling the exact failing URL
directly and reading the JSON `detail` field FastAPI always includes —
this pinpointed the exact parameter and expected type both times, rather
than guessing from generic search results.

## `Connection refused` reaching Frigate
**Symptom:** The uploader can't reach Frigate at all.
**Cause:** Many current Frigate Docker/Portainer setups only publish
port 8971 (the authenticated UI) and don't expose port 5000 (the plain
HTTP API this tool uses) to the LAN at all.
**Fix:** No code fix needed — this is a Frigate-side configuration issue.
Documented as the first thing to check before touching anything else
("Before you start," item 2 in the README).

## `curl | bash` piped execution crashed with "unbound variable"
**Symptom:** Running the install script via
`curl ... | GIT_REPO_URL=... bash` produced inconsistent, garbled
behavior instead of a clean error or success.
**Cause:** The script used `${BASH_SOURCE[0]}` to find its own directory
(for a local-file fallback), but under `set -u` (strict mode), this is
unset when a script is piped via stdin rather than run as a saved file.
**Fix:** Guard `${BASH_SOURCE[0]:-}` with a default and check whether it
actually points to a real file before using it as a directory reference.
When piped without `GIT_REPO_URL` set (meaning there's no fallback
location for local files), the script now fails immediately with a clear
explanatory message instead of crashing unpredictably.

## Template resolution double-prefix bug
**Symptom:** (caught before shipping, via testing) re-running the install
script when a template was already cached locally would have produced a
malformed template reference like
`local:vztmpl/local:vztmpl/debian-13-standard_....zst`.
**Cause:** `pveam list` already returns the full `storage:vztmpl/`-
prefixed reference, but the script's `else` branch re-prefixed it again.
**Fix:** Removed the redundant re-prefixing; the reference from
`pveam list` is used as-is.
