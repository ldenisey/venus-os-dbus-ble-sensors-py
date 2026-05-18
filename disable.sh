#!/bin/bash
#
# Disable dbus-ble-sensors-py (curl install)
# Stops the service, removes symlinks, cleans rc.local, and restores stock service.
# Does NOT remove /data/apps/dbus-ble-sensors-py or user settings.
#

set -e

INSTALL_DIR="/data/apps/dbus-ble-sensors-py"
SERVICE_NAME="dbus-ble-sensors-py"
LAUNCHER_NAME="dbus-ble-sensors-py-launcher"

echo ""
echo "Disabling $SERVICE_NAME..."

# --- Stop and remove service symlinks ---

for svc_name in "$SERVICE_NAME" "$LAUNCHER_NAME"; do
    if [ -e "/service/$svc_name" ]; then
        svc -d "/service/$svc_name" 2>/dev/null || true
    fi
done
sleep 1

for svc_name in "$SERVICE_NAME" "$LAUNCHER_NAME"; do
    rm -rf "/service/$svc_name" 2>/dev/null || true
done

pkill -f "supervise $SERVICE_NAME" 2>/dev/null || true
pkill -f "supervise $LAUNCHER_NAME" 2>/dev/null || true
pkill -f "multilog .* /var/log/$SERVICE_NAME" 2>/dev/null || true
pkill -f "multilog .* /var/log/$LAUNCHER_NAME" 2>/dev/null || true
pkill -f "python.*dbus_ble_sensors" 2>/dev/null || true

echo "  Services stopped and symlinks removed"

# --- Clean rc.local ---

if [ -f /data/rc.local ]; then
    sed -i "/.*dbus-ble-sensors-py.*/d" /data/rc.local 2>/dev/null || true
    echo "  rc.local cleaned"
fi

# --- Restore stock dbus-ble-sensors ---

STOCK_START="/opt/victronenergy/dbus-ble-sensors/start-ble-sensors.sh"

if [ -f "$STOCK_START" ]; then
    /opt/victronenergy/swupdate-scripts/remount-rw.sh 2>/dev/null || true

    sed -i 's|^#exec |exec |g' "$STOCK_START"
    sed -i '/^svc -d ./d' "$STOCK_START"
    echo "  Stock start script restored"
fi

BT_CONFIG="/lib/udev/bt-config"
BT_REMOVE="/lib/udev/bt-remove"

if [ -f "$BT_CONFIG" ] && grep -q "dbus-ble-sensors-py" "$BT_CONFIG"; then
    /opt/victronenergy/swupdate-scripts/remount-rw.sh 2>/dev/null || true
    sed -i 's|/service/dbus-ble-sensors-py |/service/dbus-ble-sensors |g' "$BT_CONFIG"
    echo "  bt-config restored"
fi

if [ -f "$BT_REMOVE" ] && grep -q "dbus-ble-sensors-py" "$BT_REMOVE"; then
    /opt/victronenergy/swupdate-scripts/remount-rw.sh 2>/dev/null || true
    sed -i '\|^ *svc -d /service/dbus-ble-sensors-py *$|d' "$BT_REMOVE"
    echo "  bt-remove restored"
fi

# Restart stock service if bluetooth hardware is present
if [ -n "$(ls /sys/class/bluetooth 2>/dev/null)" ]; then
    svc -u /service/dbus-ble-sensors 2>/dev/null || true
    echo "  Stock dbus-ble-sensors restarted"
fi

echo ""
echo "$SERVICE_NAME disabled. Stock BLE service restored."
echo ""
echo "Note: Device settings are preserved in com.victronenergy.settings."
echo "      To completely remove: rm -rf $INSTALL_DIR"
echo ""
