import sys
import os
import unittest

sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext', 'velib_python'))

from ble_device_base_tests import BleDeviceBaseTests
from ble_device_mopeka_std import BleDeviceMopekaStd


# Real frames captured with btmon on a production cerbo from
# FC:45:C3:DD:71:51 (sensor #1) and FC:45:C3:DE:36:15 (sensor #2).
# Each value is the 23-byte manufacturer-specific data block (i.e. the
# AD payload after the 0x000D / Texas Instruments company id).
#
# We retain a few frames per sensor so the test suite exercises the
# bit-unpacking path on real bytes — synthetic frames alone wouldn't
# catch off-by-one or endianness mistakes against real data.
_CAPTURED_DD71_51 = [
    bytes.fromhex('00029d692a6e20c0061a649040051c24500107a4dd7151'),
    bytes.fromhex('00029d690a0a8040050f78100101107c2001012ddd7151'),
    bytes.fromhex('00029d690a1e3000021e08300105175c70c00438dd7151'),
    bytes.fromhex('00029d690a1eb080071a043000061828d08104addd7151'),
    bytes.fromhex('00029d690a2ed041041430e04001036c1000048cdd7151'),
    bytes.fromhex('00029d690a42f081040420d04102165cc0810240dd7151'),
    bytes.fromhex('00029d690a4ef08007111000c0040f18b04003d1dd7151'),
    bytes.fromhex('00029d690a62a040011010b0810003008080057ddd7151'),
    bytes.fromhex('00029d690a72a0810414504001051c2860c0060cdd7151'),
]

_CAPTURED_DE36_15 = [
    bytes.fromhex('00029c688ba4a080071110804102161c00c105c7de3615'),
    bytes.fromhex('00029c688ba4b081040f14000101034040c00001de3615'),
    bytes.fromhex('00029c688ba4c040030600808102054040c105abde3615'),
    bytes.fromhex('00029c688ba4f040071a24d080011050f04007c3de3615'),
    bytes.fromhex('00029d686ba470c10315103001011b4870c0046cde3615'),
    bytes.fromhex('00029d686ba4a0810612504001011804804005f0de3615'),
    bytes.fromhex('00029d686ba4e081021d08b08102153050000438de3615'),
    bytes.fromhex('00029d688aa8e041000814b04003157ca08007e1de3615'),
]


