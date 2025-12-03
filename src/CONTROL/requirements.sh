#!/bin/bash
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")

# Getting velib_python files, as it is not available as a package...
mkdir -p "$SCRIPT_DIR/../opt/victronenergy/dbus-ble-sensors-py/ext/velib_python"
wget -O "$SCRIPT_DIR/../opt/victronenergy/dbus-ble-sensors-py/ext/velib_python/vedbus.py" https://raw.githubusercontent.com/victronenergy/velib_python/refs/heads/master/vedbus.py
wget -O "$SCRIPT_DIR/../opt/victronenergy/dbus-ble-sensors-py/ext/velib_python/logger.py" https://raw.githubusercontent.com/victronenergy/velib_python/refs/heads/master/logger.py
wget -O "$SCRIPT_DIR/../opt/victronenergy/dbus-ble-sensors-py/ext/velib_python/ve_utils.py" https://raw.githubusercontent.com/victronenergy/velib_python/refs/heads/master/ve_utils.py

# Downloading packages
export SKIP_CYTHON=false; pip3 install bleak --no-deps --target "$SCRIPT_DIR/../opt/victronenergy/dbus-ble-sensors-py/ext/"
pip3 install gbulb --no-deps --target "$SCRIPT_DIR/../opt/victronenergy/dbus-ble-sensors-py/ext/"
