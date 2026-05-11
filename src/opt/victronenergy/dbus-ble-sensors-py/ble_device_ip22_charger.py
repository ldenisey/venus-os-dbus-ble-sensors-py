"""
Victron Blue Smart IP22 charger (BLE manufacturer ``0x02E1``, product
IDs ``0xA330``–``0xA33F``).

The IP22 publishes live charger telemetry as encrypted Victron
advertisements, and accepts a ``DEVICE_MODE`` write (VREG ``0x0200``)
over GATT for on/off control — the same protocol already used by the
Orion-TR driver in this service.  The 16-byte advertisement key is
device-specific and must be read once via a paired GATT session; this
driver reuses :mod:`orion_tr_key_cli` to perform that provisioning.

This file mirrors the structure of :mod:`ble_device_orion_tr` but
publishes under a single ``charger`` role so the device appears on
gui-v2's *DC Sources* page alongside the VE.Direct Phoenix Smart IP43
charger reference design.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import struct
import subprocess
import threading

from gi.repository import GLib
import time
from typing import Any, Callable, Dict, Optional

import dbus

from ble_device import BleDevice
from dbus_ble_service import DbusBleService
from dbus_role_service import DbusRoleService
from dbus_settings_service import DbusSettingsService
from victron_ble.devices import detect_device_type  # type: ignore
from victron_ble.exceptions import (  # type: ignore
    AdvertisementKeyMismatchError,
)

from orion_tr_gatt import AsyncGATTWriter
from orion_tr_pin import resolve_pairing_passkey
from ip22_key_settings import (
    advertisement_key_setting_path,
    get_advertisement_key,
    get_firmware_version,
    get_preferred_adapter,
    set_advertisement_key,
    set_firmware_version,
    set_preferred_adapter,
)
from scan_control import pause_scanning, resume_scanning
from ve_types import VE_UN8

# Behaviours shared between IP22 and Orion-TR (in charger / alternator
# role): per-device GATT write queue, settings persistence, history
# accumulators, charger alarms, DVCC engagement + /State=252 override.
from ble_charger_common import (
    ChargerCommonMixin,
    CHARGE_CURRENT_DEADBAND_A,
    CHARGE_VOLTAGE_DEADBAND_V,
    battery_voltage_from_model,
    bluez_device_name as _bluez_device_name,
    encode_u16_le_scaled,
    format_mac_colons as _format_mac_colons,
    serial_from_advertised_name as _serial_from_advertised_name,
)

logger = logging.getLogger(__name__)

VICTRON_MANUFACTURER_ID = 0x02E1
IP22_PRODUCT_ID_MIN = 0xA330
IP22_PRODUCT_ID_MAX = 0xA33F

# IP22 firmware has no writable remote on/off register: the only
# verified-writable real-control register is 0xEDF0 (Battery max
# current).  /Mode / DeviceOffReason are therefore not published on the
# role at all — see ble_role_charger.py for the reasoning.  BMS-style
# control (drop /Link/ChargeVoltage to taper off) works fully through
# 0xEDF7 without needing an on/off VREG.
VREG_BATTERY_MAX_CURRENT = 0xEDF0   # u16 LE, 0.1 A
VREG_BATTERY_TYPE        = 0xEDF1   # u8;  0xFF = USER (unlocks voltage writes)
VREG_FLOAT_VOLTAGE       = 0xEDF6   # u16 LE, 0.01 V
VREG_ABSORPTION_VOLTAGE  = 0xEDF7   # u16 LE, 0.01 V

# Optional charge-profile VREGs that the standard Victron solar /
# Phoenix Smart layout puts in this range — equalize voltage /
# duration, bulk / absorption max time, rebulk threshold.  These are
# *not yet wired* on the IP22 driver: they need an end-to-end probe
# on each firmware variant before we expose writable settings paths
# to gui-v2 (an unknown-VREG write returns ack code 1 and costs a
# scan-pause / connect / disconnect cycle).
#
# Use ``scripts/probe_ip22_optional_vregs.py`` (see commit message)
# to run the probe on a bench unit and confirm which addresses
# respond before extending the role.

# IP22 battery-type sentinel that unlocks user-defined absorption /
# float voltage writes via 0xEDF7 / 0xEDF6.  Probed live: writes to
# 0xEDF7 return code 02 (param error) until 0xEDF1 == 0xFF.
BATTERY_TYPE_USER = 0xFF

# Known IP22 / Blue Smart model spec strings by product id.  Used when the
# vendored ``victron_ble`` package's table doesn't cover a given SKU.
_IP22_PRODUCT_NAMES = {
    0xA330: "Blue Smart IP22 Charger 12|30 (1)",
    0xA331: "Blue Smart IP22 Charger 12|30 (3)",
    0xA332: "Blue Smart IP22 Charger 24|16 (1)",
    0xA333: "Blue Smart IP22 Charger 24|16 (3)",
    0xA334: "Blue Smart IP22 Charger 12|15 (1)",
    0xA335: "Blue Smart IP22 Charger 12|20 (1)",
    0xA336: "Blue Smart IP22 Charger 12|20 (3)",
    0xA337: "Blue Smart IP22 Charger 24|8 (1)",
    0xA338: "Blue Smart IP22 Charger 12|15 (3)",
    0xA339: "Blue Smart IP22 Charger 24|12 (1)",
    0xA33A: "Blue Smart IP22 Charger 24|12 (3)",
    0xA33B: "Blue Smart IP22 Charger 12|10 (1)",
}

_gatt_writer: Optional[AsyncGATTWriter] = None
_provision_lock = threading.Lock()
_provision_busy = False

_KEY_CLI_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "orion_tr_key_cli.py")

def is_ip22_charger_manufacturer_data(manufacturer_data: bytes) -> bool:
    # The IP22 drops its encrypted payload when powered off and advertises
    # a short "product-id only" frame, so accept any length >= 4 as long as
    # the product id is in the IP22 range.  Frames with a full encrypted
    # payload additionally carry mode byte ``0x08`` (AcCharger).
    if len(manufacturer_data) < 4:
        return False
    pid = struct.unpack("<H", manufacturer_data[2:4])[0]
    if not (IP22_PRODUCT_ID_MIN <= pid <= IP22_PRODUCT_ID_MAX):
        return False
    if len(manufacturer_data) >= 5 and manufacturer_data[4] != 0x08:
        return False
    return True

def _shared_bus() -> dbus.Bus:
    return (
        dbus.SessionBus()
        if "DBUS_SESSION_BUS_ADDRESS" in os.environ
        else dbus.SystemBus()
    )

def _gatt() -> AsyncGATTWriter:
    global _gatt_writer
    if _gatt_writer is None:
        _gatt_writer = AsyncGATTWriter(_shared_bus())
    return _gatt_writer

def _run_key_cli(mac: str, passkey: int,
                 timeout_s: float = 60.0,
                 preferred_adapter: Optional[str] = None,
                 ) -> Optional[Dict[str, Any]]:
    cmd = [
        "python3", _KEY_CLI_PATH,
        mac,
        "--passkey", str(passkey),
        "--timeout", str(int(timeout_s)),
    ]
    if preferred_adapter:
        cmd.extend(["--preferred-adapter", preferred_adapter])
    logger.info("Spawning key-provisioner subprocess: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s + 20.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("ip22 key-provisioner subprocess timed out for %s", mac)
        return None
    except Exception:
        logger.exception("failed to spawn ip22 key-provisioner subprocess")
        return None

    if result.returncode != 0:
        logger.warning("ip22 key-provisioner exited %d: %s",
                       result.returncode, (result.stderr or "").strip())
        return None

    raw = (result.stdout or "").strip()
    try:
        payload = json.loads(raw)
    except Exception:
        logger.warning("ip22 key-provisioner non-JSON output: %r", raw)
        return None

    key = str(payload.get("key", "")).strip().lower()
    if len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
        logger.warning("ip22 key-provisioner returned invalid key: %r", key)
        return None
    payload["key"] = key
    return payload

def _format_firmware_version(raw_hex: Optional[str]) -> Optional[str]:
    if not raw_hex:
        return None
    try:
        blob = bytes.fromhex(raw_hex)
    except ValueError:
        return None

    def _bcd_byte(b: int) -> int:
        return ((b >> 4) & 0xF) * 10 + (b & 0xF)

    def _format_low16(value16: int) -> Optional[str]:
        if value16 in (0, 0xFFFF):
            return None
        major = _bcd_byte((value16 >> 8) & 0xFF)
        minor = _bcd_byte(value16 & 0xFF)
        return f"{major}.{minor:02d}"

    if len(blob) == 2:
        v = int.from_bytes(blob, "little")
        s = _format_low16(v)
        if s:
            return s
    if len(blob) == 4:
        v = int.from_bytes(blob, "little")
        if v in (0, 0xFFFFFFFF):
            return raw_hex
        base = _format_low16(v & 0xFFFF)
        if base is None:
            return raw_hex
        kind = (v >> 24) & 0xF0
        suffix = {0x40: "", 0x50: "~beta", 0xF0: "~dev"}.get(kind, "")
        return base + suffix
    return raw_hex

def _battery_voltage_for_product(model_name: Optional[str],
                                 pid: int) -> Optional[int]:
    """IP22-side wrapper around the shared ``battery_voltage_from_model``,
    falling back to the IP22 product-id table when the encrypted-payload
    path doesn't have a model name in scope (e.g. the short-beacon
    off-state path)."""
    return battery_voltage_from_model(
        model_name, pid_table=_IP22_PRODUCT_NAMES, pid=pid)

class BleDeviceIP22Charger(ChargerCommonMixin, BleDevice):
    """Blue Smart IP22 charger driven by encrypted Victron advertisements."""

    # Used in /Settings/Devices/<ns>_<mac>/* paths (see ble_charger_common).
    SETTINGS_NS_PREFIX = "ip22"

    # Some IP22 firmwares interleave a 4-byte "product-id only" beacon
    # alongside the encrypted telemetry advertisement.  When the unit is
    # genuinely off it sends only the short beacon, but a running unit can
    # still emit it occasionally — so honour the short-frame "off" reading
    # only after this many seconds without a successful telemetry decode.
    _OFF_FRAME_GRACE_S = 30.0

    # Settings-suffix → role-path map used by
    # ChargerCommonMixin.load_persisted_charger_settings() at startup.
    PERSISTED_SETTING_SUFFIXES_TO_PATHS = {
        "ChargeCurrentLimit": "/Settings/ChargeCurrentLimit",
        "AbsorptionVoltage":  "/Settings/AbsorptionVoltage",
        "FloatVoltage":       "/Settings/FloatVoltage",
    }

    @staticmethod
    def matches_manufacturer_data(manufacturer_data: bytes) -> bool:
        return is_ip22_charger_manufacturer_data(manufacturer_data)

    def __init__(self, dev_mac: str):
        self._adv_key_hex: Optional[str] = None
        self._dbus_settings = DbusSettingsService()
        self._pairing_passkey: int = resolve_pairing_passkey(
            self._dbus_settings)
        self._last_provision_attempt: float = 0.0
        self._stored_key_invalid = False
        self._last_daily_refresh_date: Optional[str] = None
        self._last_full_telemetry_at: float = 0.0
        # Cached battery-type byte; if it isn't already USER (0xFF) the
        # absorption-voltage write at 0xEDF7 will be rejected with code 02.
        self._battery_type_is_user: Optional[bool] = None
        # GATT queue / history / DVCC engagement / persistence-dedupe
        # state — all initialised by the shared mixin.
        self._init_charger_common()
        super().__init__(dev_mac)

    def configure(self, manufacturer_data: bytes):
        pid = struct.unpack("<H", manufacturer_data[2:4])[0]
        self._adv_key_hex = get_advertisement_key(self._dbus_settings,
                                                  self.info["dev_mac"])
        # Shadow MANUFACTURER_ID the same way Orion-TR does — keep 0x02E1
        # routable to BleDeviceVictronEnergy for SolarSense while still
        # satisfying the base class's per-instance check.
        self.MANUFACTURER_ID = VICTRON_MANUFACTURER_ID
        adv_name = _bluez_device_name(self.info["dev_mac"])
        product_name = (adv_name
                        or _IP22_PRODUCT_NAMES.get(pid)
                        or "Blue Smart IP22 Charger")
        device_name_base = adv_name or "IP22"
        firmware_raw = get_firmware_version(self._dbus_settings,
                                            self.info["dev_mac"])
        firmware_version = _format_firmware_version(firmware_raw) or "1.0.0"
        self.info.update(
            {
                "manufacturer_id": VICTRON_MANUFACTURER_ID,
                "product_id": pid,
                "product_name": product_name,
                "device_name": device_name_base,
                "dev_prefix": "ip22",
                "firmware_version": firmware_version,
                "roles": {"charger": {}},
                "regs": [
                    {
                        "name": "_ip22_placeholder",
                        "type": VE_UN8,
                        "offset": 0,
                        "roles": [None],
                    }
                ],
                "settings": [],
                "alarms": [],
            }
        )

    def init(self):
        super().init()
        adv_name = _bluez_device_name(self.info["dev_mac"])
        if adv_name:
            for role_service in self._role_services.values():
                current = role_service["/CustomName"]
                if not current:
                    self._publish_value(role_service, "/CustomName", adv_name)

    def check_manufacturer_data(self, manufacturer_data: bytes) -> bool:
        return self.matches_manufacturer_data(manufacturer_data)

    def handle_manufacturer_data(self, manufacturer_data: bytes):
        # Adv-arrival counter, gated on env var so it ships in
        # production but only emits log lines when actively debugging.
        # Use ``IP22_ADV_TRACE=1`` in the service's run script when
        # chasing the HCI-tap suppression issue (see
        # docs/IP22-INTEGRATION.md §"HCI-tap suppression").
        if os.environ.get("IP22_ADV_TRACE"):
            if not hasattr(self, "_adv_trace_counts"):
                self._adv_trace_counts: dict[int, int] = {}
                self._adv_trace_last_log = 0.0
            L = len(manufacturer_data)
            self._adv_trace_counts[L] = (
                self._adv_trace_counts.get(L, 0) + 1)
            now = time.monotonic()
            if now - self._adv_trace_last_log > 30.0:
                self._adv_trace_last_log = now
                logger.info(
                    "%s: adv-trace counts (last 30 s+) %s",
                    self._plog, dict(self._adv_trace_counts))
                self._adv_trace_counts = {}

        if not DbusBleService.get().is_device_enabled(self.info):
            return

        if self._stored_key_invalid:
            self._maybe_provision_key()
            return

        key = self._adv_key_hex or get_advertisement_key(
            self._dbus_settings, self.info["dev_mac"])
        if key:
            self._adv_key_hex = key

        if not key:
            self._maybe_provision_key()
            return

        # Short "off" frame: just the product-id prefix, no encrypted
        # payload.  Some IP22 firmwares interleave short beacons with full
        # telemetry advertisements as a power-saving feature even while the
        # charger is running, so do not treat a short frame as authoritative
        # off-state if a full telemetry frame arrived recently — let the
        # most recent decoded telemetry stand.  Only honour the short frame
        # as "off" once the IP22 has gone quiet for ``_OFF_FRAME_GRACE_S``.
        if len(manufacturer_data) < 10:
            now = time.monotonic()
            last_full = getattr(self, "_last_full_telemetry_at", 0.0)
            if now - last_full >= self._OFF_FRAME_GRACE_S:
                self._publish_off_state()
            return

        try:
            parsed = self._decode_advertisement(key, manufacturer_data)
        except AdvertisementKeyMismatchError:
            logger.warning(
                "%s: advertisement decrypt failed (key mismatch) — "
                "re-reading VREG 0xEC65",
                self._plog,
            )
            self._stored_key_invalid = True
            self._adv_key_hex = None
            self._maybe_provision_key()
            return
        except Exception:
            logger.exception("%s: IP22 advertisement decode error",
                             self._plog)
            return

        if parsed is None:
            return

        self._last_full_telemetry_at = time.monotonic()
        self._publish(parsed)
        self._maybe_daily_refresh()

    @staticmethod
    def _decode_advertisement(key_hex: str, manufacturer_data: bytes):
        device_cls = detect_device_type(manufacturer_data)
        if device_cls is None:
            return None
        parser = device_cls(key_hex)
        parsed = parser.parse(manufacturer_data)

        charge_state = parsed.get_charge_state()
        charger_error = parsed.get_charger_error()

        model_name = parsed.get_model_name()
        if model_name and model_name.startswith("<Unknown"):
            pid = struct.unpack("<H", manufacturer_data[2:4])[0]
            model_name = _IP22_PRODUCT_NAMES.get(pid, model_name)

        return {
            "device_state": (int(charge_state.value)
                             if charge_state is not None else 0),
            "charger_error": (int(charger_error.value)
                              if charger_error is not None else 0),
            "output_voltage1": parsed.get_output_voltage1(),
            "output_voltage2": parsed.get_output_voltage2(),
            "output_voltage3": parsed.get_output_voltage3(),
            "output_current1": parsed.get_output_current1(),
            "output_current2": parsed.get_output_current2(),
            "output_current3": parsed.get_output_current3(),
            "temperature": parsed.get_temperature(),
            "ac_current": parsed.get_ac_current(),
            "model_name": model_name,
        }

    # ------------------------------------------------------------------
    # Key provisioning lifecycle (mirrors orion_tr_key_cli pipeline)
    # ------------------------------------------------------------------

    _PROVISION_BACKOFF_SECS = 180.0

    def _maybe_provision_key(self) -> None:
        global _provision_busy
        if _provision_busy:
            return
        now = time.monotonic()
        since_last = now - self._last_provision_attempt
        if (self._last_provision_attempt > 0
                and since_last < self._PROVISION_BACKOFF_SECS):
            return

        self._last_provision_attempt = now
        mac_colon = _format_mac_colons(self.info["dev_mac"])
        logger.info(
            "%s: no advertisement key cached — spawning subprocess to "
            "read VREG 0xEC65",
            self._plog,
        )

        pause_scanning("ip22 key provisioning")
        _provision_busy = True

        pref_adapter = get_preferred_adapter(self._dbus_settings,
                                             self.info["dev_mac"])

        def worker():
            global _provision_busy
            try:
                with _provision_lock:
                    payload = _run_key_cli(mac_colon,
                                           self._pairing_passkey,
                                           preferred_adapter=pref_adapter)
                if not payload:
                    logger.warning(
                        "%s: key provisioning did not produce a 16-byte "
                        "key; will retry after backoff", self._plog)
                    return
                self._persist_provisioning_result(payload)
            finally:
                _provision_busy = False
                resume_scanning("ip22 key provisioning")

        threading.Thread(
            target=worker, name=f"ip22-keyprov-{mac_colon}",
            daemon=True).start()

    def _persist_provisioning_result(self, payload: Dict[str, Any]) -> None:
        key_hex = payload.get("key")
        if key_hex:
            try:
                set_advertisement_key(self._dbus_settings,
                                      self.info["dev_mac"], key_hex)
                self._adv_key_hex = key_hex
                self._stored_key_invalid = False
                logger.info(
                    "%s: advertisement key stored at %s",
                    self._plog,
                    advertisement_key_setting_path(
                        self.info["dev_mac"]))
            except Exception:
                logger.exception(
                    "%s: failed to persist advertisement key", self._plog)

        firmware_raw = payload.get("firmware")
        if firmware_raw:
            try:
                set_firmware_version(self._dbus_settings,
                                     self.info["dev_mac"], firmware_raw)
                pretty = _format_firmware_version(firmware_raw) or firmware_raw
                self.info["firmware_version"] = pretty
                for role_service in self._role_services.values():
                    try:
                        self._publish_value(role_service,
                                            "/FirmwareVersion", pretty)
                    except Exception:
                        pass
            except Exception:
                logger.exception(
                    "%s: failed to persist firmware version", self._plog)

        hw_version = payload.get("hardware_version")
        if hw_version:
            try:
                self.info["hardware_version"] = hw_version
                for role_service in self._role_services.values():
                    try:
                        self._publish_value(role_service,
                                            "/HardwareVersion", hw_version)
                    except Exception:
                        pass
            except Exception:
                logger.exception(
                    "%s: failed to set hardware version", self._plog)

        adapter = payload.get("adapter")
        if adapter:
            try:
                set_preferred_adapter(self._dbus_settings,
                                      self.info["dev_mac"], adapter)
            except Exception:
                logger.exception(
                    "%s: failed to store preferred adapter", self._plog)

    # ------------------------------------------------------------------
    # Daily early-morning refresh
    # ------------------------------------------------------------------

    _DAILY_REFRESH_HOUR_MIN = 3
    _DAILY_REFRESH_HOUR_MAX = 5

    def _maybe_daily_refresh(self) -> None:
        global _provision_busy
        if not self._adv_key_hex:
            return
        if _provision_busy:
            return
        now = datetime.datetime.now()
        if not (self._DAILY_REFRESH_HOUR_MIN <= now.hour
                <= self._DAILY_REFRESH_HOUR_MAX):
            return
        today = now.strftime("%Y-%m-%d")
        if self._last_daily_refresh_date == today:
            return

        self._last_daily_refresh_date = today
        mac_colon = _format_mac_colons(self.info["dev_mac"])
        logger.info(
            "%s: daily morning refresh — reading firmware via GATT",
            self._plog)

        pref_adapter = get_preferred_adapter(self._dbus_settings,
                                             self.info["dev_mac"])
        pause_scanning("ip22 daily refresh")
        _provision_busy = True

        def worker():
            global _provision_busy
            try:
                with _provision_lock:
                    payload = _run_key_cli(mac_colon,
                                           self._pairing_passkey,
                                           preferred_adapter=pref_adapter)
                if not payload:
                    return
                self._persist_provisioning_result(payload)
            finally:
                _provision_busy = False
                resume_scanning("ip22 daily refresh")

        threading.Thread(
            target=worker, name=f"ip22-daily-{mac_colon}",
            daemon=True).start()

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def _publish_off_state(self) -> None:
        """Publish a minimal snapshot when the device is advertising the
        short power-off frame (no encrypted payload).

        We deliberately publish ``None`` for ``/Dc/0/{Voltage,Current,
        Power,Temperature}`` instead of ``0.0``: the IP22's short beacon
        carries no measurements, and an off / unconnected unit isn't
        actually measuring 0 V / 0 A — it has nothing to report.  None
        is the honest signal; gui-v2 then renders "---" instead of
        confidently fabricated "0.00V | 0.0A" rows.
        """
        # Tick history with zero current so OperationTime freezes but
        # ChargedAh stops integrating.
        self._tick_history(state=0, current_a=0.0)

        pid = self.info.get("product_id", 0)
        battery_v = _battery_voltage_for_product(None, pid)
        # Lookup-once cache: ``"serial"`` key absence means we have not
        # looked yet; presence (even empty string) means we have, and a
        # subsequent advertisement must not re-query BlueZ.  The earlier
        # ``if not self.info.get("serial")`` form re-fired forever
        # whenever ``_bluez_device_name`` returned no Victron-format
        # serial token (which happens whenever the user has renamed the
        # BlueZ device — common on this gateway).  Each retry was a
        # ``GetManagedObjects`` round-trip per advertisement; on a
        # multi-charger setup that pegged ~5% of single-core CPU.
        if "serial" not in self.info:
            self.info["serial"] = _serial_from_advertised_name(
                _bluez_device_name(self.info["dev_mac"])) or ""

        self._last_advertised_state = 0
        for role_service in list(self._role_services.values()):
            with role_service:
                # Off stays off regardless of DVCC engagement — there's no
                # power flowing for "external control" to claim.
                self._publish_value(role_service, "/State", 0)
                # Actively clear measurement paths.  SensorPublisher writes
                # None through (clearing) the first time per path; repeated
                # off-state ads then skip.  Without this, a stale value
                # from the last decoded telemetry tick would persist and
                # read like live data.
                self._publish_value(role_service, "/Dc/0/Voltage", None,
                                    sensor_type="charger_voltage")
                self._publish_value(role_service, "/Dc/0/Current", None,
                                    sensor_type="charger_current")
                self._publish_value(role_service, "/Dc/0/Power", None,
                                    sensor_type="power")
                self._publish_value(role_service, "/Dc/0/Temperature", None,
                                    sensor_type="temperature")
                self._publish_value(role_service, "/Ac/In/L1/I", None,
                                    sensor_type="current")
                if battery_v is not None:
                    self._publish_value(role_service,
                                        "/Settings/BatteryVoltage",
                                        battery_v, sensor_type="voltage")
                if self.info.get("serial"):
                    self._publish_value(role_service, "/Serial",
                                        self.info["serial"])
                # Off-state by definition can't have charger-side alarms;
                # clear all of them so a stale alarm from before the unit
                # was switched off doesn't linger.
                self._publish_alarms(role_service, 0)
                self._publish_history(role_service)
            role_service.connect()

    def _publish(self, parsed) -> None:
        for role_service in list(self._role_services.values()):
            ble_svc = DbusBleService.get()
            if not ble_svc.is_device_role_enabled(
                    self.info, role_service.ble_role.NAME):
                continue

            with role_service:
                st = int(parsed["device_state"])
                v1 = parsed.get("output_voltage1")
                i1 = parsed.get("output_current1")
                # IP22 is always a charger — use charger_voltage /
                # charger_current types so the GUI/DVCC see sub-10 mV /
                # sub-10 mA precision needed for absorption/float
                # convergence tracking and tail-current detection.
                # Always assign — passing None through SensorPublisher
                # clears any stale value from a previous tick, so
                # gui-v2 stops rendering yesterday's voltage as if it
                # were live.
                self._publish_value(role_service, "/Dc/0/Voltage", v1,
                                    sensor_type="charger_voltage")
                self._publish_value(role_service, "/Dc/0/Current", i1,
                                    sensor_type="charger_current")
                self._publish_value(
                    role_service, "/Dc/0/Power",
                    (round(v1 * i1, 2) if v1 is not None and i1 is not None
                     else None),
                    sensor_type="power")

                for idx, out in enumerate(("2", "3")):
                    vk = f"output_voltage{out}"
                    ik = f"output_current{out}"
                    self._publish_value(role_service,
                                        f"/Dc/{idx + 1}/Voltage",
                                        parsed.get(vk),
                                        sensor_type="charger_voltage")
                    self._publish_value(role_service,
                                        f"/Dc/{idx + 1}/Current",
                                        parsed.get(ik),
                                        sensor_type="charger_current")

                self._publish_value(role_service, "/Dc/0/Temperature",
                                    parsed.get("temperature"),
                                    sensor_type="temperature")
                self._publish_value(role_service, "/Ac/In/L1/I",
                                    parsed.get("ac_current"),
                                    sensor_type="current")

                model = parsed.get("model_name")
                if model and not model.startswith("<Unknown"):
                    self._publish_value(role_service, "/ProductName", model)
                self._publish_value(role_service, "/ProductId",
                                    self.info["product_id"])
                err = int(parsed["charger_error"])
                # When a BMS / GX is dictating setpoints (DVCC engaged), the
                # gui-v2 + dbus-systemcalc-py contract is to publish /State
                # = 252 (EXTERNAL_CONTROL) so the rest of the system shows
                # the charger as "externally controlled" instead of as a
                # stand-alone unit happening to be in bulk/abs/float.  The
                # IP22 firmware itself doesn't know it's externally
                # controlled — it just sees us bumping 0xEDF0 / 0xEDF7 — so
                # the override has to happen here, on the publish side.  We
                # still pass the *real* advertised state to _tick_history()
                # below so OperationTime / ChargedAh keep accumulating
                # correctly while externally controlled.
                self._last_advertised_state = st
                self._publish_value(role_service, "/State",
                                    self._derive_published_state(st))
                self._publish_value(role_service, "/ErrorCode", err)
                self._publish_alarms(role_service, err)

                # /Serial — populated lazily from the BlueZ-advertised name
                # on first telemetry tick (the encrypted payload itself
                # doesn't carry the serial).  Use the ``"serial" in
                # self.info`` sentinel so that a *negative* lookup (no
                # Victron-format token in the BlueZ name) is also cached;
                # otherwise this re-queried BlueZ for every advertisement.
                if "serial" not in self.info:
                    self.info["serial"] = _serial_from_advertised_name(
                        _bluez_device_name(self.info["dev_mac"])) or ""
                if self.info["serial"]:
                    self._publish_value(role_service, "/Serial",
                                        self.info["serial"])

                # /Settings/BatteryVoltage — fixed per product id.  GUIs use
                # this to label the battery bus and pick reasonable display
                # ranges.  Only published when we can resolve a value;
                # mis-publishing here would cause downstream confusion.
                battery_v = _battery_voltage_for_product(
                    model, self.info["product_id"])
                if battery_v is not None:
                    self._publish_value(role_service,
                                        "/Settings/BatteryVoltage",
                                        battery_v, sensor_type="voltage")

                # NrOfOutputs — any non-None out2/out3 bumps it up
                outputs = 1
                if parsed.get("output_voltage2") is not None:
                    outputs = 2
                if parsed.get("output_voltage3") is not None:
                    outputs = 3
                self._publish_value(role_service, "/NrOfOutputs", outputs)

                # /Mode is intentionally not published — see the role file
                # for why (firmware doesn't support remote on/off, and
                # PageAcCharger.qml gates the Switch on dataItem.valid).

                # History accumulators — tick before publishing so the
                # values reflect the current sample window.
                current_a = (i1 if i1 is not None else 0.0)
                self._tick_history(state=st, current_a=current_a)
                self._publish_history(role_service)

            role_service.connect()

    # ------------------------------------------------------------------
    # /Settings/ChargeCurrentLimit write (GATT) — VREG 0xEDF0, u16 LE in 0.1A
    # ------------------------------------------------------------------

    def _ip22_on_charge_current_limit_write(self,
                                            role_service: DbusRoleService,
                                            value_amps) -> bool:
        # User-facing /Settings/ChargeCurrentLimit write.  Same VREG as
        # /Link/ChargeCurrent (0xEDF0), so we go through the queued path
        # for serialisation, dedupe, and busy-recovery, then persist the
        # accepted value to com.victronenergy.settings so it survives
        # the next service restart.
        try:
            new_a = float(value_amps)
        except (TypeError, ValueError):
            return False
        if new_a < 0 or new_a > 1000:
            return False
        tenths = int(round(new_a * 10))
        if tenths < 0 or tenths > 0xFFFF:
            return False
        value_bytes = bytes([tenths & 0xFF, (tenths >> 8) & 0xFF])

        def on_done(success: bool):
            if success:
                self._last_pushed_charge_current_a = new_a
                self._persist_setting("ChargeCurrentLimit", new_a)

        self._enqueue_write(VREG_BATTERY_MAX_CURRENT, value_bytes,
                            on_complete=on_done)
        return True

    # ------------------------------------------------------------------
    # DVCC integration — /Link/* writes pushed by dbus-systemcalc-py
    # ------------------------------------------------------------------
    #
    # dbus-systemcalc-py pushes target setpoints onto a charger service via
    # /Link/ChargeCurrent and /Link/ChargeVoltage and expects them to take
    # effect on the hardware.  For IP22 we map:
    #
    #   /Link/ChargeCurrent  -> VREG 0xEDF0 (Battery max current,    0.1 A)
    #   /Link/ChargeVoltage  -> VREG 0xEDF7 (Absorption voltage,     0.01 V)
    #
    # The other DVCC inputs (/Link/TemperatureSense, /Link/VoltageSense,
    # /Link/BatteryCurrent, /Link/NetworkMode, /Settings/BmsPresent) are
    # accepted on the role but only stored — IP22 firmware 3.65 has no
    # writable VREG that consumes external sense or BMS-presence data, so
    # we surface them on D-Bus for systemcalc to read back without trying
    # to push them onto the wire.

    # ------------------------------------------------------------------
    # /Link/ChargeCurrent — DVCC target current → VREG 0xEDF0
    # ------------------------------------------------------------------

    def _ip22_on_link_charge_current_write(self,
                                            role_service: DbusRoleService,
                                            value_amps) -> bool:
        try:
            new_a = float(value_amps)
        except (TypeError, ValueError):
            return False
        if new_a < 0 or new_a > 1000:
            return False
        # A /Link/ChargeCurrent write means DVCC is in control regardless
        # of whatever NetworkMode/BmsPresent currently say.
        self._set_dvcc_engaged(role_service, True)
        last = self._last_pushed_charge_current_a
        if last is not None and abs(new_a - last) < CHARGE_CURRENT_DEADBAND_A:
            return True
        tenths = int(round(new_a * 10))
        if tenths < 0 or tenths > 0xFFFF:
            return False
        value_bytes = bytes([tenths & 0xFF, (tenths >> 8) & 0xFF])

        def on_done(success: bool):
            if success:
                self._last_pushed_charge_current_a = new_a

        self._enqueue_write(VREG_BATTERY_MAX_CURRENT, value_bytes,
                            on_complete=on_done)
        return True

    # ------------------------------------------------------------------
    # /Link/ChargeVoltage — DVCC target voltage → VREG 0xEDF7
    # ------------------------------------------------------------------

    def _ip22_on_link_charge_voltage_write(self,
                                            role_service: DbusRoleService,
                                            value_volts) -> bool:
        try:
            new_v = float(value_volts)
        except (TypeError, ValueError):
            return False
        if new_v <= 0 or new_v > 80:
            return False
        self._set_dvcc_engaged(role_service, True)
        last = self._last_pushed_charge_voltage_v
        if last is not None and abs(new_v - last) < CHARGE_VOLTAGE_DEADBAND_V:
            return True
        centivolts = int(round(new_v * 100))
        if centivolts < 0 or centivolts > 0xFFFF:
            return False
        value_bytes = bytes([centivolts & 0xFF, (centivolts >> 8) & 0xFF])
        self._ensure_battery_type_user()

        def on_voltage_set(success: bool):
            if success:
                self._last_pushed_charge_voltage_v = new_v

        self._enqueue_write(VREG_ABSORPTION_VOLTAGE, value_bytes,
                            on_complete=on_voltage_set)
        return True

    # /Link/{NetworkMode,TemperatureSense,VoltageSense,BatteryCurrent}
    # and /Settings/BmsPresent passive handlers come from
    # ChargerCommonMixin: _on_link_passive_write,
    # _on_link_network_mode_write, _on_settings_bms_present_write.

    # ------------------------------------------------------------------
    # /Settings/AbsorptionVoltage  ->  VREG 0xEDF7
    # /Settings/FloatVoltage       ->  VREG 0xEDF6
    # ------------------------------------------------------------------
    #
    # Same wire format as /Link/ChargeVoltage: u16 LE in 0.01 V units,
    # gated on 0xEDF1 (battery type) being USER (0xFF).  These let the
    # user configure the charge profile via gui-v2 in addition to the
    # DVCC override path.

    def _ensure_battery_type_user(self) -> None:
        if self._battery_type_is_user is True:
            return

        def _on_user_set(success: bool):
            if success:
                self._battery_type_is_user = True
            else:
                logger.error(
                    "%s: GATT BatteryType=USER write failed",
                    self._plog)

        self._enqueue_write(
            VREG_BATTERY_TYPE, bytes([BATTERY_TYPE_USER]),
            on_complete=_on_user_set,
        )

    def _ip22_on_absorption_voltage_write(self,
                                           role_service: DbusRoleService,
                                           value_volts) -> bool:
        try:
            new_v = float(value_volts)
        except (TypeError, ValueError):
            return False
        if new_v <= 0 or new_v > 80:
            return False
        last = self._last_pushed_charge_voltage_v
        if last is not None and abs(new_v - last) < CHARGE_VOLTAGE_DEADBAND_V:
            self._persist_setting("AbsorptionVoltage", new_v)
            return True
        value_bytes = encode_u16_le_scaled(new_v, 100)
        if value_bytes is None:
            return False
        self._ensure_battery_type_user()

        def on_done(success: bool):
            if success:
                self._last_pushed_charge_voltage_v = new_v
                self._persist_setting("AbsorptionVoltage", new_v)

        self._enqueue_write(VREG_ABSORPTION_VOLTAGE, value_bytes,
                            on_complete=on_done)
        return True

    def _ip22_on_float_voltage_write(self,
                                      role_service: DbusRoleService,
                                      value_volts) -> bool:
        try:
            new_v = float(value_volts)
        except (TypeError, ValueError):
            return False
        if new_v <= 0 or new_v > 80:
            return False
        value_bytes = encode_u16_le_scaled(new_v, 100)
        if value_bytes is None:
            return False
        self._ensure_battery_type_user()

        def on_done(success: bool):
            if success:
                self._persist_setting("FloatVoltage", new_v)

        self._enqueue_write(VREG_FLOAT_VOLTAGE, value_bytes,
                            on_complete=on_done)
        return True

    # /Link/NetworkStatus dynamic update, /State = 252 override on
    # DVCC engagement, settings persistence, history accumulators,
    # and /Alarms/* derivation are all provided by ChargerCommonMixin.
    # IP22-specific extension points: PERSISTED_SETTING_SUFFIXES_TO_PATHS
    # (declared near the top of the class), and _publish_alarms /
    # _publish_history called from _publish() below.
