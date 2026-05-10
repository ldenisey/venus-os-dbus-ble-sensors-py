#!/bin/bash
#
# Remote installer for dbus-ble-sensors-py on Venus OS
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/TechBlueprints/venus-os-dbus-ble-sensors-py/main/install.sh | bash
#

set -e

REPO_URL="https://github.com/TechBlueprints/venus-os-dbus-ble-sensors-py.git"
BRANCH="main"
INSTALL_DIR="/data/apps/dbus-ble-sensors-py"
SERVICE_NAME="dbus-ble-sensors-py"
LAUNCHER_NAME="dbus-ble-sensors-py-launcher"
APP_DIR="src/opt/victronenergy/dbus-ble-sensors-py"
VELIB_URL="https://raw.githubusercontent.com/victronenergy/velib_python/refs/heads/master"

echo "========================================"
echo " dbus-ble-sensors-py Installer"
echo "========================================"
echo ""

# --- Preflight checks ---

if [ ! -d "/data" ]; then
    echo "Error: /data not found. This script must run on Venus OS."
    exit 1
fi
mkdir -p /data/apps

# --- Step 1: Detect and remove opkg installation ---

echo "Step 1: Checking for existing installations..."

if opkg list-installed 2>/dev/null | grep -q "^dbus-ble-sensors-py "; then
    echo "  Detected opkg installation. Removing to switch to curl-managed install..."
    echo "  Note: All device settings and configurations are preserved."
    opkg remove dbus-ble-sensors-py
    echo "  opkg package removed"
fi
echo ""

# --- Step 2: Ensure git is available ---
#
# Venus OS ships with rootfs (/) mounted read-only.  When git is missing
# we temporarily remount rw, install via opkg, and revert to ro.  An EXIT
# trap guarantees the rootfs is restored to ro even if opkg fails, the
# script is interrupted, or a later step exits non-zero.

echo "Step 2: Checking for git..."
if ! command -v git >/dev/null 2>&1; then
    echo "  Git not found. Installing via opkg (temporary remount,rw)..."

    # Only remount if rootfs is currently mounted ro.  On a system that
    # already happens to have / mounted rw (developer device, etc.) we
    # leave the mount state alone.
    if grep -qE '^[^ ]+ / [^ ]+ ro[, ]' /proc/mounts; then
        if ! mount -o remount,rw /; then
            echo "Error: Could not remount / read-write to install git."
            exit 1
        fi
        # Restore ro on any exit path (success, opkg failure, ^C, later
        # step failure).  Mount may already be back to ro by then; that's
        # fine, the second remount is a no-op.
        trap 'mount -o remount,ro / 2>/dev/null || true' EXIT
        REMOUNTED_FOR_GIT=true
    else
        REMOUNTED_FOR_GIT=false
    fi

    if ! opkg update; then
        echo "Error: opkg update failed.  Check the device's network."
        exit 1
    fi
    if ! opkg install git; then
        echo "Error: Failed to install git via opkg."
        exit 1
    fi

    if [ "$REMOUNTED_FOR_GIT" = true ]; then
        mount -o remount,ro / 2>/dev/null || true
        trap - EXIT
    fi
    echo "  Git installed (rootfs returned to read-only)"
else
    echo "  Git already available"
fi
echo ""

# --- Step 3: Clone or update repository ---

echo "Step 3: Setting up repository..."

NEEDS_RESTART=false

if [ -d "$INSTALL_DIR" ]; then
    cd "$INSTALL_DIR"
    git config --global --add safe.directory "$INSTALL_DIR" 2>/dev/null || true

    if [ -d .git ]; then
        CURRENT_REMOTE=$(git remote get-url origin 2>/dev/null || echo "")
        if [ "$CURRENT_REMOTE" != "$REPO_URL" ]; then
            git remote set-url origin "$REPO_URL" 2>/dev/null || git remote add origin "$REPO_URL"
        fi
        echo "  Fetching latest changes..."
        git fetch origin
        LOCAL=$(git rev-parse HEAD 2>/dev/null || echo "none")
        REMOTE=$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo "none")
        if [ "$LOCAL" != "$REMOTE" ]; then
            echo "  Updates available. Resetting to latest..."
            git checkout "$BRANCH" 2>/dev/null || git checkout -b "$BRANCH" "origin/$BRANCH"
            git reset --hard "origin/$BRANCH"
            NEEDS_RESTART=true
            echo "  Repository updated"
        else
            echo "  Already up to date"
        fi
    else
        echo "  Not a git repository. Converting..."
        git init
        git remote add origin "$REPO_URL"
        git fetch origin
        git checkout -b "$BRANCH" "origin/$BRANCH" 2>/dev/null || git checkout "$BRANCH"
        git reset --hard "origin/$BRANCH"
        NEEDS_RESTART=true
        echo "  Converted to git repository"
    fi
