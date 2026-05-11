#!/bin/bash
#
# Re-enable dbus-ble-sensors-py (curl install)
# Repairs permissions, recreates service symlinks, updates rc.local.
# Useful for recovery after a firmware update or if services go missing.
#

set -e

INSTALL_DIR="/data/apps/dbus-ble-sensors-py"
SERVICE_NAME="dbus-ble-sensors-py"
LAUNCHER_NAME="dbus-ble-sensors-py-launcher"
APP_DIR="$INSTALL_DIR/src/opt/victronenergy/dbus-ble-sensors-py"

echo ""
echo "Re-enabling $SERVICE_NAME..."

if [ ! -d "$INSTALL_DIR" ]; then
    echo "Error: $INSTALL_DIR not found. Run install.sh first."
    exit 1
fi

# --- Fix permissions ---

chmod +x "$INSTALL_DIR"/service/run 2>/dev/null || true
chmod +x "$INSTALL_DIR"/service/log/run 2>/dev/null || true
chmod +x "$INSTALL_DIR"/service-launcher/run 2>/dev/null || true
chmod +x "$INSTALL_DIR"/service-launcher/log/run 2>/dev/null || true
chmod +x "$INSTALL_DIR"/*.sh 2>/dev/null || true
echo "  Permissions fixed"

# --- Disable stock service ---

STOCK_START="/opt/victronenergy/dbus-ble-sensors/start-ble-sensors.sh"
BT_CONFIG="/lib/udev/bt-config"
BT_REMOVE="/lib/udev/bt-remove"

if [ -f "$STOCK_START" ] && grep -q "^exec " "$STOCK_START"; then
    /opt/victronenergy/swupdate-scripts/remount-rw.sh 2>/dev/null || true
    sed -i 's|^exec |#exec |g' "$STOCK_START"
    sed -i '\|--banner|a\
svc -d .' "$STOCK_START"
    echo "  Stock start script disabled"
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

if [ -n "$(ls /sys/class/bluetooth 2>/dev/null)" ]; then
    svc -d /service/dbus-ble-sensors 2>/dev/null || true
fi

# --- Stop any stale service entries before recreating ---

for svc_name in "$SERVICE_NAME" "$LAUNCHER_NAME"; do
    if [ -e "/service/$svc_name" ]; then
        svc -d "/service/$svc_name" 2>/dev/null || true
    fi
done
sleep 1

# Remove stale symlinks or directories
for svc_name in "$SERVICE_NAME" "$LAUNCHER_NAME"; do
    rm -rf "/service/$svc_name" 2>/dev/null || true
done

pkill -f "supervise $SERVICE_NAME" 2>/dev/null || true
pkill -f "supervise $LAUNCHER_NAME" 2>/dev/null || true
pkill -f "python.*dbus_ble_sensors" 2>/dev/null || true

# --- Create service symlinks ---

ln -s "$INSTALL_DIR/service" "/service/$SERVICE_NAME"
ln -s "$INSTALL_DIR/service-launcher" "/service/$LAUNCHER_NAME"
echo "  Service symlinks created"

# --- Ensure rc.local persistence ---

RC_LOCAL="/data/rc.local"
if [ ! -f "$RC_LOCAL" ]; then
    echo "#!/bin/bash" > "$RC_LOCAL"
    chmod 755 "$RC_LOCAL"
fi

RC_ENTRY="bash $INSTALL_DIR/enable.sh > $INSTALL_DIR/startup.log 2>&1 &"
if ! grep -qF "dbus-ble-sensors-py" "$RC_LOCAL"; then
    echo "$RC_ENTRY" >> "$RC_LOCAL"
    echo "  Added to rc.local"
fi

# --- Start services ---

svc -u "/service/$LAUNCHER_NAME" 2>/dev/null || true
sleep 2
echo "  Services started"

echo ""
echo "$SERVICE_NAME enabled."
echo ""
svstat "/service/$SERVICE_NAME" 2>/dev/null || true
svstat "/service/$LAUNCHER_NAME" 2>/dev/null || true
echo ""
