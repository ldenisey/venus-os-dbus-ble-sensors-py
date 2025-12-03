import sys
import os
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))
import unittest
import logging
from ble_role_digitalinput import BleRoleDigitalInput


class BleRoleDigitalInputTests(unittest.TestCase):
    def setUp(self):
        logging.basicConfig(level=logging.DEBUG)
        self.role = BleRoleDigitalInput()
        self.role.check_configuration()
        self.svc = {
            'Type': 2,  # Door alarm => Open/Closed mapping offset=6
            'Settings/InvertTranslation': 0,
            'Settings/AlarmSetting': 1,
            'Settings/InvertAlarm': 0,
            'Count': 42,
            'State': 0,
            'Alarm': 0
        }

    def test_state_mapping_open_closed(self):
        self.role.update_data(self.svc, {'InputState': 0})
        self.assertEqual(self.svc['State'], 6)  # Open
        self.role.update_data(self.svc, {'InputState': 1})
        self.assertEqual(self.svc['State'], 7)  # Closed

    def test_invert_translation(self):
        self.svc['Settings/InvertTranslation'] = 1
        self.role.update_data(self.svc, {'InputState': 0})
        self.assertEqual(self.svc['State'], 7)
        self.role.update_data(self.svc, {'InputState': 1})
        self.assertEqual(self.svc['State'], 6)

    def test_count_increments_on_changes(self):
        # Multiple updates but only one state change
        self.role.update_data(self.svc, {'InputState': 0})
        self.role.update_data(self.svc, {'InputState': 0})
        self.role.update_data(self.svc, {'InputState': 1})
        self.role.update_data(self.svc, {'InputState': 1})
        self.assertEqual(self.svc['Count'], 43)

    def test_alarm_logic(self):
        # alarm on, input on
        self.role.update_data(self.svc, {'InputState': 1})
        self.svc['Alarm'] = self.role._update_alarm_state(self.svc)
        self.assertEqual(self.svc['Alarm'], 2)  # Alarm on
        # Alarm off, input on
        self.svc['Settings/AlarmSetting'] = 0
        self.svc['Alarm'] = self.role._update_alarm_state(self.svc)
        self.assertEqual(self.svc['Alarm'], 0)  # alarm off
        # alarm on, input on, invert alarm on
        self.svc['Settings/AlarmSetting'] = 1
        self.svc['Settings/InvertAlarm'] = 1
        self.svc['Alarm'] = self.role._update_alarm_state(self.svc)
        self.assertEqual(self.svc['Alarm'], 0)  # alarm off
        # alarm on, input on, invert alarm on, invert input on
        self.svc['Settings/InvertTranslation'] = 1
        self.svc['Alarm'] = self.role._update_alarm_state(self.svc)
        self.assertEqual(self.svc['Alarm'], 2)  # alarm on
