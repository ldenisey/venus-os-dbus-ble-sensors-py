#!/bin/bash
#
# Remote installer for dbus-ble-sensors-py on Venus OS
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/ldenisey/venus-os-dbus-ble-sensors-py/main/install.sh | bash
#

set -e

REPO_URL="https://github.com/ldenisey/venus-os-dbus-ble-sensors-py.git"
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
        REMOTE=$(git rev-parse origin/main 2>/dev/null || echo "none")
        if [ "$LOCAL" != "$REMOTE" ]; then
            echo "  Updates available. Resetting to latest..."
            git checkout main 2>/dev/null || git checkout -b main origin/main
            git reset --hard origin/main
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
        git checkout -b main origin/main 2>/dev/null || git checkout main
        git reset --hard origin/main
        NEEDS_RESTART=true
        echo "  Converted to git repository"
    fi
else
    echo "  Cloning repository..."
    git clone "$REPO_URL" "$INSTALL_DIR"
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

# --- Step 6: Set up service ---

echo "Step 6: Setting up services..."

chmod +x "$INSTALL_DIR"/service/run 2>/dev/null || true
chmod +x "$INSTALL_DIR"/service/log/run 2>/dev/null || true
chmod +x "$INSTALL_DIR"/service-launcher/run 2>/dev/null || true
chmod +x "$INSTALL_DIR"/service-launcher/log/run 2>/dev/null || true
chmod +x "$INSTALL_DIR"/*.sh 2>/dev/null || true

# Create or update service symlinks
for svc_name in "$SERVICE_NAME" "$LAUNCHER_NAME"; do
    link="/service/$svc_name"
    if [ "$svc_name" = "$SERVICE_NAME" ]; then
        target="$INSTALL_DIR/service"
    else
        target="$INSTALL_DIR/service-launcher"
    fi

    if [ -L "$link" ]; then
        rm "$link"
    elif [ -d "$link" ]; then
        svc -d "$link" 2>/dev/null || true
        sleep 1
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

# --- Step 8: Start or restart ---

echo "Step 8: Starting services..."

if [ "$NEEDS_RESTART" = true ] || ! svstat "/service/$SERVICE_NAME" 2>/dev/null | grep -q "up"; then
    svc -u "/service/$LAUNCHER_NAME" 2>/dev/null || true
    sleep 3

    if svstat "/service/$SERVICE_NAME" 2>/dev/null | grep -q "up"; then
        echo "  Service started successfully"
    else
        echo "  Service may still be starting (launcher controls lifecycle)"
    fi
else
    svc -t "/service/$SERVICE_NAME" 2>/dev/null || true
    sleep 2
    echo "  Service restarted"
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
