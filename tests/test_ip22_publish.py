"""
Behaviour tests for IP22 publish paths and the charger role surface.

These pin three things we changed for the gui-v2 honesty pass:

1. ``ble_role_charger.py`` registers no ``/Mode``,
   ``/Capabilities/HasNoDeviceOffMode``, or ``/DeviceOffReason`` on a
   ``charger.*`` role.  PageAcCharger.qml gates its Switch widget on
   ``dataItem.valid`` so absent ``/Mode`` makes the row disappear.
2. ``_publish_off_state`` writes ``None`` to ``/Dc/0/{Voltage,Current,
   Power,Temperature}`` and ``/Ac/In/L1/I``, never ``0.0``.  The
   off-state advertisement carries no measurements; we don't fabricate.
3. ``_publish`` with full telemetry never touches ``/Mode``, regardless
   of advertised state.  Stale ``/Dc/0/*`` from a previous tick is
   actively cleared when the new tick has a missing field.
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _CapturingService:
    """Captures every ``s.add_path(...)`` call from a role's ``init()``."""

    def __init__(self):
        self.paths: dict[str, dict] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def add_path(self, path, value, writeable=False, onchangecallback=None):
        self.paths[path] = {
            "initial": value,
            "writeable": writeable,
            "onchangecallback": onchangecallback,
        }

class _FakeRoleService:
    """Dict-by-path role-service stand-in."""

    def __init__(self, ble_role_name="charger"):
        self.values: dict[str, object] = {}
        # dual-shape: subscript and ble_role attribute
        self.ble_role = types.SimpleNamespace(NAME=ble_role_name)

    def __setitem__(self, key, value):
        self.values[key] = value

    def __getitem__(self, key):
        return self.values[key]

    def __contains__(self, key):
        return key in self.values

    def connect(self):  # role_service.connect() is a no-op here
        pass

# ---------------------------------------------------------------------------
# 1. Role surface:  /Mode, /Capabilities/HasNoDeviceOffMode, /DeviceOffReason
#    must NOT be registered on the IP22 charger role.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def role_module():
    """Import ble_role_charger with a stubbed BleRole base."""
    if "ble_role" not in sys.modules:
        ble_role = types.ModuleType("ble_role")
    else:
        ble_role = sys.modules["ble_role"]
    if not hasattr(ble_role, "BleRole"):
        class _BleRoleBase:
            def __init__(self):
                self.info = {}
        ble_role.BleRole = _BleRoleBase
        sys.modules["ble_role"] = ble_role
    return importlib.import_module("ble_role_charger")

def _initialise_role(role_module):
    role = role_module.BleRoleCharger()
    svc = _CapturingService()
    rs = types.SimpleNamespace(
        _dbus_service=svc,
        _ble_device=types.SimpleNamespace(
            # No `_ip22_*` handlers on this stub; _bind() degrades to
            # store-only callbacks.  That's enough to exercise
            # add_path() instrumentation without a real device class.
        ),
    )
    role.init(rs)
    return svc.paths

def test_charger_role_does_not_publish_mode(role_module):
    paths = _initialise_role(role_module)
    assert "/Mode" not in paths

def test_charger_role_does_not_publish_has_no_device_off_mode(role_module):
    paths = _initialise_role(role_module)
    assert "/Capabilities/HasNoDeviceOffMode" not in paths

def test_charger_role_does_not_publish_device_off_reason(role_module):
    paths = _initialise_role(role_module)
    assert "/DeviceOffReason" not in paths

def test_charger_role_publishes_state_and_dc_paths(role_module):
    paths = _initialise_role(role_module)
    # Sanity:  the role still exposes the parts gui-v2 / DVCC / vrmlogger
    # actually need.  Failure here means we deleted too much.
    for required in ("/State", "/ErrorCode", "/Relay/0/State",
                     "/Dc/0/Voltage", "/Dc/0/Current", "/Dc/0/Power",
                     "/Link/ChargeCurrent", "/Link/ChargeVoltage",
                     "/Link/NetworkStatus"):
        assert required in paths, f"missing required path {required}"

# ---------------------------------------------------------------------------
# 2.  IP22 _publish_off_state:  None, never 0.0, and no /Mode write.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ip22_module():
    """Import ble_device_ip22_charger with a chain of stubs."""
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
                 "ble_role", "ip22_key_settings", "orion_tr_pin",
                 "orion_tr_key_settings", "dbus_settings_service"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    sys.modules["ble_device"].BleDevice = type("BleDevice", (), {
        "MANUFACTURER_ID": None,
        "DEVICE_CLASSES": {},
        "info": {},
    })
    sys.modules["dbus_role_service"].DbusRoleService = type(
        "DbusRoleService", (), {})
    # DbusBleService.get() returns an object with is_device_role_enabled()
    # and is_device_enabled(); both default to True.  Force-override
    # any pre-existing stub from a sibling test module — pytest may
    # have imported a barebones placeholder before this fixture runs.
    class _StubBleSvc:
        @staticmethod
        def get():
            return _StubBleSvc()
        def is_device_role_enabled(self, _info, _name):
            return True
        def is_device_enabled(self, _info):
            return True
    sys.modules["dbus_ble_service"].DbusBleService = _StubBleSvc
    # If ble_device_ip22_charger was imported by an earlier test fixture
    # (with a less capable DbusBleService stub), patch the module-level
    # name there so its `_publish` path uses our stub.
    _existing = sys.modules.get("ble_device_ip22_charger")
    if _existing is not None:
        _existing.DbusBleService = _StubBleSvc
    sys.modules["dbus_settings_service"].DbusSettingsService = type(
        "DbusSettingsService", (), {"__init__": lambda self: None})
    for fn in ("advertisement_key_setting_path", "get_advertisement_key",
               "get_firmware_version", "get_preferred_adapter",
               "set_advertisement_key", "set_firmware_version",
               "set_preferred_adapter"):
        setattr(sys.modules["ip22_key_settings"], fn, lambda *a, **kw: None)
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

    return importlib.import_module("ble_device_ip22_charger")

