"""
Pure-helper coverage for ble_charger_common.

These tests exercise only the module-level functions and constants —
no instance state, no D-Bus, no GATT.  They are the cheapest tier of
defence against regressions in the shared infrastructure.
"""
from __future__ import annotations

import pytest

import ble_charger_common as bcc
from fixtures.captured_advertisements import (
    BLUEZ_NAMES,
    IP22_PRODUCT_NAMES,
    ORION_TR_PRODUCT_NAMES,
)

# ---------------------------------------------------------------------------
# serial_from_advertised_name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key,expected", [
    ("ip22_long",  "HQ2133XMU6Y"),
    ("ip22_short", "HQ2133XMU6Y"),
    ("ip22_other", "HQ2133CG4QA"),
    ("orion_tr",   "HQ20326VVVJ"),
    ("no_serial",  None),
])
def test_serial_from_advertised_name_real_captures(key, expected):
    assert bcc.serial_from_advertised_name(BLUEZ_NAMES[key]) == expected

def test_serial_from_advertised_name_none_input():
    assert bcc.serial_from_advertised_name(None) is None

def test_serial_from_advertised_name_empty():
    assert bcc.serial_from_advertised_name("") is None

def test_serial_from_advertised_name_too_short():
    # "HQ" prefix is not enough — regex demands at least 8 alphanumerics
    assert bcc.serial_from_advertised_name("HQ123") is None

def test_serial_from_advertised_name_first_match_wins():
    # Two HQ tokens — first one is returned.
    txt = "Blue Smart BL HQ11111111 also HQ22222222"
    assert bcc.serial_from_advertised_name(txt) == "HQ11111111"

# ---------------------------------------------------------------------------
# alarms_for_error
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code,expected", [
    (0,  {}),                                    # NO_ERROR
    (1,  {}),                                    # battery-temp errors stay in /ErrorCode
    (2,  {"/Alarms/HighVoltage": 2}),            # VOLTAGE_HIGH
    (11, {"/Alarms/HighRipple": 2}),             # HIGH_RIPPLE
    (14, {}),                                    # battery-temp errors stay in /ErrorCode
    (17, {"/Alarms/HighTemperature": 2}),        # TEMPERATURE_CHARGER
    (18, {}),                                    # OVER_CURRENT — no alarm path
    (22, {"/Alarms/HighTemperature": 2}),        # INTERNAL_TEMPERATURE_A
    (23, {"/Alarms/HighTemperature": 2}),        # INTERNAL_TEMPERATURE_B
    (24, {"/Alarms/Fan": 2}),                    # FAN
    (26, {"/Alarms/HighTemperature": 2}),        # OVERHEATED
    (27, {}),                                    # SHORT_CIRCUIT — no alarm path
])
def test_alarms_for_error_table(code, expected):
    assert bcc.alarms_for_error(code) == expected

def test_alarms_for_error_unknown_code_returns_empty():
    # Codes not in the map should return empty dict, not raise.
    assert bcc.alarms_for_error(255) == {}
    assert bcc.alarms_for_error(99) == {}

def test_charger_alarm_paths_complete():
    """Every value-side path in the error map must be declared."""
    declared = set(bcc.CHARGER_ALARM_PATHS)
    used: set[str] = set()
    for mapping in bcc.CHARGER_ERROR_TO_ALARMS.values():
        used |= set(mapping.keys())
    missing = used - declared
    assert not missing, f"alarm paths used in map but not declared: {missing}"

def test_charger_alarm_paths_no_battery_state():
    """Regression guard: battery-monitor / inverter alarm paths must
    NOT appear here.  The charger isn't authoritative on battery state."""
    forbidden = {
        "/Alarms/HighBatteryTemperature",
        "/Alarms/LowBatteryTemperature",
        "/Alarms/LowVoltage",
        "/Alarms/LowSoc",
        "/Alarms/Overload",
        "/Alarms/Ripple",
        "/Alarms/LoadDisconnect",
        "/Alarms/VecanDisconnected",
    }
    assert not (set(bcc.CHARGER_ALARM_PATHS) & forbidden)

