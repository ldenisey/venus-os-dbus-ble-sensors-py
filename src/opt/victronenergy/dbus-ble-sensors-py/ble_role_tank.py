from ble_role import BleRole
from ve_types import *
import logging


class BleRoleTank(BleRole):
    """
    Tank level sensor role class.
    Device claiming this role must provide a 'RawValue' item.

    Methods to compute high and low level alarms are provided, but can be overloaded by device class.

    C.f.:
    - https://github.com/victronenergy/dbus-ble-sensors/blob/master/src/tank.c
    - https://github.com/victronenergy/gui-v2/blob/main/data/mock/conf/services/tank-lpg.json
    - https://github.com/victronenergy/dbus-recorder/blob/master/demo2_water.csv
    """

    NAME = 'tank'

    INT32_MAX = 2**31-1
    TANK_SHAPE_MAX_POINTS = 10

    FLUID_TYPES = {
        0: 'Fuel',
        1: 'Fresh water',
        2: 'Waste water',
        3: 'Live well',
        4: 'Oil',
        5: 'Black water (sewage)',
        6: 'Gasoline',
        7: 'Diesel',
        8: 'LPG',
        9: 'LNG',
        10: 'Hydraulic oil',
        11: 'Raw water'
    }

    TANK_STATUS = {
        0: 'Tank_Status_Ok',
        1: 'Tank_Status_Open_Circuit',
        2: 'Tank_Status_ShortCircuited',
        3: 'Tank_Status_ReversePolarity',
        4: 'Tank_Status_Unknown',
        5: 'Tank_Status_Error'
    }

    _empty_props = {
        'type': VE_FLOAT,
        'def': 0.0,
        'min': 0.0,
        'max': 500.0
    }

    _full_props = {
        'type': VE_FLOAT,
        'def': 20.0,
        'min': 0.0,
        'max': 500.0
    }

    def __init__(self, config: dict = None):
        super().__init__()

        flags = config.get('flags', []) if config is not None else []
        self._is_topdown: bool = 'TANK_FLAG_TOPDOWN' in flags
        self._shape_map = None

        self.info.update(
            {
                'name': 'tank',
                'dev_instance': 20,
                'settings': [
                    {
                        'name': 'Capacity',
                        'props': {
                            'type': VE_FLOAT,
                            'def': 0.2,
                            'min': 0,
                            'max': 1000
                        },
                        'onchange': self._tank_capacity_changed
                    },
                    {
                        'name': 'FluidType',
                        'props': {
                            'type': VE_SN32,
                            'def': 0,
                            'min': 0,
                            'max': self.INT32_MAX - 3
                        }
                    },
                    {
                        'name': 'Shape',
                        'props': {
                            'type': VE_HEAP_STR,
                            'def': '',
                        },
                        'onchange': self._tank_shape_changed
                    },
                    {
                        'name': 'RawValueEmpty',
                        'props': self._full_props if self._is_topdown else self._empty_props,
                        'onchange': self._tank_empty_changed
                    },
                    {
                        'name': 'RawValueFull',
                        'props': self._empty_props if self._is_topdown else self._full_props,
                        'onchange': self._tank_full_changed
                    },
                    {
                        'name': '/Alarms/High/Enable',
                        'props': {
                            'type': VE_UN8,
                            'def': 0,
                            'min': 0,
                            'max': 1
                        }
                    },
                    {
                        'name': '/Alarms/High/Active',
                        'props': {
                            'type': VE_SN32,
                            'def': 90,
                            'min': 0,
                            'max': 100
                        }
                    },
                    {
                        'name': '/Alarms/High/Restore',
                        'props': {
                            'type': VE_SN32,
                            'def': 80,
                            'min': 0,
                            'max': 100
                        }
                    },
                    {
                        'name': '/Alarms/Low/Enable',
                        'props': {
                            'type': VE_UN8,
                            'def': 0,
                            'min': 0,
                            'max': 1
                        }
                    },
                    {
                        'name': '/Alarms/Low/Active',
                        'props': {
                            'type': VE_SN32,
                            'def': 10,
                            'min': 0,
                            'max': 100
                        }
                    },
                    {
                        'name': '/Alarms/Low/Restore',
                        'props': {
                            'type': VE_SN32,
                            'def': 15,
                            'min': 0,
                            'max': 100
                        }
                    },
                ],
                'alarms': [
                    {
                        'name': '/Alarms/High/State',
                        'update': self.get_alarm_high_state  # Can be overloaded by device class
                    },
                    {
                        'name': '/Alarms/Low/State',
                        'update': self.get_alarm_low_state  # Can be overloaded by device class
                    },
                ]
            }
        )

    def get_alarm_high_state(self, role_service) -> int:
        """
        Default method to compute tank high level alarm. Can be overridden by overloading info['alarms'] entries in device class.
        """

        if role_service['/Alarms/High/Enable']:
            alarm_state = bool(role_service['/Alarms/High/State'])
            alarm_threshold = role_service[f"/Alarms/High/{'Restore' if alarm_state else 'Active'}"]
            tank_level = float(role_service['Level'])
            return int(tank_level > alarm_threshold)
        else:
            return 0

    def get_alarm_low_state(self, role_service) -> int:
        """
        Default method to compute tank low level alarm. Can be overridden by overloading info['alarms'] entries in device class.
        """

        if role_service['/Alarms/Low/Enable']:
            alarm_state = bool(role_service['/Alarms/Low/State'])
            alarm_threshold = role_service[f"/Alarms/Low/{'Restore' if alarm_state else 'Active'}"]
            tank_level = float(role_service['Level'])
            return int(tank_level < alarm_threshold)
        else:
            return 0

    def init(self, role_service):
        role_service['RawUnit'] = 'cm'
        role_service['Remaining'] = 0.0
        role_service['Level'] = 0.0

    def _compute_level(self, rawValue: float, empty: float, full: float, capacity: float) -> tuple[int, float, int]:
        """
        Compute tank info based on parameters and returns (level, remaining volume, status code)
        """
        error = (None, None, 4)
        if rawValue is None or rawValue == 1 or empty is None or full is None or capacity is None:
            return error

        if self._is_topdown:
            if empty <= full:
                return error
        else:
            if empty >= full:
                return error

        level = (rawValue - empty) / (full - empty)
        if level < 0:
            level = 0
        elif level > 1:
            level = 1

        for i in range(1, len(self._shape_map)):
            if self._shape_map[i][0] >= level:
                lev_1 = float(self._shape_map[i-1][0])
                lev_2 = float(self._shape_map[i][0])
                vol_1 = float(self._shape_map[i-1][1])
                vol_2 = float(self._shape_map[i][1])
                level = vol_1 + (level - lev_1) / (lev_2 - lev_1) * (vol_2 - vol_1)
                break

        return int(100 * level), level * capacity, 0

    def _tank_capacity_changed(self, role_service, new_capacity):
        (level, remain, status) = self._compute_level(
            float(role_service['RawValue']),
            float(role_service['RawValueEmpty']),
            float(role_service['RawValueFull']),
            float(new_capacity)
        )

        role_service['Level'] = level
        role_service['Remaining'] = remain
        role_service['Status'] = status

    def _parse_shape_str(self, shape_str: str):
        if shape_str is None or shape_str == '':
            self._shape_map = []
            return
        self._shape_map = [(0, 0)]
        points = shape_str.split(',')
        for i in range(0, min(self.TANK_SHAPE_MAX_POINTS, len(points))):
            point = points[i].split(':')
            level = 0
            volume = 0
            if len(point) != 2:
                logging.warning(f"{self._plog} ignoring shape, shape point {point!r} does not contain 2 elements")
                self._shape_map = []
                return
            try:
                level = int(point[0]) / 100
                volume = int(point[1]) / 100
                if level <= 0 or level >= 1 or volume <= 0 or volume >= 1:
                    logging.warning(f"{self._plog} ignoring shape, shape point {point!r} element(s) out of range 1-99")
                    self._shape_map = []
                    return
            except ValueError:
                logging.warning(f"{self._plog} ignoring shape, shape point {point!r} contains non-integer elements")
                self._shape_map = []
                return
            if level <= self._shape_map[i][0] or volume <= self._shape_map[i][1]:
                logging.warning(f"{self._plog} ignoring shape, shape point {point!r} elements not strictly increasing")
                self._shape_map = []
                return
            self._shape_map.append((level, volume))
        self._shape_map.append((1.0, 1.0))
        self._shape_map.sort(key=lambda x: x[0])

    def _tank_shape_changed(self, role_service, new_shape):
        # Check shape validity and convert it to percentage values
        self._parse_shape_str(new_shape)

        (level, remain, status) = self._compute_level(
            float(role_service['RawValue']),
            float(role_service['RawValueEmpty']),
            float(role_service['RawValueFull']),
            float(role_service['Capacity'])
        )

        role_service['Level'] = level
        role_service['Remaining'] = remain
        role_service['Status'] = status

    def _tank_empty_changed(self, role_service, new_empty):
        (level, remain, status) = self._compute_level(
            float(role_service['RawValue']),
            float(new_empty),
            float(role_service['RawValueFull']),
            float(role_service['Capacity'])
        )

        role_service['Level'] = level
        role_service['Remaining'] = remain
        role_service['Status'] = status

    def _tank_full_changed(self, role_service, new_full):
        (level, remain, status) = self._compute_level(
            float(role_service['RawValue']),
            float(role_service['RawValueEmpty']),
            float(new_full),
            float(role_service['Capacity'])
        )

        role_service['Level'] = level
        role_service['Remaining'] = remain
        role_service['Status'] = status

    def update_data(self, role_service, sensor_data: dict):
        if self._shape_map is None:
            self._parse_shape_str(role_service['Shape'])

        (level, remain, status) = self._compute_level(
            float(sensor_data['RawValue']),
            float(role_service['RawValueEmpty']),
            float(role_service['RawValueFull']),
            float(role_service['Capacity'])
        )

        sensor_data['Level'] = level
        sensor_data['Remaining'] = remain
        sensor_data['Status'] = status
