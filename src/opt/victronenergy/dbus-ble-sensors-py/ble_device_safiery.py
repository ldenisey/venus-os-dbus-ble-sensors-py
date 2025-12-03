from ve_types import *
from ble_device import BleDevice


class BleDeviceSafiery(BleDevice):
    """
    Safiery devices class managing Star-Tank devices.

    Cf.
    - https://github.com/victronenergy/dbus-ble-sensors/blob/master/src/safiery.c
    """

    MANUFACTURER_ID = 0x0067

    def configure(self, _: bytes):
        self.info.update({
            'manufacturer_id': BleDeviceSafiery.MANUFACTURER_ID,
            'product_id': 0x0000,  # TODO: find the appropriate value
            'product_name': 'StarTank',
            'dev_prefix': 'safiery',
            'roles': {'tank': {'flags': ['TANK_FLAG_TOPDOWN']}},
            'regs': [
                {
                    'name':  'HardwareID',
                    'type': VE_UN8,
                    'offset': 0,
                    'bits': 7,
                    # .format	= &veUnitNone,
                },
                {
                    'name':  'BatteryVoltage',
                    'type': VE_UN8,
                    'offset': 1,
                    'bits': 7,
                    'scale': 32,
                    # .format	= &veUnitVolt2Dec,
                },
                {
                    'name':  'Temperature',
                    'type': VE_UN8,
                    'offset': 2,
                    'bits': 7,
                    'scale': 1,
                    'bias': -40,
                    # .format	= &veUnitCelsius1Dec,
                },
                {
                    'name':  'SyncButton',
                    'type': VE_UN8,
                    'offset': 2,
                    'shift': 7,
                    'bits': 1,
                    # .format	= &veUnitNone,
                },
                {
                    'name':  'RawValue',
                    'type': VE_UN16,
                    'offset': 3,
                    'bits': 14,
                    'scale': 10,
                    # .format	= &veUnitNone,
                },
                {
                    'name':  'AccelX',
                    'type': VE_SN8,
                    'offset': 8,
                    'scale': 1024,
                    # .format	= &veUnitG2Dec,
                },
                {
                    'name':  'AccelY',
                    'type': VE_SN8,
                    'offset': 9,
                    'scale': 1024,
                    # .format	= &veUnitG2Dec,
                },
                {
                    'name':  'AccelZ',
                    'type': VE_SN8,
                    'offset': 10,
                    'scale': 1024,
                    # .format	= &veUnitG2Dec,
                },
            ]
        })

    def check_manufacturer_data(self, manufacturer_data: bytes) -> bool:
        if len(manufacturer_data) != 10:
            return False

        # Check NIC (Network Interface Controller)
        dev_mac = self.info['dev_mac']
        if manufacturer_data[5] != int(dev_mac[6:8], 16) or \
                manufacturer_data[6] != int(dev_mac[8:10], 16) or \
                manufacturer_data[7] != int(dev_mac[10:], 16):
            return False
        return True
