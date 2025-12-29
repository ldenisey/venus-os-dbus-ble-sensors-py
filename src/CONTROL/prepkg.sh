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

# Set process version from control file
VERSION=$(cat "$SCRIPT_DIR"/control | grep Version | sed -e "s/Version:\s*\(\d*\)\D*/\1/")
sed -i "s/^\([[:space:]]*PROCESS_VERSION[[:space:]]*=[[:space:]]*\)\(['\"]\)[^'\"]*\(['\"]\)/\1\2$VERSION\3/" "$SCRIPT_DIR/../opt/victronenergy/dbus-ble-sensors-py/conf.py"

# Update manufacturer name file
wget -O /tmp/manuf.yaml https://bitbucket.org/bluetooth-SIG/public/raw/main/assigned_numbers/company_identifiers/company_identifiers.yaml
MAN_FILE="$SCRIPT_DIR/../opt/victronenergy/dbus-ble-sensors-py/man_id.py"
echo '# Generated file, do not modify' > $MAN_FILE
echo 'MAN_NAMES = {' >> $MAN_FILE
awk '
/value:/ { id = $3; next }
/name: / {
  sq1 = index($0, "'\''")
  if (sq1 > 0) {
    sq2 = index(substr($0, sq1 + 1), "'\''")
    if (sq2 > 0) {
      name = substr($0, sq1 + 1, sq2 - 1)
      print "    " id ": '\''" name "'\'',"
    }
  }
}
' /tmp/manuf.yaml >> $MAN_FILE
echo '}' >> $MAN_FILE