class BleDeviceMopekaStdTests(BleDeviceBaseTests):
    """Run with: python3 -m unittest test_ble_device_mopeka_std"""

    def setUp(self):
        # MAC matches the trailing three bytes of the captured frames so
        # ``check_manufacturer_data`` accepts them.
        super().setUp(BleDeviceMopekaStd, 'fc45c3dd7151')

    # -- Static helpers -----------------------------------------------

    def test_unpack_5bit_zero(self):
        # 5 zero bytes => 8 zero fields.
        self.assertEqual(BleDeviceMopekaStd._unpack_5bit_chunk(b'\x00\x00\x00\x00\x00'),
                         [0, 0, 0, 0, 0, 0, 0, 0])

    def test_unpack_5bit_all_ones(self):
        # 0xFF bytes => every 5-bit field is 0x1F.
        self.assertEqual(BleDeviceMopekaStd._unpack_5bit_chunk(b'\xFF\xFF\xFF\xFF\xFF'),
                         [0x1F] * 8)

    def test_unpack_5bit_known_pattern(self):
        # Hand-picked: pack [0..7] into a 40-bit LE word and verify both
        # directions agree.
        fields = [1, 2, 3, 4, 5, 6, 7, 8]
        bits = 0
        for i, f in enumerate(fields):
            bits |= (f & 0x1F) << (5 * i)
        chunk = bits.to_bytes(5, 'little')
        self.assertEqual(BleDeviceMopekaStd._unpack_5bit_chunk(chunk), fields)

    def test_find_best_time_picks_strongest(self):
        # Build a sensor table where the only non-zero amplitude is at the
        # third (time, value) slot, value=20 with time deltas summing to 6.
        fields_chunk_0 = [1, 0, 2, 0, 3, 20, 0, 0]   # accumulated time 6 at value=20
        fields_chunk_1 = [0] * 8
        fields_chunk_2 = [0] * 8

        def pack(fs):
            v = 0
            for i, f in enumerate(fs):
                v |= (f & 0x1F) << (5 * i)
            return v.to_bytes(5, 'little')

        sensor_table = pack(fields_chunk_0) + pack(fields_chunk_1) + pack(fields_chunk_2)
        best_time, best_value, usable = BleDeviceMopekaStd._find_best_time(sensor_table)
        # Each encoded time gets +1 in the algorithm: 1+1=2, 2+1=3, 3+1=4 → sum=9.
        self.assertEqual(best_time, 9)
        self.assertEqual(best_value, 20)
        self.assertEqual(usable, 1)

    def test_find_best_time_no_echo(self):
        sensor_table = b'\x00' * 15
        self.assertEqual(BleDeviceMopekaStd._find_best_time(sensor_table), (0, 0, 0))

    def test_find_best_time_strongest_among_many(self):
        # Two echoes — the strongest one wins.
        fields_chunk_0 = [0, 7, 0, 12, 0, 5, 0, 0]
        fields_chunk_1 = [0] * 8
        fields_chunk_2 = [0] * 8

        def pack(fs):
            v = 0
            for i, f in enumerate(fs):
                v |= (f & 0x1F) << (5 * i)
            return v.to_bytes(5, 'little')

        sensor_table = pack(fields_chunk_0) + pack(fields_chunk_1) + pack(fields_chunk_2)
        best_time, best_value, usable = BleDeviceMopekaStd._find_best_time(sensor_table)
        self.assertEqual(best_value, 12)
        self.assertEqual(usable, 3)

    # -- Speed of sound formula --------------------------------------

    def test_speed_of_sound_pure_propane_zero_c(self):
        # All non-T0 terms vanish at T=0, r=0; left with the constant.
        self.assertAlmostEqual(BleDeviceMopekaStd._speed_of_sound_lpg(0.0, 0.0), 1040.71, places=2)

    def test_speed_of_sound_drops_with_temperature(self):
        # The first temperature term has a negative coefficient; warmer => slower.
        cold = BleDeviceMopekaStd._speed_of_sound_lpg(0.0, 0.0)
        warm = BleDeviceMopekaStd._speed_of_sound_lpg(40.0, 0.0)
        self.assertGreater(cold, warm)

    # -- Frame validation --------------------------------------------

    def test_check_manufacturer_data_accepts_real_frame(self):
        raw = _CAPTURED_DD71_51[0]
        self.device.configure(raw)
        self.device._load_configuration()
        self.assertTrue(self.device.check_manufacturer_data(raw))

    def test_check_manufacturer_data_wrong_length(self):
        self.device.configure(_CAPTURED_DD71_51[0])
        self.device._load_configuration()
        self.assertFalse(self.device.check_manufacturer_data(b'\x00\x02\x00'))

    def test_check_manufacturer_data_wrong_sensor_type(self):
        # Take a real frame and corrupt the sensor type byte.
        raw = bytearray(_CAPTURED_DD71_51[0])
        raw[1] = 0x77  # not in SENSOR_TYPES
        self.device.configure(_CAPTURED_DD71_51[0])
        self.device._load_configuration()
        self.assertFalse(self.device.check_manufacturer_data(bytes(raw)))

    def test_check_manufacturer_data_mac_trailer_mismatch(self):
        raw = bytearray(_CAPTURED_DD71_51[0])
        raw[-1] = (raw[-1] ^ 0xFF) & 0xFF  # break the MAC trailer
        self.device.configure(_CAPTURED_DD71_51[0])
        self.device._load_configuration()
        self.assertFalse(self.device.check_manufacturer_data(bytes(raw)))

    # -- Real-frame parsing (sensor #1) ------------------------------

    def test_real_frame_dd7151_basic_fields(self):
        # 00 02 9d 69 ...
        # byte[1]=0x02 -> HardwareID & 0xCF = 0x02
        # byte[2]=0x9d -> raw battery 157
        #   voltage = 157/256*2 + 1.5 = 2.7265625 V
        # byte[3]=0x69 -> raw_temp (low 6 bits) = 0x29 = 41, slow=0, sync=0
        #   temperature = (41-25) * 1.776964 ≈ 28.43 °C
        raw = _CAPTURED_DD71_51[0]
        self.device.configure(raw)
        self.device._load_configuration()
        self.assertTrue(self.device.check_manufacturer_data(raw))

        parsed = self.device._parse_manufacturer_data(raw)
        # _parse_manufacturer_data returns role-keyed dicts; the std driver
        # publishes only the tank role.
        tank = parsed['tank']
        self.assertEqual(tank['HardwareID'], 0x02)
        # The bit-decoded ToF table is stashed for update_data.
        self.assertIn('BestTime', tank)
        self.assertIn('BestValue', tank)
        self.assertIn('UsableMeasurements', tank)

    def test_real_frame_dd7151_all_pass_validation(self):
        for raw in _CAPTURED_DD71_51:
            self.device.configure(raw)
            self.device._load_configuration()
            self.assertTrue(self.device.check_manufacturer_data(raw),
                            msg=f"frame rejected: {raw.hex()}")

    # -- Real-frame parsing (sensor #2, different MAC) ---------------

    def test_real_frame_de3615_passes_validation(self):
        device = BleDeviceMopekaStd('fc45c3de3615')
        device.configure(_CAPTURED_DE36_15[0])
        device._load_configuration()
        for raw in _CAPTURED_DE36_15:
            self.assertTrue(device.check_manufacturer_data(raw),
                            msg=f"frame rejected: {raw.hex()}")

    def test_real_frame_de3615_rejected_against_wrong_mac(self):
        # Same frames must fail check_manufacturer_data when configured with
        # the *other* sensor's MAC — guards against accidentally cross-wiring
        # devices that happen to share the manufacturer id.
        for raw in _CAPTURED_DE36_15:
            self.assertFalse(self.device.check_manufacturer_data(raw),
                             msg=f"frame from DE:36:15 wrongly accepted on DD:71:51 device: {raw.hex()}")


if __name__ == '__main__':
    unittest.main()
