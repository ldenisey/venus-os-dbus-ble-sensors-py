from seelevel_common import BleDeviceSeeLevel
import logging


class BleDeviceSeeLevelBTP3(BleDeviceSeeLevel):
    """
    SeeLevel 709-BTP3 (Cypress) tank/temperature/battery monitor.

    Each advertisement reports a single sensor. The same BLE MAC sends
    advertisements for different sensor numbers over time, so services
    are created lazily as new sensor numbers appear.

    Byte layout (14 bytes):
        0-2: Coach ID (24-bit, little-endian)
        3:   Sensor number (0-13)
        4-6: Sensor data (3 ASCII chars: "OPN", "ERR", or numeric)
        7-9: Volume in gallons (3 ASCII chars)
       10-12: Total capacity in gallons (3 ASCII chars)
        13:  Alarm state (ASCII digit '0'-'9')

    Sensor number mapping:
        0: Fresh Water (tank)     7: Temp (temperature)
        1: Toilet Water (tank)    8: Temp 2 (temperature)
        2: Wash Water (tank)      9: Temp 3 (temperature)
        3: LPG (tank)            10: Temp 4 (temperature)
        4: LPG 2 (tank)          11: Chemical (tank)
        5: Galley Water (tank)   12: Chemical 2 (tank)
        6: Galley Water 2 (tank) 13: Battery (battery)

    Cf.
    - https://github.com/TechBlueprints/victron-seelevel-python
    """

    MANUFACTURER_ID = 0x0131  # 305
    PRODUCT_NAME = 'SeeLevel 709-BTP3'
    DEV_PREFIX = 'seelevel_btp3'
    ROLES = {'tank': {}, 'temperature': {}, 'battery': {}}

    # (name, role_type, default_fluid_type)
    SENSORS = {
        0:  ("Fresh Water", "tank", 1),
        1:  ("Toilet Water", "tank", 5),
        2:  ("Wash Water", "tank", 2),
        3:  ("LPG", "tank", 8),
        4:  ("LPG 2", "tank", 8),
        5:  ("Galley Water", "tank", 2),
        6:  ("Galley Water 2", "tank", 2),
        7:  ("Temp", "temperature", None),
        8:  ("Temp 2", "temperature", None),
        9:  ("Temp 3", "temperature", None),
        10: ("Temp 4", "temperature", None),
        11: ("Chemical", "tank", 0),
        12: ("Chemical 2", "tank", 0),
        13: ("Voltage", "battery", None),
    }

    def check_manufacturer_data(self, manufacturer_data: bytes) -> bool:
        if len(manufacturer_data) < 7:
            return False
        sensor_num = manufacturer_data[3]
        return sensor_num in self.SENSORS

    def init(self):
        self._load_configuration()
        logging.debug(f"{self._plog} initialized (services created on demand)")

    def handle_manufacturer_data(self, manufacturer_data: bytes):
        sensor_num = manufacturer_data[3]
        sensor_info = self.SENSORS.get(sensor_num)
        if sensor_info is None:
            return

        name, role_type, fluid_type = sensor_info
        if role_type is None:
            return

        data_str = manufacturer_data[4:7].decode('ascii', errors='ignore').strip()

        if data_str == "OPN":
            return

        key = f'{role_type}_{sensor_num:02d}'

        if key not in self._role_services:
            config = {}
            if role_type == 'tank' and fluid_type is not None:
                config['fluid_type'] = fluid_type
            role_service = self._create_indexed_role_service(
                role_type, sensor_num, device_name=f"SeeLevel {name}",
                config=config)
            if role_service is None:
                return
        else:
            role_service = self._role_services[key]

        if not self._is_indexed_role_enabled(role_type, sensor_num):
            return

        if data_str == "ERR":
            self._set_error_status(role_service)
            return

        try:
            sensor_value = int(data_str)
        except ValueError:
            logging.warning(f"{self._plog} sensor {sensor_num}: unparseable value {data_str!r}")
            return

        if len(manufacturer_data) >= 14:
            alarm_byte = manufacturer_data[13]
            if ord('0') <= alarm_byte <= ord('9'):
                hw_alarm = alarm_byte - ord('0')
                if hw_alarm > 0:
                    logging.debug(f"{self._plog} sensor {sensor_num}: hardware alarm {hw_alarm}")

        if role_type == 'tank':
            level = max(0, min(100, sensor_value))
            sensor_data = self._build_tank_sensor_data(level, role_service)
            self._update_dbus_data(role_service, sensor_data)
            for alarm in role_service.ble_role.info.get('alarms', []):
                role_service.update_alarm(alarm)

        elif role_type == 'temperature':
            temp_c = round((sensor_value - 32.0) * 5.0 / 9.0, 1)
            sensor_data = {
                'Temperature': temp_c,
                'Status': 0,
            }
            role_service.ble_role.update_data(role_service, sensor_data)
            self._update_dbus_data(role_service, sensor_data)

        elif role_type == 'battery':
            voltage = sensor_value / 10.0
            sensor_data = {
                '/Dc/0/Voltage': voltage,
                'Status': 0,
            }
            self._update_dbus_data(role_service, sensor_data)

        role_service.connect()
