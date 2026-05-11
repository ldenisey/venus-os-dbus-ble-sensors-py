"""
Orion-TR advertisement-filter and product-name-parser tests.

Covers:
  - is_orion_tr_manufacturer_data() against captured byte streams
  - the cross-driver path that battery_voltage_from_model() walks for
    Orion-TR naming (``"Orion Smart 12V/24V-15A …"`` and the older
    ``"Orion-TR Smart 12/24-10A"`` shorthand)

The Orion-TR's product-id range is 0xA3C0–0xA3DF, distinct from IP22's
0xA330–0xA33F, so the two filters are mutually exclusive.
"""
from __future__ import annotations

import sys
import types

import pytest

import ble_charger_common as bcc
from fixtures.captured_advertisements import (
    IP22_FULL_TELEMETRY_HEX_SAMPLES,
    IP22_SHORT_BEACON_HEX,
    ORION_TR_FULL_TELEMETRY_HEX_SAMPLES,
    ORION_TR_PRODUCT_NAMES,
)

@pytest.fixture(scope="module")
def orion_module():
    """Import ble_device_orion_tr with the same stubs the IP22 module
    test uses.  Sharing the stub set keeps both modules importable in
    a vanilla Python environment."""
    if "vedbus" not in sys.modules:
        vedbus = types.ModuleType("vedbus")
        vedbus.VeDbusItemImport = type("VeDbusItemImport", (), {})
        vedbus.VeDbusItemExport = type("VeDbusItemExport", (), {})
        vedbus.VeDbusService = type("VeDbusService", (), {})
        sys.modules["vedbus"] = vedbus
    if "dbus_bus" not in sys.modules:
        dbus_bus = types.ModuleType("dbus_bus")
        dbus_bus.get_bus = lambda _name: types.SimpleNamespace(
            list_names=lambda: [_name])
        sys.modules["dbus_bus"] = dbus_bus
    for name in ("dbus_ble_service", "dbus_role_service", "ble_device",
                 "ble_role", "orion_tr_pin", "orion_tr_key_settings",
                 "ip22_key_settings"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    sys.modules["ble_device"].BleDevice = type("BleDevice", (), {
        "MANUFACTURER_ID": None,
        "DEVICE_CLASSES": {},
        "info": {},
    })
    sys.modules["ble_role"].BleRole = type("BleRole", (), {})
    sys.modules["dbus_role_service"].DbusRoleService = type(
        "DbusRoleService", (), {})
    sys.modules["dbus_ble_service"].DbusBleService = type(
        "DbusBleService", (), {})
    for fn in ("get_advertisement_key", "set_advertisement_key",
               "get_firmware_version", "set_firmware_version",
               "get_preferred_adapter", "set_preferred_adapter",
               "advertisement_key_setting_path"):
        setattr(sys.modules["orion_tr_key_settings"], fn,
                lambda *a, **kw: None)
    sys.modules["orion_tr_pin"].resolve_pairing_passkey = lambda _s: 14916

    if "ve_types" not in sys.modules:
        vt = types.ModuleType("ve_types")
        vt.VE_UN8 = int
        sys.modules["ve_types"] = vt

    try:
        import victron_ble  # noqa: F401
        from victron_ble.devices import detect_device_type  # noqa: F401
    except Exception:
        vb = types.ModuleType("victron_ble")
        vb_devices = types.ModuleType("victron_ble.devices")
        vb_devices.detect_device_type = lambda _b: None
        vb_exc = types.ModuleType("victron_ble.exceptions")
        vb_exc.AdvertisementKeyMismatchError = type(
            "AdvertisementKeyMismatchError", (Exception,), {})
        sys.modules["victron_ble"] = vb
        sys.modules["victron_ble.devices"] = vb_devices
        sys.modules["victron_ble.exceptions"] = vb_exc

    import importlib
    return importlib.import_module("ble_device_orion_tr")

# ---------------------------------------------------------------------------
# is_orion_tr_manufacturer_data — structural filter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hex_payload", ORION_TR_FULL_TELEMETRY_HEX_SAMPLES)
def test_is_orion_tr_full_telemetry_passes(orion_module, hex_payload):
    data = bytes.fromhex(hex_payload)
    assert orion_module.is_orion_tr_manufacturer_data(data)

def test_is_orion_tr_short_payload_rejected(orion_module):
    # Less than 4 bytes — no product id at all.
    assert not orion_module.is_orion_tr_manufacturer_data(b"\x10\x00")
    assert not orion_module.is_orion_tr_manufacturer_data(b"")

def test_is_orion_tr_ip22_payload_rejected(orion_module):
    """IP22 product id (0xA330) must not slip through the Orion-TR
    filter — that would route the wrong device class."""
    assert not orion_module.is_orion_tr_manufacturer_data(
        bytes.fromhex(IP22_SHORT_BEACON_HEX))
    for hex_p in IP22_FULL_TELEMETRY_HEX_SAMPLES:
        assert not orion_module.is_orion_tr_manufacturer_data(
            bytes.fromhex(hex_p))

def test_is_orion_tr_product_id_range_boundaries(orion_module):
    # 0xA3C0 (lower bound) → pass
    assert orion_module.is_orion_tr_manufacturer_data(
        bytes.fromhex("1000c0a3"))
    # 0xA3DF (upper bound) → pass
    assert orion_module.is_orion_tr_manufacturer_data(
        bytes.fromhex("1000dfa3"))
    # 0xA3BF (just below) → reject
    assert not orion_module.is_orion_tr_manufacturer_data(
        bytes.fromhex("1000bfa3"))
    # 0xA3E0 (just above) → reject
    assert not orion_module.is_orion_tr_manufacturer_data(
        bytes.fromhex("1000e0a3"))

# ---------------------------------------------------------------------------
# Shared parser: battery_voltage_from_model() handles both naming
# conventions correctly.  These tests cover gap #3.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pid,expected", [
    (0xA3C0, 12),   # 12/12 — output is 12V
    (0xA3C1, 24),   # 12/24 — output 24V (house bank)
    (0xA3C2, 48),   # 12/48 — output 48V
    (0xA3C9, 24),   # "Orion Smart 12V/24V-15A DC-DC Converter" (bench unit)
    (0xA3D0, 12),   # 24/12 — input 24V, output 12V
    (0xA3D5, 24),   # 48/24 — output 24V
    (0xA3D6, 48),   # 48/48 — output 48V
])
def test_orion_tr_battery_voltage_table(pid, expected):
    name = ORION_TR_PRODUCT_NAMES[pid]
    assert bcc.battery_voltage_from_model(name) == expected

