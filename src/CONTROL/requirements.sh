#!/bin/bash
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")

# Getting velib_python files, as it is not available as a package...
mkdir -p "$SCRIPT_DIR/../opt/victronenergy/dbus-ble-sensors-py/ext/velib_python"
wget -O "$SCRIPT_DIR/../opt/victronenergy/dbus-ble-sensors-py/ext/velib_python/vedbus.py" https://raw.githubusercontent.com/victronenergy/velib_python/refs/heads/master/vedbus.py
wget -O "$SCRIPT_DIR/../opt/victronenergy/dbus-ble-sensors-py/ext/velib_python/logger.py" https://raw.githubusercontent.com/victronenergy/velib_python/refs/heads/master/logger.py
wget -O "$SCRIPT_DIR/../opt/victronenergy/dbus-ble-sensors-py/ext/velib_python/ve_utils.py" https://raw.githubusercontent.com/victronenergy/velib_python/refs/heads/master/ve_utils.py

# victron_ble (used by the IP22 / Orion-TR drivers for Instant Readout
# advertisement decryption) is vendored in-tree at
# ext/victron_ble/, with a patch to prefer python3-cryptography (shipped
# in Venus OS) over PyCryptodome (not available).  See
# ext/victron_ble/VENDORED.md for the rationale.
