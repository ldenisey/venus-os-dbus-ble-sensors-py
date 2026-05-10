#!/bin/sh
# Convenience wrapper: run the BLE-charger test suite from the repo root.
#
#   ./tests/run.sh            # all tests, verbose
#   ./tests/run.sh -k history # only history-related tests
#
# Sets PYTHONPATH so the shared module (``ble_charger_common``) is
# importable without installing anything.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
DRIVER="$ROOT/src/opt/victronenergy/dbus-ble-sensors-py"
EXT="$DRIVER/ext:$DRIVER/ext/velib_python"

PYTHONPATH="$DRIVER:$EXT:$HERE" \
    exec python3 -m pytest "$HERE" -v "$@"
