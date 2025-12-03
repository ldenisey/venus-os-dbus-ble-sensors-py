from ble_role import BleRole
import logging
from gi.repository import GLib
from ve_types import *


class BleRoleDigitalInput(BleRole):
    """
    Digital input sensor role class.
    Device claiming this role must provide a 'InputState' item.

    Device type is set to 'Door alarm' by default. To change the default value, change the value of 'Type' setting below.
    You can change the type of a single device with dbus, for example:
        dbus -y com.victronenergy.digitalinput.teltonika_7cd9f411427d /Type SetValue 3 
    """

    NAME = 'digitalinput'

    INT32_MAX = 2**31-1

    INPUT_TYPES = {
        0: 'Disabled',
        1: 'Pulse meter',
        2: 'Door alarm',
        3: 'Bilge pump',
        4: 'Bilge alarm',
        5: 'Burglar alarm',
        6: 'Smoke alarm',
        7: 'Fire alarm',
        8: 'CO2 alarm',
        9: 'Generator',
        10: 'Generic I/O',  # Is it ? Gui V2 says not used...
        11: 'Touch enable',
    }

    INPUT_STATE = {
        0: 'Low',
        1: 'High',
        2: 'Off',
        3: 'On',
        4: 'No',
        5: 'Yes',
        6: 'Open',
        7: 'Closed',
        8: 'OK',
        9: 'Alarm',
        10: 'Running',
        11: 'Stopped',
    }

    ALARM_STATE = {
        0: 'OK',
        1: 'Warning',
        2: 'Alarm'
    }

    def __init__(self, config: dict = None):
        super().__init__()
        self._input_state: int = 0

        self.info.update(
            {
                'name': 'digitalinput',
                'dev_instance': 1,
                'settings': [
                    {
                        'name': 'Count',
                        'props': {
                            'type': VE_UN32,
                            'def': 0,
                            'min': 0,
                            'max': self.INT32_MAX
                        }
                    },
                    {
                        'name': 'Type',
                        'props': {
                            'type': VE_UN8,
                            'def': 2,  # Change this to change default input type
                            'min': 0,
                            'max': 11
                        },
                        'onchange': self._update_type
                    },
                    {
                        'name': 'Settings/InvertTranslation',
                        'props': {
                            'type': VE_UN8,
                            'def': 0,
                            'min': 0,
                            'max': 1
                        },
                        'onchange': self._update_invert_translation
                    },
                    {
                        'name': 'Settings/AlarmSetting',
                        'props': {
                            'type': VE_UN8,
                            'def': 0,
                            'min': 0,
                            'max': 1
                        },
                        'onchange': self._update_alarm_setting
                    },
                    {
                        'name': 'Settings/InvertAlarm',
                        'props': {
                            'type': VE_UN8,
                            'def': 0,
                            'min': 0,
                            'max': 1
                        },
                        'onchange': self._update_invert_alarm
                    },
                ],
                'alarms': [
                    {
                        'name': '/Alarm',
                        'update': self._update_alarm_state
                    }
                ]
            }
        )

    @staticmethod
    def _get_state_offset(_type: int) -> int:
        match _type:
            case 10:  # Low / High
                return 0
            case 3:  # Off / On
                return 2
            # case : # No / Yes
            #    return 4
            case 2:  # Open / Closed
                return 6
            case 4 | 5 | 6 | 7 | 8:  # Ok / Alarm
                return 8
            case 9:  # Running / Stopped
                return 10
            case _:  # types 0, 1, 11 do not generate digital input service
                return 0
        return None

    def _update_state(self, role_service, _type: int, input_state: int, invert_translation: int):
        __type = _type if _type is not None else role_service['Type']
        if __type == 0:
            role_service['State'] = 0
            return
        _input_state = input_state if input_state is not None else self._input_state
        _invert_translation = invert_translation if invert_translation is not None \
            else role_service['Settings/InvertTranslation']
        role_service['State'] = self._get_state_offset(__type) + (_input_state ^ _invert_translation)

    def _inc_count(self, role_service):
        count = (int(role_service['Count']) + 1) % self.INT32_MAX
        role_service['Count'] = count

    def _get_alarm_state(self, role_service, input_state: int, invert_translation: int, alarm_setting: int, invert_alarm: int) -> int:
        _input_state = input_state if input_state is not None else self._input_state
        _invert_translation = invert_translation if invert_translation is not None else role_service[
            'Settings/InvertTranslation']
        _alarm_setting = alarm_setting if alarm_setting is not None else role_service['Settings/AlarmSetting']
        _invert_alarm = invert_alarm if invert_alarm is not None else role_service['Settings/InvertAlarm']
        return 2 * bool(((_input_state ^ _invert_translation) ^ _invert_alarm) and _alarm_setting)

    def update_data(self, role_service, sensor_data: dict):
        input_state = int(sensor_data['InputState'])
        # Count state changes
        if input_state != self._input_state:
            self._inc_count(role_service)
        self._update_state(role_service, None, input_state, None)
        self._input_state = input_state

    def _update_type(self, role_service, new_type):
        def disable():
            role_service['Type'] = 0
            return False  # Do not call again

        match int(new_type):
            case 0:
                logging.warning(f"{self._plog} type '0' set, disabling digital input")
                self._update_state(role_service, 0, None, None)
            case 1 | 9 | 11:
                logging.warning(f"{self._plog} can not manage type '{new_type!r}', disabling digital input")
                GLib.idle_add(disable)
            case 2 | 3 | 4 | 5 | 6 | 7 | 8 | 10:
                self._update_state(role_service, int(new_type), None, None)
            case _:
                logging.error(f"{self._plog} unknown type '{new_type!r}', disabling digital input")
                GLib.idle_add(disable)

    def _update_invert_translation(self, role_service, new_translation):
        self._update_state(role_service, None, None, int(new_translation))
        role_service['/Alarm'] = self._get_alarm_state(role_service, None, int(new_translation), None, None)

    def _update_alarm_setting(self, role_service, alarm_setting):
        role_service['/Alarm'] = self._get_alarm_state(role_service, None, None, int(alarm_setting), None)

    def _update_invert_alarm(self, role_service, invert_alarm):
        role_service['/Alarm'] = self._get_alarm_state(role_service, None, None, None, int(invert_alarm))

    def _update_alarm_state(self, role_service) -> int:
        return self._get_alarm_state(role_service, None, None, None, None)
