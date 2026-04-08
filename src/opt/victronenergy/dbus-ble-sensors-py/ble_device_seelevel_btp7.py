from seelevel_common import BleDeviceSeeLevel
import logging


class BleDeviceSeeLevelBTP7(BleDeviceSeeLevel):
    """
    SeeLevel 709-BTP7 tank/battery monitor.

    Broadcasts 8 tank levels (0-100%) and battery voltage in a single
    14-byte manufacturer data advertisement.

    Byte layout:
        0-2: Coach ID (24-bit, little-endian)
        3-10: Tank levels (1 byte each, 0-100 = %, >100 = error code)
              Slots: Fresh, Wash, Toilet, Fresh2, Wash2, Toilet2, Wash3, LPG
        11:  Battery voltage * 10 (e.g. 120 = 12.0V)

    Cf.
    - https://github.com/TechBlueprints/victron-seelevel-python
    """

    MANUFACTURER_ID = 0x0CC0  # 3264
    PRODUCT_NAME = 'SeeLevel 709-BTP7'
    ROLES = {'tank': {}, 'battery': {}}

    TANK_SLOTS = [
        ("Fresh Water", 1),      # slot 0: FluidType = Fresh water
        ("Wash Water", 2),       # slot 1: FluidType = Waste water
        ("Toilet Water", 5),     # slot 2: FluidType = Black water
        ("Fresh Water 2", 1),    # slot 3
        ("Wash Water 2", 2),     # slot 4
        ("Toilet Water 2", 5),   # slot 5
        ("Wash Water 3", 2),     # slot 6
        ("LPG", 8),             # slot 7: FluidType = LPG
    ]

    def check_manufacturer_data(self, manufacturer_data: bytes) -> bool:
        return len(manufacturer_data) >= 12

    def init(self):
        self._load_configuration()

        for slot, (tank_name, fluid_type) in enumerate(self.TANK_SLOTS):
            role_service = self._create_indexed_role_service(
                'tank', slot, device_name=f"SeeLevel {tank_name}")
            if role_service:
                if role_service['FluidType'] == 0:
                    role_service['FluidType'] = fluid_type
                if role_service['Capacity'] == 0.2:
                    role_service['Capacity'] = 0.0

        self._create_indexed_role_service(
            'battery', 8, device_name="SeeLevel Voltage")

        logging.debug(f"{self._plog} initialized {len(self._role_services)} service slots")

    def handle_manufacturer_data(self, manufacturer_data: bytes):
        for slot in range(8):
            if not self._is_indexed_role_enabled('tank', slot):
                continue

            role_service = self._role_services.get(f'tank_{slot:02d}')
            if role_service is None:
                continue

            level = manufacturer_data[slot + 3]

            if level > 100:
                self._set_error_status(role_service, level)
                continue

            sensor_data = self._build_tank_sensor_data(level, role_service)
            self._update_dbus_data(role_service, sensor_data)
            role_service.connect()

        if self._is_indexed_role_enabled('battery', 8):
            battery_service = self._role_services.get('battery_08')
            if battery_service and len(manufacturer_data) >= 12:
                voltage = manufacturer_data[11] / 10.0
                self._update_dbus_data(battery_service, {
                    '/Dc/0/Voltage': voltage,
                    'Status': 0,
                })
                battery_service.connect()