else
    echo "  Cloning repository..."
    git clone -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    git config --global --add safe.directory "$INSTALL_DIR" 2>/dev/null || true
    echo "  Repository cloned"
fi
echo ""

# --- Step 4: Fetch velib_python ---

echo "Step 4: Fetching velib_python dependencies..."
VELIB_DIR="$INSTALL_DIR/$APP_DIR/ext/velib_python"
mkdir -p "$VELIB_DIR"
for f in vedbus.py logger.py ve_utils.py; do
    wget -q -O "$VELIB_DIR/$f" "$VELIB_URL/$f"
done
echo "  velib_python fetched"
echo ""

# --- Step 5: Disable stock dbus-ble-sensors ---

echo "Step 5: Disabling stock dbus-ble-sensors..."

STOCK_START="/opt/victronenergy/dbus-ble-sensors/start-ble-sensors.sh"
BT_CONFIG="/lib/udev/bt-config"
BT_REMOVE="/lib/udev/bt-remove"

disable_stock_service() {
    if [ -f "$STOCK_START" ]; then
        if grep -q "^exec " "$STOCK_START"; then
            /opt/victronenergy/swupdate-scripts/remount-rw.sh 2>/dev/null || true
            sed -i 's|^exec |#exec |g' "$STOCK_START"
            sed -i '\|--banner|a\
svc -d .' "$STOCK_START"
            echo "  Stock start script disabled"
        else
            echo "  Stock start script already disabled"
        fi

        if [ -n "$(ls /sys/class/bluetooth 2>/dev/null)" ]; then
            svc -d /service/dbus-ble-sensors 2>/dev/null || true
        fi
    else
        echo "  Stock service not found (may not be installed)"
    fi

    if [ -f "$BT_CONFIG" ] && grep -q "/service/dbus-ble-sensors " "$BT_CONFIG"; then
        /opt/victronenergy/swupdate-scripts/remount-rw.sh 2>/dev/null || true
        sed -i 's|/service/dbus-ble-sensors |/service/dbus-ble-sensors-py |g' "$BT_CONFIG"
        echo "  bt-config patched"
    fi

    if [ -f "$BT_REMOVE" ] && ! grep -q "dbus-ble-sensors-py" "$BT_REMOVE"; then
        /opt/victronenergy/swupdate-scripts/remount-rw.sh 2>/dev/null || true
        sed -i '\|/service/dbus-ble-sensors$|a\
    svc -d /service/dbus-ble-sensors-py' "$BT_REMOVE"
        echo "  bt-remove patched"
    fi
}

disable_stock_service
echo ""

# --- Step 5.5: Apply vesmart-server safety patch ---
#
# Victron's vesmart-server has a hardcoded 60-second timer that, when
# any BLE device connects, disconnects EVERY connected BLE device on
# EVERY adapter -- including ours.  This is upstream bug
# https://github.com/victronenergy/venus/issues/1587 and it makes
# stable BLE scanning impossible without a patch.
#
# We vendor TechBlueprints/victron-bluetooth-safety into
# ext/victron-bluetooth-safety/ and run its installer in `--mode patch`.
# The fix is applied entirely with `mount --bind` -- the rootfs is
# never modified -- and a `/data/rc.local` boot hook re-establishes it
# on every reboot (including after Venus OS firmware updates).
#
# See ext/victron-bluetooth-safety/VENDORED.md for the design rationale
# and the alternative `--mode disable` for setups that don't use
# VictronConnect over BLE.

echo "Step 5.5: Applying vesmart-server safety patch..."

VBS_SRC="$INSTALL_DIR/$APP_DIR/ext/victron-bluetooth-safety"
VBS_DEST="/data/victron-bluetooth-safety"

if [ ! -f "$VBS_SRC/victron-bluetooth-safety.sh" ]; then
    echo "  WARN: vendored ext/victron-bluetooth-safety/ not found — skipping"
    echo "        ($VBS_SRC/victron-bluetooth-safety.sh missing)"
