"""
IP22 advertisement-filter tests against captured byte streams.

The full ``victron_ble`` decode chain isn't unit-tested here (it lives
in vendor code under ``ext/victron_ble`` and we don't ship test keys).
What this module covers is the *structural* path our driver uses to
decide whether an advertisement belongs to an IP22:

  - is_ip22_charger_manufacturer_data()  — length / product-id / mode-byte gate
  - the captured short-beacon vs full-telemetry distinction

Plus the IP22 product-id → battery-voltage derivation, which depends
purely on the model-name string parser.
"""
from __future__ import annotations

import importlib.util
import sys
import types

import pytest

from fixtures.captured_advertisements import (
    IP22_FULL_TELEMETRY_HEX_SAMPLES,
    IP22_PRODUCT_NAMES,
    IP22_SHORT_BEACON_HEX,
)

@pytest.fixture(scope="module")
def ip22_module():
    """Import ble_device_ip22_charger with stubbed dependencies.

    The module pulls in dbus, vedbus, and a chain of in-repo modules
    at import time.  We provide just enough fakes to satisfy the
    imports we need to reach is_ip22_charger_manufacturer_data() and
    _battery_voltage_for_product().
    """
    # Stub vedbus + dbus_settings_service before any of our module
    # imports trigger a chain.
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

    # Other in-repo modules we don't need to exercise — provide
    # minimal stubs so import doesn't fail.
    for name in ("dbus_ble_service", "dbus_role_service", "ble_device",
                 "ble_role", "ip22_key_settings", "orion_tr_pin",
                 "orion_tr_key_settings"):
        if name not in sys.modules:
            mod = types.ModuleType(name)
            sys.modules[name] = mod

    # Minimal BleDevice base + role-svc class so the module can subclass
    # without complaining.
    sys.modules["ble_device"].BleDevice = type("BleDevice", (), {
        "MANUFACTURER_ID": None,
        "DEVICE_CLASSES": {},
        "info": {},
    })
    sys.modules["dbus_role_service"].DbusRoleService = type(
        "DbusRoleService", (), {})
    sys.modules["dbus_ble_service"].DbusBleService = type(
        "DbusBleService", (), {})
    # Stubs for the symbol functions imported by the module:
    for fn in ("advertisement_key_setting_path", "get_advertisement_key",
               "get_firmware_version", "get_preferred_adapter",
               "set_advertisement_key", "set_firmware_version",
               "set_preferred_adapter"):
        setattr(sys.modules["ip22_key_settings"], fn, lambda *a, **kw: None)
    sys.modules["orion_tr_pin"].resolve_pairing_passkey = lambda _s: 14916

    # ve_types for VE_UN8
    if "ve_types" not in sys.modules:
        vt = types.ModuleType("ve_types")
        vt.VE_UN8 = int
        sys.modules["ve_types"] = vt

    # victron_ble: the real package may import cryptography — stub if absent.
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

    # Now import the module — only need module-level names.
    import importlib
    return importlib.import_module("ble_device_ip22_charger")

# ---------------------------------------------------------------------------
# is_ip22_charger_manufacturer_data — structural filter
# ---------------------------------------------------------------------------

def test_is_ip22_charger_short_beacon_passes(ip22_module):
    data = bytes.fromhex(IP22_SHORT_BEACON_HEX)
    assert ip22_module.is_ip22_charger_manufacturer_data(data)

@pytest.mark.parametrize("hex_payload", IP22_FULL_TELEMETRY_HEX_SAMPLES)
def test_is_ip22_charger_full_telemetry_passes(ip22_module, hex_payload):
    data = bytes.fromhex(hex_payload)
    assert ip22_module.is_ip22_charger_manufacturer_data(data)

def test_is_ip22_charger_too_short_rejected(ip22_module):
    # Less than 4 bytes — not even a product id.
    assert not ip22_module.is_ip22_charger_manufacturer_data(b"\x10\x00\x30")
    assert not ip22_module.is_ip22_charger_manufacturer_data(b"")

def test_is_ip22_charger_wrong_product_id_rejected(ip22_module):
    # 0xA3C9 is Orion-TR, not IP22.
    data = bytes.fromhex("1000c9a3041234567890")
    assert not ip22_module.is_ip22_charger_manufacturer_data(data)

def test_is_ip22_charger_wrong_record_type_rejected(ip22_module):
    # IP22 product id but record-type byte != 0x08 (AcCharger).
    data = bytes.fromhex("100030a304abcdef")
    assert not ip22_module.is_ip22_charger_manufacturer_data(data)

def test_is_ip22_charger_short_beacon_no_record_byte_passes(ip22_module):
    # 4 bytes exactly — no record-type to check, just the prefix.
    # The driver synthesizes off-state from this.
    assert ip22_module.is_ip22_charger_manufacturer_data(
        bytes.fromhex(IP22_SHORT_BEACON_HEX))

def test_is_ip22_charger_product_id_range_boundaries(ip22_module):
    # 0xA330 (lower bound) → pass
    assert ip22_module.is_ip22_charger_manufacturer_data(
        bytes.fromhex("100030a3"))
    # 0xA33F (upper bound) → pass
    assert ip22_module.is_ip22_charger_manufacturer_data(
        bytes.fromhex("10003fa3"))
    # 0xA340 (just above) → reject
    assert not ip22_module.is_ip22_charger_manufacturer_data(
        bytes.fromhex("100040a3"))
    # 0xA32F (just below) → reject
    assert not ip22_module.is_ip22_charger_manufacturer_data(
        bytes.fromhex("10002fa3"))

# ---------------------------------------------------------------------------
# _battery_voltage_for_product — model-name parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pid,expected", [
    (0xA330, 12),
    (0xA331, 12),
    (0xA332, 24),
    (0xA337, 24),
    (0xA33B, 12),
])
def test_battery_voltage_for_product_ip22_table(ip22_module, pid, expected):
    name = IP22_PRODUCT_NAMES[pid]
    assert ip22_module._battery_voltage_for_product(name, pid) == expected

def test_battery_voltage_for_product_handles_slash_separator(ip22_module):
    # Older naming used ``"... 12/30 (1)"`` instead of ``12|30``.
    assert ip22_module._battery_voltage_for_product(
        "Blue Smart IP22 Charger 12/30 (1)", 0xA330) == 12

def test_battery_voltage_for_product_unknown_pid_returns_none(ip22_module):
    # No model name and unknown id — caller should get None back.
    assert ip22_module._battery_voltage_for_product(None, 0xFFFF) is None

def test_battery_voltage_for_product_36v_48v(ip22_module):
    assert ip22_module._battery_voltage_for_product(
        "Phoenix Smart IP43 Charger 36|15 (1) 120-240V", 0xA340) == 36
    assert ip22_module._battery_voltage_for_product(
        "Phoenix Smart IP43 Charger 48|13 (1) 120-240V", 0xA341) == 48

def test_battery_voltage_for_product_falls_back_to_table(ip22_module):
    # If no model_name passed but the product id is in the table,
    # the helper consults _IP22_PRODUCT_NAMES.
    assert ip22_module._battery_voltage_for_product(None, 0xA330) == 12
