#!/usr/bin/env bash
#
# create-frigate-gdrive-ct.sh
#
# Run this ON THE PROXMOX HOST (as root) to create a preconfigured LXC
# container running the Frigate -> Google Drive uploader.
#
# It downloads an OS template if needed (Debian 13 by default - see
# TEMPLATE_OS below), creates an unprivileged container, installs Python +
# dependencies inside it, writes the application files, and sets up (but
# does not start) a systemd service.
#
# Usage:
#   chmod +x create-frigate-gdrive-ct.sh
#   ./create-frigate-gdrive-ct.sh
#
# Override any setting via environment variables, e.g.:
#   CTID=210 STORAGE=local-zfs BRIDGE=vmbr1 ./create-frigate-gdrive-ct.sh
#
# To use a specific template (e.g. matching one you already downloaded):
#   TEMPLATE_OS=debian-13-standard ./create-frigate-gdrive-ct.sh
# (this is already the default - see TEMPLATE_OS below)
#
# Override any setting via environment variables, e.g.:
#   CTID=210 STORAGE=local-zfs BRIDGE=vmbr1 ./create-frigate-gdrive-ct.sh
#
# To install the app by cloning it from your own GitHub repo instead of
# copying local files, set GIT_REPO_URL (and optionally GIT_REF, default
# "main"). The repo must have main.py and requirements.txt at its root:
#   GIT_REPO_URL=https://github.com/you/frigate-gdrive-uploader.git \
#     ./create-frigate-gdrive-ct.sh
#
# For a private repo, use a URL with an embedded token or SSH remote that
# the container can already authenticate with - see the README for
# guidance and the security tradeoffs of each approach.
#
set -euo pipefail

# --------------------------------------------------------------------------
# Configuration - override via environment variables if needed
# --------------------------------------------------------------------------
CTID="${CTID:-210}"
HOSTNAME_CT="${HOSTNAME_CT:-frigate-gdrive}"
STORAGE="${STORAGE:-local-lvm}"              # storage for the container rootfs
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}" # storage where CT templates live
DISK_SIZE_GB="${DISK_SIZE_GB:-4}"
MEMORY_MB="${MEMORY_MB:-512}"
SWAP_MB="${SWAP_MB:-512}"
CORES="${CORES:-1}"
BRIDGE="${BRIDGE:-vmbr0}"
# Boot order relative to other guests. Defaults to starting AFTER most
# things (order=2) since this container depends on Frigate already being
# reachable. If Frigate runs on the same Proxmox host, set that guest to
# order=1 with a startup delay (e.g. "pct set <frigate-id> -startup
# order=1,up=120") so it has time to fully start first - see README,
# "Reboot resilience," for why this matters and how to verify it.
STARTUP_ORDER="${STARTUP_ORDER:-order=2}"
IP_CONFIG="${IP_CONFIG:-dhcp}"               # e.g. "10.0.0.50/24,gw=10.0.0.1" for static
UNPRIVILEGED="${UNPRIVILEGED:-1}"
TEMPLATE_OS="${TEMPLATE_OS:-debian-13-standard}"  # e.g. debian-12-standard, debian-13-standard, ubuntu-24.04-standard

# Optional: clone the app from a git repo instead of pushing local files.
# Leave GIT_REPO_URL empty to use the local-file method (default).
GIT_REPO_URL="${GIT_REPO_URL:-}"
GIT_REF="${GIT_REF:-main}"

APP_DIR="/opt/frigate-gdrive-uploader"
SERVICE_USER="frigate-uploader"

echo "== Frigate -> Google Drive uploader: LXC provisioning =="
echo "CTID=$CTID  HOSTNAME=$HOSTNAME_CT  STORAGE=$STORAGE  DISK=${DISK_SIZE_GB}G  MEM=${MEMORY_MB}MB  BRIDGE=$BRIDGE"
echo

if pct status "$CTID" &>/dev/null; then
  echo "ERROR: Container ID $CTID already exists. Pick a different CTID (env var CTID=...)." >&2
  exit 1
fi

# --------------------------------------------------------------------------
# 1. Ensure the chosen OS template is available
# --------------------------------------------------------------------------
echo "-- Checking for $TEMPLATE_OS LXC template on storage '$TEMPLATE_STORAGE'..."
pveam update >/dev/null 2>&1 || true

TEMPLATE_FILE=$(pveam list "$TEMPLATE_STORAGE" 2>/dev/null | awk '{print $1}' | grep -E "${TEMPLATE_OS}_.*_amd64\.tar\.zst" | tail -n1 || true)

