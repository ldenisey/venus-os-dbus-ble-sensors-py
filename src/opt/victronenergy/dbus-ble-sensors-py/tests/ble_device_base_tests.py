import sys
import os
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext', 'velib_python'))
from ble_role import BleRole
import unittest
import logging


# @unittest.skip("Base test class")
class BleDeviceBaseTests(unittest.TestCase):

    def setUp(self, dev_class, dev_mac: str, dev_name: str):
        logging.basicConfig(level=logging.DEBUG)
        self.maxDiff = None  # See full comparison on failures
        BleRole.load_classes(os.path.dirname(os.path.abspath(__file__)))
        self.device = dev_class(dev_mac, dev_name)

    def tearDown(self):
        del self.device

    def _test_parsing(self, raw_data: bytes, expected_dict: dict) -> dict:
        # Prepare
        self.device.configure(raw_data)
        self.device._load_configuration()

        # Test
        self.assertTrue(self.device.check_manufacturer_data(raw_data))
        parsed_dict = self.device._parse_manufacturer_data(raw_data)
        self.assertDictEqual(parsed_dict, expected_dict)
        return parsed_dict
