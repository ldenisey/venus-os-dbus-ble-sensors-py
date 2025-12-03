from ve_types import *
from ble_device import BleDevice


class BleDeviceGobius(BleDevice):
    """
    Gobius C device class.

    Cf.
    - https://gobiusc.com/
    - https://github.com/victronenergy/dbus-ble-sensors/blob/master/src/gobius.c
    """

    MANUFACTURER_ID = 0x0F53

    _GOBIUS_ERROR = 0xffff
    _GOBIUS_STARTUP = 0xfffe

    def configure(self, manufacturer_data: bytes):

        self.info.update({
            'manufacturer_id': BleDeviceGobius.MANUFACTURER_ID,
            'product_id': 0x0000,  # TODO: find the appropriate value
            'product_name': 'Gobius C',
            'dev_prefix': 'gobius',
            'firmware_version': f"{manufacturer_data[7]}.{manufacturer_data[8]}.{manufacturer_data[9]}",
            'roles': {'tank': {}},
            'regs': [
                # As stated on Victron's site, manufacturer_data is 14 bytes:
                #   0    : HardwareID (7 bits used)
                #   1    : Temperature (7 bits used; °C = value - 40), MSB reserved
                #   2-3  : Distance (mm, uint16 LE)
                #   4-6  : UID tail = advertiser address bytes [2:0]
                #   7-9  : Firmware version (major, middle, minor)
                #   10   : Status Flags (ignored here)
                #   11-13: Spare (ignored; expected 0)
                {
                    'name':  'HardwareID',
                    'type': VE_UN8,
                    'offset': 0,
                    'bits': 7,
                    # .format	= &veUnitNone,
                },
                {
                    'name':  'Temperature',
                    'type': VE_UN8,
                    'offset': 1,
                    'bits': 7,
                    'scale': 1,
                    'bias': -40,
                    # .format	= &veUnitCelsius1Dec,
                },
                {
                    'name':  'RawValue',
                    'type': VE_UN16,
                    'offset': 2,
                    'xlate': self.gobius_level
                    # .format	= &veUnitcm,
                },
            ]
        })

    def check_manufacturer_data(self, manufacturer_data: bytes) -> bool:
        if len(manufacturer_data) != 14:
            return False

        # Check NIC (Network Interface Controller)
        dev_mac = self.info['dev_mac']
        if manufacturer_data[4] != int(dev_mac[6:8], 16) or \
                manufacturer_data[5] != int(dev_mac[8:10], 16) or \
                manufacturer_data[6] != int(dev_mac[10:], 16):
            return False
        return True

    def gobius_level(self, value: int) -> float:
        if value in [self._GOBIUS_STARTUP, self._GOBIUS_ERROR]:
            return -1
        return value / 10