if [ -z "$TEMPLATE_FILE" ]; then
  echo "-- No local $TEMPLATE_OS template found, downloading the latest one..."
  LATEST=$(pveam available --section system | awk '{print $2}' | grep -E "^${TEMPLATE_OS}_.*_amd64\.tar\.zst$" | sort -V | tail -n1)
  if [ -z "$LATEST" ]; then
    echo "ERROR: Could not find a $TEMPLATE_OS template in 'pveam available'." >&2
    exit 1
  fi
  pveam download "$TEMPLATE_STORAGE" "$LATEST"
  TEMPLATE_FILE="${TEMPLATE_STORAGE}:vztmpl/${LATEST}"
fi
# Note: when found via `pveam list` above, TEMPLATE_FILE already includes
# the "storage:vztmpl/" prefix, so it's used as-is (no re-prefixing here).
echo "-- Using template: $TEMPLATE_FILE"

# --------------------------------------------------------------------------
# 2. Create the container
# --------------------------------------------------------------------------
echo "-- Creating container $CTID..."
pct create "$CTID" "$TEMPLATE_FILE" \
  --hostname "$HOSTNAME_CT" \
  --cores "$CORES" \
  --memory "$MEMORY_MB" \
  --swap "$SWAP_MB" \
  --rootfs "${STORAGE}:${DISK_SIZE_GB}" \
  --net0 "name=eth0,bridge=${BRIDGE},ip=${IP_CONFIG}" \
  --unprivileged "$UNPRIVILEGED" \
  --features nesting=1 \
  --startup "$STARTUP_ORDER" \
  --onboot 1
# Note: nesting=1 is required for systemd to work correctly in an
# unprivileged container on some OS templates, notably Debian 13.

echo "-- Starting container $CTID..."
pct start "$CTID"

echo "-- Waiting for network..."
for _ in $(seq 1 30); do
  if pct exec "$CTID" -- ping -c1 -W2 deb.debian.org &>/dev/null; then
    break
  fi
  sleep 2
done

# --------------------------------------------------------------------------
# 3. Install OS packages inside the container
# --------------------------------------------------------------------------
echo "-- Installing packages inside container..."
GIT_PKG=""
if [ -n "$GIT_REPO_URL" ]; then
  GIT_PKG="git"
fi
pct exec "$CTID" -- bash -c "apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip sqlite3 curl tzdata $GIT_PKG >/dev/null"

pct exec "$CTID" -- useradd -r -m -d "$APP_DIR" -s /usr/sbin/nologin "$SERVICE_USER" || true
pct exec "$CTID" -- mkdir -p "$APP_DIR"/{credentials,db,tmp,logs}

# --------------------------------------------------------------------------
# 4. Get the application files into the container: either clone from a git
#    repo (if GIT_REPO_URL is set) or push local files (default)
# --------------------------------------------------------------------------
if [ -n "$GIT_REPO_URL" ]; then
  echo "-- Cloning $GIT_REPO_URL (ref: $GIT_REF) inside the container..."
  pct exec "$CTID" -- bash -c "rm -rf /tmp/frigate-src && git clone --branch '$GIT_REF' --depth 1 '$GIT_REPO_URL' /tmp/frigate-src"

  for f in main.py requirements.txt; do
    if ! pct exec "$CTID" -- test -f "/tmp/frigate-src/$f"; then
      echo "ERROR: cloned repo is missing required file '$f' at its root." >&2
      echo "Check that $GIT_REPO_URL (ref $GIT_REF) contains the frigate-gdrive-uploader files at the repo root." >&2
      exit 1
    fi
  done

  echo "-- Installing cloned files into $APP_DIR..."
  pct exec "$CTID" -- bash -c "cp /tmp/frigate-src/main.py '$APP_DIR/main.py'"
  pct exec "$CTID" -- bash -c "cp /tmp/frigate-src/requirements.txt '$APP_DIR/requirements.txt'"
  pct exec "$CTID" -- bash -c "[ -f /tmp/frigate-src/.env.example ] && cp /tmp/frigate-src/.env.example '$APP_DIR/.env.example' || true"
  pct exec "$CTID" -- bash -c "if [ -f /tmp/frigate-src/frigate-gdrive-uploader.service ]; then cp /tmp/frigate-src/frigate-gdrive-uploader.service /etc/systemd/system/frigate-gdrive-uploader.service; fi"
  # Status dashboard is optional - deploy it if present, but don't fail the
  # whole install if an older repo doesn't have it yet.
  pct exec "$CTID" -- bash -c "[ -f /tmp/frigate-src/status_dashboard.py ] && cp /tmp/frigate-src/status_dashboard.py '$APP_DIR/status_dashboard.py' || true"
  pct exec "$CTID" -- bash -c "if [ -f /tmp/frigate-src/frigate-gdrive-dashboard.service ]; then cp /tmp/frigate-src/frigate-gdrive-dashboard.service /etc/systemd/system/frigate-gdrive-dashboard.service; fi"
  pct exec "$CTID" -- rm -rf /tmp/frigate-src

  # If the repo didn't include a service file or .env.example, fall back to
  # the ones bundled alongside this script - but only if this script is
  # actually running from a real file (not piped in via `curl | bash`,
  # where there's no "alongside this script" to fall back to).
  SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
  SCRIPT_DIR=""
  if [ -n "$SCRIPT_SOURCE" ] && [ -f "$SCRIPT_SOURCE" ]; then
    SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd)"
  fi

  if ! pct exec "$CTID" -- test -f "/etc/systemd/system/frigate-gdrive-uploader.service"; then
    if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/frigate-gdrive-uploader.service" ]; then
      pct push "$CTID" "$SCRIPT_DIR/frigate-gdrive-uploader.service" "/etc/systemd/system/frigate-gdrive-uploader.service"
    else
      echo "ERROR: no systemd service file found in the repo, and none available locally to fall back to." >&2
      echo "Add frigate-gdrive-uploader.service to the repo root at $GIT_REPO_URL." >&2
      exit 1
    fi
  fi
  if ! pct exec "$CTID" -- test -f "$APP_DIR/.env.example"; then
    if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/.env.example" ]; then
      pct push "$CTID" "$SCRIPT_DIR/.env.example" "$APP_DIR/.env.example"
    fi
  fi
