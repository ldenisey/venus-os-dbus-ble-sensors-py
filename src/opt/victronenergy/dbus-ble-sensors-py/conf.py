import os

# Project variables
PROCESS_NAME = os.path.basename(os.path.dirname(__file__))
PROCESS_VERSION = '1.1.1'

# Timeouts
IGNORED_DEVICES_TIMEOUT = 600   # 10 min
DEVICE_SERVICES_TIMEOUT = 3600  # 60 min

# Optional ``[orion] PairingPin=…`` override for Orion-TR BLE pairing (see ``orion_tr_pin.py``).
ORION_OPTIONAL_INI = "/data/conf/dbus-ble-sensors-py-orion.ini"
