import sys
import os
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))
from ble_role_temperature import BleRoleTemperature
import unittest
import logging


class BleRoleTemperatureTests(unittest.TestCase):
    def setUp(self):
        #logging.basicConfig(level=logging.DEBUG)
        self.role = BleRoleTemperature()
        self.role.check_configuration()

    def test_offset_applied_in_update_data(self):
        role_service = {'Offset': 2}
        sensor = {'Temperature': 20}
        self.role.update_data(role_service, sensor)
        self.assertEqual(sensor['Temperature'], 22)

    def test_onchange_updates_immediately(self):
        role_service = {'Offset': 3, 'Temperature': None}
        # Simulate last raw temp tracked by update_data
        self.role._raw_temp = 15
        self.role.offset_update(role_service, 3)
        self.assertEqual(role_service['Temperature'], 18)