else
  echo "-- Locating application files..."
  # This script expects main.py, requirements.txt, and .env.example to be
  # present alongside it (i.e. copy the whole delivered folder to the Proxmox
  # host, don't cherry-pick this file alone) - UNLESS you set GIT_REPO_URL,
  # in which case none of this local-file lookup applies.
  SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
  if [ -z "$SCRIPT_SOURCE" ] || [ ! -f "$SCRIPT_SOURCE" ]; then
    echo "ERROR: no GIT_REPO_URL was set, and this script isn't running from a real" >&2
    echo "file (e.g. it was piped in via 'curl ... | bash'), so there's nowhere to" >&2
    echo "look for main.py/requirements.txt locally." >&2
    echo "Fix: set GIT_REPO_URL to your repo, e.g.:" >&2
    echo "  curl -fsSL <script-url> | GIT_REPO_URL=https://github.com/you/repo.git bash" >&2
    exit 1
  fi
  SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd)"

  for f in main.py requirements.txt .env.example frigate-gdrive-uploader.service; do
    if [ ! -f "$SCRIPT_DIR/$f" ]; then
      echo "ERROR: expected file '$f' next to this script but it wasn't found." >&2
      echo "Copy the entire frigate-gdrive-uploader folder to the Proxmox host and run this script from inside it," >&2
      echo "or set GIT_REPO_URL to clone from your own repo instead." >&2
      exit 1
    fi
  done

  echo "-- Pushing application files into container..."
  pct push "$CTID" "$SCRIPT_DIR/main.py" "$APP_DIR/main.py"
  pct push "$CTID" "$SCRIPT_DIR/requirements.txt" "$APP_DIR/requirements.txt"
  pct push "$CTID" "$SCRIPT_DIR/.env.example" "$APP_DIR/.env.example"
  pct push "$CTID" "$SCRIPT_DIR/frigate-gdrive-uploader.service" "/etc/systemd/system/frigate-gdrive-uploader.service"
  # Status dashboard is optional - push it if present locally.
  if [ -f "$SCRIPT_DIR/status_dashboard.py" ]; then
    pct push "$CTID" "$SCRIPT_DIR/status_dashboard.py" "$APP_DIR/status_dashboard.py"
  fi
  if [ -f "$SCRIPT_DIR/frigate-gdrive-dashboard.service" ]; then
    pct push "$CTID" "$SCRIPT_DIR/frigate-gdrive-dashboard.service" "/etc/systemd/system/frigate-gdrive-dashboard.service"
  fi
fi

# --------------------------------------------------------------------------
# 5. Python venv + dependencies
# --------------------------------------------------------------------------
echo "-- Setting up Python virtual environment..."
pct exec "$CTID" -- bash -c "cd $APP_DIR && python3 -m venv venv && ./venv/bin/pip install -q --upgrade pip && ./venv/bin/pip install -q -r requirements.txt"

