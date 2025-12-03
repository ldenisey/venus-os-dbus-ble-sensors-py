import sys
import os
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext', 'velib_python'))
import unittest
from ble_device import BleDevice
from ble_device_base_tests import BleDeviceBaseTests
from ve_types import *
import logging


class _DummyDevice(BleDevice):
    MANUFACTURER_ID = 0x1234

    def configure(self, manufacturer_data: bytes):
        self.info.update({
            'product_id': 1,
            'product_name': 'Dummy',
            'dev_prefix': 'dummy',
            'roles': {'temperature': {}, 'movement': {}},
            'regs': [
                # little-endian unsigned with bits+shift
                {'name': 'Ubits', 'type': VE_UN16, 'offset': 0, 'bits': 10, 'shift': 3},
                # big-endian signed 8 -> negative
                {'name': 'S8', 'type': VE_SN8, 'offset': 2, 'flags': ['REG_FLAG_BIG_ENDIAN']},
                # scale and bias
                {'name': 'Scaled', 'type': VE_UN16, 'offset': 3, 'scale': 10, 'bias': -5},
                # invalid flag knocks value out
                {'name': 'Maybe', 'type': VE_UN8, 'offset': 5, 'flags': ['REG_FLAG_INVALID'], 'inval': 0xFF},
                # xlate hook
                {'name': 'Hooked', 'type': VE_UN8, 'offset': 6, 'xlate': lambda v: v*2},
                # fixed-length string with NUL padding
                {'name': 'Str', 'type': VE_HEAP_STR, 'offset': 7, 'bits': 8*5},
                # role filtering (to temperature only)
                {'name': 'Onlytemperature', 'type': VE_UN8, 'offset': 12, 'roles': ['temperature']},
                # ignored role (None)
                {'name': 'Ignored', 'type': VE_UN8, 'offset': 13, 'roles': [None]},
            ]
        })


class BleDeviceParsingTests(BleDeviceBaseTests):
    def setUp(self):
        super().setUp(_DummyDevice, '001122334455', 'Dummy')

    def test_numeric_and_string_parsing(self):
        # 0-1: for Ubits with shift 3, bits 10 => take 10 bits from 13-bit window
        # 2:   S8 = 0xF6 (-10)
        # 3-4: Scaled = 1234 -> 123.4 - 5 = 118.4
        # 5:   Maybe = 0xFF -> None
        # 6:   Hooked = 0x07 -> 14
        # 7-11: "hi\x00\x00" (5 bytes)
        # 12:  Onlytemperature = 9
        # 13:  Ignored = 1 (should not appear)
        raw = b'\xB6\x40\xF6\xD2\x04\xFF\x07hi\x00\x00\x00\t\x01'

        parsed = self._test_parsing(
            raw,
            {
                'temperature': {
                    'Ubits': 22,
                    'S8': -10,
                    'Scaled': 118.4,
                    'Hooked': 14,
                    'Str': 'hi',
                    'Onlytemperature': 9,
                },
                'movement': {
                    'Ubits': 22,
                    'S8': -10,
                    'Scaled': 118.4,
                    'Hooked': 14,
                    'Str': 'hi',
                },
            }
        )
        self.assertNotIn('Maybe', parsed['temperature'])  # None filtered out
        self.assertNotIn('Ignored', parsed['temperature'])
        self.assertNotIn('Onlytemperature', parsed['movement'])