def _make_device(ip22_module):
    """Build a minimally-functional BleDeviceIP22Charger.

    We bypass __init__'s heavy chain (DbusSettingsService, super().__init__,
    etc.) and wire just enough state for the publish methods to run.
    """
    device = ip22_module.BleDeviceIP22Charger.__new__(
        ip22_module.BleDeviceIP22Charger)
    device.info = {
        "dev_mac": "ed474d2a7c2a",
        "product_id": 0xA330,
        "serial": "HQ2133XMU6Y",
        "product_name": "BSC IP22 12/30",
    }
    device._dbus_settings = None
    device._role_services = {"charger": _FakeRoleService("charger")}
    device._stored_key_invalid = False
    device._last_full_telemetry_at = 0.0
    device._last_advertised_state = 3
    device._plog = "test:ip22"   # used in mixin debug logging on KeyError
    # ChargerCommonMixin state — call _init_charger_common to set up.
    device._init_charger_common()
    return device

def test_publish_off_state_writes_none_for_dc_paths(ip22_module):
    device = _make_device(ip22_module)
    role = device._role_services["charger"]

    # Seed stale values so the function has something to clear.
    role.values["/Dc/0/Voltage"] = 13.5
    role.values["/Dc/0/Current"] = 12.3
    role.values["/Dc/0/Power"] = 166.05
    role.values["/Dc/0/Temperature"] = 28.0
    role.values["/Ac/In/L1/I"] = 0.7

    device._publish_off_state()

    assert role.values["/Dc/0/Voltage"] is None
    assert role.values["/Dc/0/Current"] is None
    assert role.values["/Dc/0/Power"] is None
    assert role.values["/Dc/0/Temperature"] is None
    assert role.values["/Ac/In/L1/I"] is None

def test_publish_off_state_does_not_publish_mode(ip22_module):
    device = _make_device(ip22_module)
    role = device._role_services["charger"]

    device._publish_off_state()

    assert "/Mode" not in role.values, (
        "_publish_off_state must not write /Mode — IP22 firmware has no "
        "remote on/off; the role intentionally omits the path.")

def test_publish_off_state_sets_state_to_zero(ip22_module):
    device = _make_device(ip22_module)
    role = device._role_services["charger"]
    device._publish_off_state()
    assert role.values["/State"] == 0

# ---------------------------------------------------------------------------
# 3.  IP22 _publish:  no /Mode write, missing fields clear stale.
# ---------------------------------------------------------------------------

def _telemetry(state, v1=None, i1=None, **extras):
    """Build a parsed-advertisement dict the way _decode_advertisement
    would.  Matches the schema in ble_device_ip22_charger._publish."""
    base = {
        "device_state": state,
        "charger_error": 0,
        "output_voltage1": v1,
        "output_voltage2": None,
        "output_voltage3": None,
        "output_current1": i1,
        "output_current2": None,
        "output_current3": None,
        "temperature": None,
        "ac_current": None,
        "model_name": "BSC IP22 12/30",
    }
    base.update(extras)
    return base

def test_publish_running_telemetry_does_not_set_mode(ip22_module):
    device = _make_device(ip22_module)
    role = device._role_services["charger"]

    device._publish(_telemetry(state=3, v1=14.4, i1=12.0))

    assert "/Mode" not in role.values

def test_publish_off_telemetry_does_not_set_mode(ip22_module):
    device = _make_device(ip22_module)
    role = device._role_services["charger"]

    device._publish(_telemetry(state=0, v1=0.0, i1=0.0))

    assert "/Mode" not in role.values

def test_publish_clears_stale_dc_paths_when_field_missing(ip22_module):
    device = _make_device(ip22_module)
    role = device._role_services["charger"]

    # First tick: full data lands.
    device._publish(_telemetry(state=3, v1=14.4, i1=12.0))
    assert role.values["/Dc/0/Voltage"] == 14.4
    assert role.values["/Dc/0/Current"] == 12.0
    assert role.values["/Dc/0/Power"] == 14.4 * 12.0

    # Second tick: voltage drops out of the parsed dict (e.g. firmware
    # reset or partial frame).  /Dc/0/Voltage and /Dc/0/Power must
    # actively clear, not retain the stale 14.4.
    device._publish(_telemetry(state=3, v1=None, i1=12.0))
    assert role.values["/Dc/0/Voltage"] is None
    assert role.values["/Dc/0/Power"] is None
    assert role.values["/Dc/0/Current"] == 12.0

def test_publish_running_telemetry_sets_dc_paths(ip22_module):
    device = _make_device(ip22_module)
    role = device._role_services["charger"]

    device._publish(_telemetry(state=4, v1=14.7, i1=15.0))

    assert role.values["/Dc/0/Voltage"] == 14.7
    assert role.values["/Dc/0/Current"] == 15.0
    assert role.values["/Dc/0/Power"] == round(14.7 * 15.0, 2)
    assert role.values["/State"] == 4