else
    mkdir -p "$VBS_DEST"
    cp "$VBS_SRC/victron-bluetooth-safety.sh" "$VBS_DEST/"
    cp "$VBS_SRC/patcher.py"                  "$VBS_DEST/"
    cp "$VBS_SRC/noop-run"                    "$VBS_DEST/"
    chmod +x "$VBS_DEST/victron-bluetooth-safety.sh" \
             "$VBS_DEST/patcher.py" \
             "$VBS_DEST/noop-run"

    # Run the unified installer.  It writes the /data/rc.local hook,
    # bind-mounts a patched gattserver.py over the upstream file, and
    # restarts vesmart-server.  Idempotent on re-run.
    sh "$VBS_DEST/victron-bluetooth-safety.sh" install --mode patch 2>&1 | sed 's/^/  /'

    # Verify the bind mount is active.
    if awk '$2 == "/opt/victronenergy/vesmart-server/gattserver.py" {found=1} END {exit !found}' /proc/mounts; then
        echo "  vesmart-server patched (bind mount active)"
    else
        echo "  WARN: vesmart-server is NOT patched (60s mass-disconnect still active)"
        echo "        BLE scans will be disrupted every minute until patched."
        echo "        Check: sh $VBS_DEST/victron-bluetooth-safety.sh status"
    fi
fi
echo ""

# --- Step 6: Set up service ---

echo "Step 6: Setting up services..."