def test_orion_tr_battery_voltage_with_v_suffix():
    """The bench-unit naming uses ``"12V/24V-15A"`` with explicit V
    suffix — the regex must match both with and without the suffix."""
    assert bcc.battery_voltage_from_model(
        "Orion Smart 12V/24V-15A DC-DC Converter") == 24

def test_orion_tr_battery_voltage_old_shorthand():
    """The older firmware advertises shorthand without V suffixes."""
    assert bcc.battery_voltage_from_model(
        "Orion-TR Smart 12/24-10A") == 24
    assert bcc.battery_voltage_from_model(
        "Orion-TR Smart 24/12-20A") == 12

def test_orion_tr_battery_voltage_returns_none_on_unknown_voltage():
    """An imaginary 6V or 96V model — not a canonical Victron rail —
    must produce None rather than a misleading value."""
    assert bcc.battery_voltage_from_model(
        "Orion-TR Smart 12/6-30A") is None
    assert bcc.battery_voltage_from_model(
        "Orion-TR Smart 12/96-3A") is None

def test_battery_voltage_disambiguates_orion_vs_charger():
    """The IP22's ``"Charger 12|30 (1)"`` and the Orion-TR's
    ``"Smart 12/24-10A"`` must not collide.  This regression guard
    catches a parser that picks up the first integer after any
    keyword."""
    # IP22 12/30 — first integer is V (12), second is A (30)
    assert bcc.battery_voltage_from_model(
        "Blue Smart IP22 Charger 12|30 (1)") == 12
    assert bcc.battery_voltage_from_model(
        "Blue Smart IP22 Charger 12/30 (1)") == 12
    # Orion-TR 12/24 — first is Vin, second is Vout (battery side)
    assert bcc.battery_voltage_from_model(
        "Orion-TR Smart 12/24-10A") == 24

def test_battery_voltage_falls_back_to_pid_table_orion():
    assert bcc.battery_voltage_from_model(
        None, pid_table=ORION_TR_PRODUCT_NAMES, pid=0xA3C9) == 24

def test_battery_voltage_unknown_pid_returns_none():
    assert bcc.battery_voltage_from_model(
        None, pid_table=ORION_TR_PRODUCT_NAMES, pid=0xFFFF) is None