# ---------------------------------------------------------------------------
# encode_u16_le_scaled
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("v,scale,expected", [
    (14.4,  100, b"\xa0\x05"),    # 1440 -> 0x05a0
    (28.5,  100, b"\x22\x0b"),    # 2850 -> 0x0b22
    (28.6,  100, b"\x2c\x0b"),    # 2860 -> 0x0b2c
    (27.0,  100, b"\x8c\x0a"),    # 2700 -> 0x0a8c
    (28.40, 100, b"\x18\x0b"),    # 2840 -> 0x0b18
    (18.0,  10,  b"\xb4\x00"),    # 180  -> 0x00b4
    (22.5,  10,  b"\xe1\x00"),    # 225  -> 0x00e1
    (0,     100, b"\x00\x00"),
])
def test_encode_u16_le_scaled_real_setpoints(v, scale, expected):
    assert bcc.encode_u16_le_scaled(v, scale) == expected

def test_encode_u16_le_scaled_rounds():
    # 14.456 -> 1446 (banker's-round neutral; Python rounds half-even)
    assert bcc.encode_u16_le_scaled(14.456, 100) == bytes([0xa6, 0x05])

@pytest.mark.parametrize("v", [-0.1, 656.36, 1000.0])
def test_encode_u16_le_scaled_out_of_range_returns_none(v):
    assert bcc.encode_u16_le_scaled(v, 100) is None

def test_encode_u16_le_scaled_invalid_input():
    assert bcc.encode_u16_le_scaled("not a number", 100) is None
    assert bcc.encode_u16_le_scaled(None, 100) is None

def test_encode_u16_le_scaled_custom_max():
    # Allow caller to constrain to a tighter range.
    assert bcc.encode_u16_le_scaled(15.0, 10, max_value=200) == b"\x96\x00"
    assert bcc.encode_u16_le_scaled(25.0, 10, max_value=200) is None

# ---------------------------------------------------------------------------
# settings_path
# ---------------------------------------------------------------------------

def test_settings_path_ip22_with_colons():
    p = bcc.settings_path("ip22", "ED:47:4D:2A:7C:2A", "ChargeCurrentLimit")
    assert p == "/Settings/Devices/ip22_ed474d2a7c2a/ChargeCurrentLimit"

def test_settings_path_ip22_no_colons():
    p = bcc.settings_path("ip22", "ed474d2a7c2a", "AbsorptionVoltage")
    assert p == "/Settings/Devices/ip22_ed474d2a7c2a/AbsorptionVoltage"

def test_settings_path_orion_tr():
    p = bcc.settings_path("orion_tr", "FF:13:42:2B:7A:4B", "FloatVoltage")
    assert p == "/Settings/Devices/orion_tr_ff13422b7a4b/FloatVoltage"

def test_settings_path_history_subfolder():
    p = bcc.settings_path("ip22", "ED:47:4D:2A:7C:2A",
                          "History/OperationTime")
    assert p == "/Settings/Devices/ip22_ed474d2a7c2a/History/OperationTime"

def test_settings_path_lowercases_mac():
    # Even if a caller hands us a fully upper-case MAC, the namespace
    # component is lowercased — so all paths converge.
    a = bcc.settings_path("ip22", "ED:47:4D:2A:7C:2A", "X")
    b = bcc.settings_path("ip22", "ed:47:4d:2a:7c:2a", "X")
    assert a == b

# ---------------------------------------------------------------------------
# format_mac_colons
# ---------------------------------------------------------------------------

def test_format_mac_colons_round_trip():
    assert bcc.format_mac_colons("ed474d2a7c2a") == "ED:47:4D:2A:7C:2A"
    assert bcc.format_mac_colons("ED:47:4D:2A:7C:2A") == "ED:47:4D:2A:7C:2A"

def test_format_mac_colons_orion():
    assert bcc.format_mac_colons("ff13422b7a4b") == "FF:13:42:2B:7A:4B"

# ---------------------------------------------------------------------------
# Constants integrity
# ---------------------------------------------------------------------------

def test_state_external_control_value():
    """gui-v2 / dbus-systemcalc-py expect 252 — must not drift."""
    assert bcc.STATE_EXTERNAL_CONTROL == 252

def test_history_tick_states_canonical():
    """OperationMode values that count as actively charging."""
    assert bcc.HISTORY_TICK_STATES == frozenset({3, 4, 5, 6, 7, 247})

def test_dvcc_deadbands_match_device_resolution():
    # IP22 max-current is u16 LE in 0.1 A — 0.1 A is the smallest
    # change the device can resolve.  Voltage VREGs are 0.01 V.
    assert bcc.CHARGE_CURRENT_DEADBAND_A == 0.1
    assert bcc.CHARGE_VOLTAGE_DEADBAND_V == 0.05