chmod +x "$INSTALL_DIR"/service/run 2>/dev/null || true
chmod +x "$INSTALL_DIR"/service/log/run 2>/dev/null || true
chmod +x "$INSTALL_DIR"/service-launcher/run 2>/dev/null || true
chmod +x "$INSTALL_DIR"/service-launcher/log/run 2>/dev/null || true
chmod +x "$INSTALL_DIR"/*.sh 2>/dev/null || true

# Take down a service tree cleanly before we replace it.
#
# daemontools normally responds to `svc -d` on the parent service, but the
# log sub-service runs its own `multilog` process which holds an fcntl lock
# on /var/log/$svc_name/.  If we just `rm -rf /service/$svc_name` while
# multilog is still alive, the lock survives and the *new* multilog can
# never start, leaving the new service running with no log output.
#
# Belt and suspenders:
#   1. svc -d the log service first so multilog gets SIGTERM
#   2. svc -d the parent service
#   3. pkill any multilog whose argv references our log dir, in case the
#      log service entry was missing/broken (i.e. supervise wasn't
#      managing it any more so svc -d was a no-op)
stop_service_tree() {
    local svc_name="$1"
    local link="/service/$svc_name"
    local log_dir="/var/log/$svc_name"

    if [ -e "$link" ]; then
        if [ -d "$link/log" ] || [ -L "$link/log" ]; then
            svc -d "$link/log" 2>/dev/null || true
        fi
        svc -d "$link" 2>/dev/null || true
        sleep 1
    fi

    if pgrep -f "multilog .* $log_dir\$" >/dev/null 2>&1; then
        pkill -f "multilog .* $log_dir\$" 2>/dev/null || true
        sleep 1
        if pgrep -f "multilog .* $log_dir\$" >/dev/null 2>&1; then
            pkill -KILL -f "multilog .* $log_dir\$" 2>/dev/null || true
            sleep 1
        fi
    fi
}

# Create or update service symlinks
for svc_name in "$SERVICE_NAME" "$LAUNCHER_NAME"; do
    link="/service/$svc_name"
    if [ "$svc_name" = "$SERVICE_NAME" ]; then
        target="$INSTALL_DIR/service"
    else
        target="$INSTALL_DIR/service-launcher"
    fi

    stop_service_tree "$svc_name"

    if [ -L "$link" ] || [ -e "$link" ]; then
        rm -rf "$link"
    fi
    ln -s "$target" "$link"
done
echo "  Service symlinks created"
echo ""

# --- Step 7: Persist across reboots via rc.local ---

echo "Step 7: Setting up boot persistence..."

RC_LOCAL="/data/rc.local"
if [ ! -f "$RC_LOCAL" ]; then
    echo "#!/bin/bash" > "$RC_LOCAL"
    chmod 755 "$RC_LOCAL"
fi

RC_ENTRY="bash $INSTALL_DIR/enable.sh > $INSTALL_DIR/startup.log 2>&1 &"
if ! grep -qF "dbus-ble-sensors-py" "$RC_LOCAL"; then
    echo "$RC_ENTRY" >> "$RC_LOCAL"
    echo "  Added to rc.local"
else
    echo "  Already in rc.local"
fi
echo ""

# --- Step 8: Start services ---
#
# Step 6 always takes the existing service tree down (so multilog can
# release the log-dir lock) and then re-creates the /service/ symlinks.
# That means at this point the launcher and service are guaranteed to
# be down, regardless of whether the install was a fresh clone or a
# no-op upgrade.  `svstat` reports e.g. "down 5 seconds, normally up"
# so we can't pattern-match for the literal word "up" -- we look for
# "up (pid", which only appears when the service is actively running.

is_service_up() {
    svstat "$1" 2>/dev/null | grep -q "up (pid"
}

echo "Step 8: Starting services..."

# Bring up each service tree explicitly.  daemontools does not
# recursively manage the log sub-service: `svc -u /service/x` only
# starts /service/x/run, not /service/x/log.  The log sub-service is
# its own supervise instance and needs its own `svc -u` call,
# otherwise multilog never starts and the log file stays stale.
for svc_name in "$LAUNCHER_NAME" "$SERVICE_NAME"; do
    svc -u "/service/$svc_name/log" 2>/dev/null || true
done
svc -u "/service/$LAUNCHER_NAME" 2>/dev/null || true

# Wait up to 10 seconds for the worker to be supervised "up (pid ...)".
for i in 1 2 3 4 5 6 7 8 9 10; do
    if is_service_up "/service/$SERVICE_NAME"; then
        echo "  Service started successfully (after ${i}s)"
        break
    fi
    sleep 1
done

if ! is_service_up "/service/$SERVICE_NAME"; then
    echo "  Service did not come up within 10s — launcher may be"
    echo "  waiting on /Settings/Services/BleSensors=1 or hardware."
fi
echo ""

# --- Step 9: Post-install verification ---
#
# Confirms the service is actually running from $INSTALL_DIR (not a leftover
# /opt/ install) and that multilog is appending to the log file.  Surfaces
# the most common silent-fail modes that would otherwise look healthy in
# `svstat`.

echo "Step 9: Verifying installation..."

verify_install() {
    sleep 4  # give the service time to import + start logging

    local pid
    pid=$(pgrep -f "$INSTALL_DIR/.*dbus_ble_sensors.py" 2>/dev/null | head -n 1)

    if [ -z "$pid" ]; then
        echo "  WARN: no dbus_ble_sensors.py process running from $INSTALL_DIR"
        echo "        check 'tail /var/log/$SERVICE_NAME/current'"
        return 1
    fi

    echo "  Service running as pid $pid"

    local log_file="/var/log/$SERVICE_NAME/current"
    if [ ! -f "$log_file" ]; then
        echo "  WARN: log file $log_file does not exist yet"
        return 1
    fi

    local now
    local mtime
    now=$(date +%s)
    mtime=$(stat -c %Y "$log_file" 2>/dev/null || echo 0)
    local age=$((now - mtime))

    if [ "$age" -lt 60 ]; then
        echo "  Log file is fresh (last write ${age}s ago)"
    else
        echo "  WARN: log file last modified ${age}s ago"
        echo "        a stale multilog may be holding the lock; check"
        echo "        'pgrep -af multilog | grep $SERVICE_NAME'"
        return 1
    fi
}

if verify_install; then
    echo "  All checks passed"
else
    echo "  Some checks did not pass — install may still recover, but"
    echo "  inspect the log path above before relying on the install."
fi
echo ""

# --- Done ---

echo "========================================"
echo " Installation Complete!"
echo "========================================"
echo ""
echo "Service status:"
svstat "/service/$SERVICE_NAME" 2>/dev/null || echo "  (not yet supervised)"
svstat "/service/$LAUNCHER_NAME" 2>/dev/null || echo "  (not yet supervised)"
echo ""
echo "View logs:"
echo "  tail -f /var/log/$SERVICE_NAME/current | tai64nlocal"
echo ""
echo "Service management:"
echo "  svc -u /service/$SERVICE_NAME   # Start"
echo "  svc -d /service/$SERVICE_NAME   # Stop"
echo "  svc -t /service/$SERVICE_NAME   # Restart"
echo ""
echo "To disable:  bash $INSTALL_DIR/disable.sh"
echo "To remove:   bash $INSTALL_DIR/disable.sh && rm -rf $INSTALL_DIR"
echo ""
