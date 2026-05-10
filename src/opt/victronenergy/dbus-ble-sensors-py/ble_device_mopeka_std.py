from ve_types import *
from ble_device import BleDevice
import logging
from dbus_role_service import DbusRoleService


class BleDeviceMopekaStd(BleDevice):
    """
    Mopeka Standard / first-generation Tank Check sensors.

    These predate the 'Pro' line and use a different BLE format: the
    manufacturer specific data is broadcast under Texas Instruments'
    company id (0x000D, since the original sensor is built around a TI
    BLE chip), and the advertisement also carries the 16-bit Service UUID
    0xADA0.  The Pro line moved to Nordic Semiconductor (0x0059) and a
    much smaller payload — that variant is handled in
    ``ble_device_mopeka.py``.

    Supported models:
    - Mopeka Tank Check (M1001) — propane / butane via ultrasonic time of flight

    Reference public implementations describing the wire format:
    - https://esphome.io/components/sensor/mopeka_std_check/
      (Apache 2.0; the canonical open description of the layout, sensor
      type byte, battery scaling, temperature scaling, and time-of-flight
      table)
    - https://github.com/spbrogan/mopeka_pro_check (MIT)

    Manufacturer data layout (23 bytes after the 0x000D company id):

      offset | bits | name              | notes
      -------+------+-------------------+-------------------------------
        0    |   8  | _data_0           | reserved / status (unused)
        1    |   8  | sensor_type       | low 6 bits + high 2 (mask 0xCF)
        2    |   8  | raw_voltage       | volts = raw / 256 * 2 + 1.5
        3    |  6   | raw_temp          | low 6 bits of byte 3
        3    |  1   | slow_update       | bit 6 of byte 3
        3    |  1   | sync_pressed      | bit 7 of byte 3
        4-18 | 120  | sensor_table      | 12 (time, value) pairs of 5 bits each
       19    |   8  | session counter   | varies, ignored
       20-22 |  24  | mac_trail         | last 3 bytes of the device MAC
    """

    MANUFACTURER_ID = 0x000D  # 'Texas Instruments Inc.'

    # service UUID broadcast alongside the manufacturer data (advertisement
    # includes a 16-bit service UUID list with this single entry)
    SERVICE_UUID_16 = 0xADA0

    EXPECTED_MAN_DATA_LEN = 23

    # Sensor type byte (after masking with 0xCF) -> (device_name, roles, settings)
    _SENSOR_LPG = 0x02   # standard propane/butane
    _SENSOR_XL = 0x03    # XL variant
    _SENSOR_LPG_ALT = 0x44  # alternate firmware revision
    _SENSOR_ETRAILER = 0x46

    SENSOR_TYPES = {
        _SENSOR_LPG:      'Mopeka Std',
        _SENSOR_XL:       'Mopeka XL',
        _SENSOR_LPG_ALT:  'Mopeka Std',
        _SENSOR_ETRAILER: 'Mopeka eTrailer',
    }

    # Speed of sound in LPG vapour space at temperature T (°C), as a function
    # of butane mole fraction r in [0, 1].  Pure propane: r=0.  Pure butane:
    # r=1.  Coefficients taken from the public ESPHome implementation:
    #   v = 1040.71 - 4.87 T - 137.5 r - 0.0107 T^2 - 1.63 T r           (m/s)
    _SOS_C0 = 1040.71
    _SOS_C_T = -4.87
    _SOS_C_R = -137.5
    _SOS_C_TT = -0.0107
    _SOS_C_TR = -1.63

    def configure(self, manufacturer_data: bytes):
        sensor_type_byte = manufacturer_data[1] & 0xCF
        device_name = self.SENSOR_TYPES.get(sensor_type_byte, 'Mopeka Std')

        self.info.update({
            'manufacturer_id': BleDeviceMopekaStd.MANUFACTURER_ID,
            'product_id': 0xC02B,  # distinct from Pro line (0xC02A)
            'product_name': 'Mopeka sensor',
            'device_name': device_name,
            'dev_prefix': 'mopeka_std',
            'roles': {'tank': {}},
            'regs': [
                # byte 1: sensor type (low 6 bits + high 2; bits 4-5 reserved)
                {
                    'name':  'HardwareID',
                    'type': VE_UN8,
                    'offset': 1,
                    'xlate': lambda v: v & 0xCF,
                },
                # byte 2: battery raw, converted to BatteryVoltage in update_data
                # (must flow into the role dict for update_data to consume it).
                {
                    'name':  'BatteryRaw',
                    'type': VE_UN8,
                    'offset': 2,
                },
                # byte 3 low 6 bits: raw temperature index, converted to
                # Temperature in update_data.
                {
                    'name':  'RawTemp',
                    'type': VE_UN8,
                    'offset': 3,
                    'bits': 6,
                },
                # byte 3 bit 6: slow update flag
                {
                    'name':  'SlowUpdate',
                    'type': VE_UN8,
                    'offset': 3,
                    'shift': 6,
                    'bits': 1,
                    'roles': [None],
                },
                # byte 3 bit 7: sync button / pairing
                {
                    'name':  'SyncButton',
                    'type': VE_UN8,
                    'offset': 3,
                    'shift': 7,
                    'bits': 1,
                    'roles': [None],
                },
            ],
            'settings': [
                {
                    'name': 'ButaneRatio',
                    'props': {
                        'type': VE_SN32,
                        'def': 0,
                        'min': 0,
                        'max': 100,
                    },
                },
            ],
            'alarms': [
                {
                    'name': '/Alarms/LowBattery',
                    'update': self._get_low_battery_state,
                },
            ],
        })

    def check_manufacturer_data(self, manufacturer_data: bytes) -> bool:
        if len(manufacturer_data) != BleDeviceMopekaStd.EXPECTED_MAN_DATA_LEN:
            return False
        # Sensor type byte must be one we recognise; otherwise this is some
        # other 0x000D / Texas Instruments device we don't speak.
        if (manufacturer_data[1] & 0xCF) not in BleDeviceMopekaStd.SENSOR_TYPES:
            return False
        # Last three bytes of the manufacturer payload echo the last three
        # bytes of the device MAC; reject mismatched frames the same way
        # the existing Mopeka and Gobius drivers do.
        dev_mac = self.info['dev_mac'].upper()
        if manufacturer_data[20] != int(dev_mac[6:8], 16) or \
                manufacturer_data[21] != int(dev_mac[8:10], 16) or \
                manufacturer_data[22] != int(dev_mac[10:], 16):
            return False
        return True

    @staticmethod
    def _unpack_5bit_chunk(chunk: bytes) -> list[int]:
        """
        Unpack a 5-byte (40-bit) chunk into 8 5-bit fields, LSB first.

        Returns [time_0, value_0, time_1, value_1, time_2, value_2, time_3, value_3]
        in the order the device packs them (matches the C struct
        ``mopeka_std_values`` on a little-endian target).
        """
        if len(chunk) != 5:
            return []
        bits = int.from_bytes(chunk, byteorder='little')
        return [(bits >> (5 * i)) & 0x1F for i in range(8)]

    @classmethod
    def _find_best_time(cls, sensor_table: bytes) -> tuple[int, int, int]:
        """
        Walk the 12 (time, value) measurements packed into the 15-byte sensor
        table and return ``(best_time, best_value, usable_count)``.

        Each measurement carries an amplitude (``value``) and a delta-time
        increment (``time``).  The reading time accumulates across leading
        zero-amplitude entries (because the encoded delta is 5 bits and the
        actual gap may exceed 31), and resets each time a non-zero value
        lands.  The "best" measurement is the one with the strongest
        amplitude, and its accumulated time is what we use for time of
        flight.

        ``best_time`` is in 10us ticks.
        """
        if len(sensor_table) != 15:
            return 0, 0, 0

        times: list[int] = []
        values: list[int] = []
        for chunk_offset in range(0, 15, 5):
            unpacked = cls._unpack_5bit_chunk(sensor_table[chunk_offset:chunk_offset + 5])
            for i in range(0, 8, 2):
                # +1 because the encoded delta is 0-based but a measurement
                # always advances time by at least one tick.
                times.append(unpacked[i] + 1)
                values.append(unpacked[i + 1])

        accumulated = 0
        best_time = 0
        best_value = 0
        usable = 0
        for t, v in zip(times, values):
            accumulated += t
            if v != 0:
                usable += 1
                if v > best_value:
                    best_value = v
                    best_time = accumulated
                accumulated = 0
        return best_time, best_value, usable

    @classmethod
    def _speed_of_sound_lpg(cls, temperature_c: float, butane_ratio: float) -> float:
        """
        Speed of sound (m/s) in propane/butane vapour at *temperature_c* °C
        with a butane mole fraction of *butane_ratio* (0..1).
        """
        t = temperature_c
        r = butane_ratio
        return (cls._SOS_C0
                + cls._SOS_C_T * t
                + cls._SOS_C_R * r
                + cls._SOS_C_TT * t * t
                + cls._SOS_C_TR * t * r)

    def _parse_manufacturer_data(self, manufacturer_data: bytes) -> dict:
        # Run the regular reg-driven parse first.
        values = super()._parse_manufacturer_data(manufacturer_data)

        # Extract the time-of-flight table; record the best (time, value) pair
        # so update_data() can convert it to a distance.
        if len(manufacturer_data) >= 19:
            best_time, best_value, usable = self._find_best_time(manufacturer_data[4:19])
            for role in values:
                values[role]['BestTime'] = best_time
                values[role]['BestValue'] = best_value
                values[role]['UsableMeasurements'] = usable
        return values

    def update_data(self, role_service: DbusRoleService, sensor_data: dict):
        """
        Compute Temperature (°C), BatteryVoltage (V), and RawValue (cm) from the
        raw fields parsed by the regs system.  Mirrors the public ESPHome
        std parser's behaviour.
        """
        if role_service.ble_role.NAME != 'tank':
            return

        # Temperature: raw==0 maps to -40°C (sensor disconnect / startup),
        # otherwise temp = (raw - 25) * 1.776964.
        raw_temp = sensor_data.pop('RawTemp', None)
        if raw_temp is None:
            logging.warning(f"{self._plog} missing raw temperature, skipping update")
            return
        if raw_temp == 0:
            temperature = -40.0
        else:
            temperature = (raw_temp - 25.0) * 1.776964
        sensor_data['Temperature'] = temperature

        # Battery voltage: raw / 256 * 2 + 1.5 V.  The /Alarms/LowBattery
        # callback consumes BatteryVoltage by name, matching the Pro driver.
        raw_voltage = sensor_data.pop('BatteryRaw', None)
        if raw_voltage is None:
            logging.warning(f"{self._plog} missing raw battery, skipping update")
            return
        sensor_data['BatteryVoltage'] = raw_voltage / 256.0 * 2.0 + 1.5

        # Distance from time of flight, only if the table had a usable echo.
        best_time = sensor_data.pop('BestTime', 0)
        best_value = sensor_data.pop('BestValue', 0)
        usable = sensor_data.pop('UsableMeasurements', 0)

        if usable < 1 or best_value < 2 or best_time < 2:
            sensor_data['RawValue'] = 0
            return

        butane_ratio = role_service['ButaneRatio']
        if butane_ratio is None:
            butane_ratio = 0
        speed_mps = self._speed_of_sound_lpg(temperature, butane_ratio / 100.0)

        # ESPHome computes mm: speed (m/s) * best_time (10us ticks) / 100.0.
        # Convert to cm to match the Pro driver convention.
        distance_mm = speed_mps * best_time / 100.0
        sensor_data['RawValue'] = distance_mm / 10.0

    def _get_low_battery_state(self, role_service: DbusRoleService) -> int:
        try:
            battery_voltage = role_service['BatteryVoltage']
        except (KeyError, TypeError):
            return 0
        if battery_voltage is None:
            return 0
        # CR2032 scaling matches the Pro driver
        battery_percentage = max(0, min(100, ((battery_voltage - 2.2) / 0.65) * 100))
        return int(battery_percentage < 15)
