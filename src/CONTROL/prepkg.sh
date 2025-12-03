#!/bin/bash
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")

# Setting shell rights
chmod +x "$SCRIPT_DIR"/post*
chmod +x "$SCRIPT_DIR"/pre*
chmod +x "$SCRIPT_DIR/../opt/victronenergy/dbus-ble-sensors-py/start-dbus-ble-sensors-py.sh"
chmod +x "$SCRIPT_DIR/../opt/victronenergy/service/dbus-ble-sensors-py/run"
chmod +x "$SCRIPT_DIR/../opt/victronenergy/service/dbus-ble-sensors-py/log/run"
chmod +x "$SCRIPT_DIR/../opt/victronenergy/service/dbus-ble-sensors-py-launcher/run"
chmod +x "$SCRIPT_DIR/../opt/victronenergy/service/dbus-ble-sensors-py-launcher/log/run"

# Clean unwanted files
find "$SCRIPT_DIR/../opt/victronenergy/dbus-ble-sensors-py" -type d -name "__pycache__" -exec rm -rf {} +

# Check ext files
if [ ! -d "$SCRIPT_DIR/../opt/victronenergy/dbus-ble-sensors-py/ext" ]; then
    echo " Downloading ext files..."
    "$SCRIPT_DIR"/requirements.sh
fi