# --------------------------------------------------------------------------
# 6. Prepare .env from template (user still needs to fill in real values)
# --------------------------------------------------------------------------
pct exec "$CTID" -- bash -c "[ -f $APP_DIR/.env ] || cp $APP_DIR/.env.example $APP_DIR/.env"

# --------------------------------------------------------------------------
# 7. Fix ownership and register the systemd service (not started yet)
# --------------------------------------------------------------------------
pct exec "$CTID" -- bash -c "chown -R $SERVICE_USER:$SERVICE_USER $APP_DIR"
pct exec "$CTID" -- bash -c "sed -i 's#WorkingDirectory=.*#WorkingDirectory=$APP_DIR#; s#ExecStart=.*#ExecStart=$APP_DIR/venv/bin/python3 $APP_DIR/main.py#' /etc/systemd/system/frigate-gdrive-uploader.service"
pct exec "$CTID" -- systemctl daemon-reload
pct exec "$CTID" -- systemctl enable frigate-gdrive-uploader.service >/dev/null

# Status dashboard is read-only and handles a missing DB/config gracefully,
# so unlike the main service, it's safe to enable AND start immediately.
DASHBOARD_DEPLOYED=0
if pct exec "$CTID" -- test -f "$APP_DIR/status_dashboard.py" && pct exec "$CTID" -- test -f "/etc/systemd/system/frigate-gdrive-dashboard.service"; then
  pct exec "$CTID" -- bash -c "sed -i 's#WorkingDirectory=.*#WorkingDirectory=$APP_DIR#; s#ExecStart=.*#ExecStart=$APP_DIR/venv/bin/python3 $APP_DIR/status_dashboard.py#' /etc/systemd/system/frigate-gdrive-dashboard.service"
  pct exec "$CTID" -- systemctl daemon-reload
  pct exec "$CTID" -- systemctl enable --now frigate-gdrive-dashboard.service >/dev/null
  DASHBOARD_DEPLOYED=1
fi

CT_IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}' || echo "unknown")

if [ "$DASHBOARD_DEPLOYED" -eq 1 ]; then
  DASHBOARD_NOTE="
Status dashboard is already running: http://${CT_IP}:8080
(shows upload stats, service health, and lets you download logs -
 no setup needed, but it's read-only, and has no auth by default -
 set DASHBOARD_USERNAME/DASHBOARD_PASSWORD in .env to add HTTP Basic Auth)
"
else
  DASHBOARD_NOTE=""
fi

cat <<EOF

== Container $CTID ('$HOSTNAME_CT') created and provisioned ==
IP address: ${CT_IP}
Boot order set to: $STARTUP_ORDER

The service is installed and ENABLED but NOT YET STARTED, because it
still needs your Frigate URL, Drive folder ID, and credentials.

Finish setup:

  1. Push your credentials in (whichever matches DRIVE_AUTH_MODE in .env):
       pct push $CTID /path/to/oauth_token.json $APP_DIR/credentials/oauth_token.json
     or:
       pct push $CTID /path/to/service_account.json $APP_DIR/credentials/service_account.json

     Then fix ownership (pushed files land owned by root):
       pct exec $CTID -- chown -R frigate-uploader:frigate-uploader $APP_DIR/credentials

  2. Edit the config:
       pct exec $CTID -- nano $APP_DIR/.env
     At minimum set:
       FRIGATE_URL=http://<your-frigate-ip>:5000
       DRIVE_ROOT_FOLDER_ID=<your Drive folder ID>
       DRIVE_AUTH_MODE=oauth_user   (personal Gmail) or service_account (Workspace)

  3. Verify everything before starting (checks Frigate connectivity,
     Drive credentials, folder access, and timezone in one command):
       pct exec $CTID -- $APP_DIR/venv/bin/python3 $APP_DIR/main.py --check
     If Frigate shows "Connection refused," your Frigate setup likely
     only publishes port 8971 - see README, "Before you start," item 2.

  4. Start it:
       pct exec $CTID -- systemctl start frigate-gdrive-uploader

  5. Check it's working:
       pct exec $CTID -- journalctl -u frigate-gdrive-uploader -f

  6. If Frigate runs in another Proxmox guest on this same host, set its
     boot order so it starts (and has time to come up) before this
     container does, to avoid "Connection refused" right after a host
     reboot:
       pct set <frigate-guest-id> -startup order=1,up=120
     (This container is already set to order=2 by default.)
$DASHBOARD_NOTE
EOF
