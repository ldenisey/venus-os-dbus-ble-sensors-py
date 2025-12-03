import sys
import os
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))
import unittest
from ble_role_movement import BleRoleMovement


class BleRoleMovementTests(unittest.TestCase):
    def setUp(self):
        self.role = BleRoleMovement()
        self.role.check_configuration()
        self.svc = {'Alarms/Movement/Enable': 1, 'MovementState': None, 'MovementCount': 10}

    def test_alarm_from_state_if_present(self):
        self.svc['MovementState'] = 0
        self.assertEqual(self.role.get_alarm_movement(self.svc), 0)
        self.svc['MovementState'] = 1
        self.assertEqual(self.role.get_alarm_movement(self.svc), 1)

    def test_alarm_from_count_delta_if_no_state(self):
        self.svc['MovementState'] = None
        self.role.update_data(self.svc, {'MovementCount': 10})
        self.assertEqual(self.role.get_alarm_movement(self.svc), 0)
        self.role.update_data(self.svc, {'MovementCount': 12})
        self.assertEqual(self.role.get_alarm_movement(self.svc), 1)
