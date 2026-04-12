"""
SeeLevel 709-BT Protocol Reference
====================================

The Garnet SeeLevel 709-BT is a BLE Broadcaster that continuously cycles
through its connected sensors, transmitting advertisement packets.  No BLE
connection is required -- all data is read from manufacturer-specific data
in the advertisement PDU.

Two hardware variants exist, distinguished by manufacturer ID:

  BTP3 (Cypress)   MFG ID 305  (0x0131)
  BTP7 (SeeLevel)  MFG ID 3264 (0x0CC0)


BLE Packet Formats
------------------

BTP3 payload (14 bytes):
    Bytes 0-2   Coach ID (24-bit, little-endian)
    Byte  3     Sensor number (0-13)
    Bytes 4-6   Sensor data (3 ASCII chars: "OPN", "ERR", or numeric)
    Bytes 7-9   Volume in gallons (3 ASCII chars)
    Bytes 10-12 Total capacity in gallons (3 ASCII chars)
    Byte  13    Alarm state (ASCII digit '0'-'9')

BTP7 payload (14 bytes):
    Bytes 0-2   Coach ID (24-bit, little-endian)
    Bytes 3-10  Tank levels, 1 byte each (0-100 = %, >100 = error code)
                Order: Fresh, Wash, Toilet, Fresh2, Wash2, Toilet2, Wash3, LPG
    Byte  11    Battery voltage * 10 (e.g. 130 = 13.0 V)
    Bytes 12-13 Unused


Sensor Numbers
--------------

    Number  BTP3 (0x0131)       BTP7 (0x0CC0)
    ------  ----------------    ----------------
      0     Fresh Water         Fresh Water
      1     Toilet Water        Wash Water
      2     Wash Water          Toilet Water
      3     LPG                 Fresh Water 2
      4     LPG 2               Wash Water 2
      5     Galley Water        Toilet Water 2
      6     Galley Water 2      Wash Water 3
      7     Temperature         LPG
      8     Temperature 2       Battery (voltage * 10)
      9     Temperature 3       -
     10     Temperature 4       -
     11     Chemical            -
     12     Chemical 2          -
     13     Battery (voltage*10) -


Status / Error Codes
--------------------

BTP3: the 3-char data field (bytes 4-6) may contain:
    "OPN"   Sensor open / disconnected (no service created)
    "ERR"   Sensor error (service shown with error status)
    Numeric Actual sensor reading

BTP7: tank byte values > 100 are error codes:
    101  Short Circuit
    102  Open / No response
    103  Bitcount error
    104  Non-stacked config with stacked data
    105  Stacked, missing bottom sender
    106  Stacked, missing top sender
    108  Bad Checksum
    110  Tank disabled
    111  Tank init


Unit Conversions
----------------

    Tank Level      Direct percentage (0-100)
    Temperature     (°F - 32) * 5/9 = °C
    Battery Voltage Value / 10 = Volts
    Tank Capacity   Gallons * 0.00378541 = m³  (if volume desired)


Victron D-Bus Mappings
----------------------

Product IDs:
    Tank Sensor         0xA142  (VE_PROD_ID_TANK_SENSOR)
    Temperature Sensor  0xA143  (VE_PROD_ID_TEMPERATURE_SENSOR)
    Battery Monitor     0xA381  (VE_PROD_ID_BATTERY_MONITOR)

Fluid Types:
    Fresh Water  1  (FLUID_TYPE_FRESH_WATER)
    Wash Water   2  (FLUID_TYPE_WASTE_WATER)
    Toilet Water 5  (FLUID_TYPE_BLACK_WATER)
    LPG          8  (FLUID_TYPE_LPG)
    Chemical     0  (custom)


References
----------

- Protocol decode: https://github.com/custom-components/ble_monitor/issues/1181
- BTP7 btmon capture: https://github.com/TechBlueprints/victron-seelevel-python/issues/1
- BTP7 PR discussion: https://github.com/TechBlueprints/victron-seelevel-python/pull/2
- Original Python impl: https://github.com/TechBlueprints/victron-seelevel-python
"""

from ve_types import *
from ble_device import BleDevice
import logging


class BleDeviceSeeLevel(BleDevice):
    """
    Shared base class for SeeLevel 709-BT protocols.

    Both BTP3 (manufacturer 305) and BTP7 (manufacturer 3264) share a common
    advertisement header, tank level processing, and error handling.  Subclasses
    provide protocol-specific parsing in handle_manufacturer_data().

    See module docstring for the full protocol reference.
    """

    CUSTOM_PARSING = True

    STATUS_CODES = {
        101: "Short Circuit",
        102: "Open",
        103: "Bitcount error",
        104: "Non-stacked config with stacked data",
        105: "Stacked, missing bottom sender",
        106: "Stacked, missing top sender",
        108: "Bad Checksum",
        110: "Tank disabled",
        111: "Tank init",
    }

    def configure(self, manufacturer_data: bytes):
        self.info.update({
            'product_id': 0xA142,
            'product_name': self.PRODUCT_NAME,
            'device_name': 'SeeLevel',
            'dev_prefix': self.DEV_PREFIX,
            'roles': dict(self.ROLES),
            'regs': [],
        })

    def _parse_coach_id(self, manufacturer_data: bytes) -> int:
        """Parse 24-bit little-endian coach ID from bytes 0-2."""
        return int.from_bytes(manufacturer_data[0:3], byteorder='little')

    def _build_tank_sensor_data(self, level: int, role_service) -> dict:
        """Build D-Bus sensor data dict from a tank level percentage (0-100)."""
        capacity = float(role_service['Capacity'] or 0)
        remaining = round(capacity * level / 100.0, 3) if capacity else 0.0

        return {
            'RawValue': float(level),
            'Level': level,
            'Remaining': remaining,
            'Status': 0,
        }

    def _set_error_status(self, role_service, error_code=None):
        """Set error status on a role service. Logs the error code if known."""
        if error_code is not None:
            status_msg = self.STATUS_CODES.get(error_code, f"Unknown ({error_code})")
            logging.debug(f"{self._plog} error: {status_msg}")
        role_service['Status'] = 5
        role_service.connect()
