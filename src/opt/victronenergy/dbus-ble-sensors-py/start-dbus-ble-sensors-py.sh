#!/bin/sh
#
# Start script for dbus-ble-sensors-py
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")

get_setting() {
  dbus-send --print-reply=literal --system --type=method_call \
  --dest=com.victronenergy.settings $1 com.victronenergy.BusItem.GetValue |
  awk '/int32/ { print $NF; exit }'
}

if ! ls /sys/class/bluetooth/* >/dev/null 2>&1; then
  echo "Error: No bluetooth device detected, cancelling service launch"
  svc -d .
  exit 1
fi

if [ "$(get_setting /Settings/Services/BleSensors)" != 1 ]; then
  echo "Error: Bluetooth service deactivated by configuration, cancelling service launch"
  svc -d .
  exit 1
fi

# Passive scanning requires bluetoothd with experimental features enabled.
# If bluetoothd is not running at all, start it with -E.  If it is running
# without -E, log a note (the Python code will fall back to active scanning).
if ! pidof bluetoothd > /dev/null 2>&1; then
  if [ -x /usr/libexec/bluetooth/bluetoothd ]; then
    echo "Starting bluetoothd with experimental features for passive scanning..."
    /usr/libexec/bluetooth/bluetoothd -E &
    sleep 2
  fi
elif ! cat /proc/$(pidof bluetoothd)/cmdline 2>/dev/null | tr '\0' ' ' | grep -qE '\-E|--experimental'; then
  echo "Note: bluetoothd running without --experimental; passive scanning will fall back to active"
fi

exec python3 "$SCRIPT_DIR/dbus_ble_sensors.py"
