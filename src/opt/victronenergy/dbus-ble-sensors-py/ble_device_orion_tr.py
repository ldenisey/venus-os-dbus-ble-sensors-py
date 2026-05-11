"""
Victron Orion-TR Smart (BLE manufacturer ``0x02E1``, product IDs
``0xA3C0``–``0xA3DF``).

This device is **not** registered in ``BleDevice.DEVICE_CLASSES`` because
``0x02E1`` is already owned by ``BleDeviceVictronEnergy`` (SolarSense).
Dispatch is handled explicitly in :mod:`dbus_ble_sensors`.

Compared to the other devices in this service, the Orion-TR has two
unusual needs:

1. **Encrypted advertisements.** The 16-byte advertisement key is
   device-specific and is not printed on the unit.  It can only be read
   from the device itself via a paired GATT session.  We store the key
   in a *silent* setting under
   ``/Settings/Services/BleSensors/OrionTr/<mac>/AdvertisementKey`` and
   kick off :class:`orion_tr_key_provision.OrionKeyProvisioner` when
   the setting is missing or stale.

2. **``dcdc`` vs ``alternator`` service.** When the unit runs a charger
   algorithm (bulk/absorption/float/storage) the stock Victron service
   publishes under ``com.victronenergy.alternator.*`` so the gui-v2
   *DC Sources* page picks it up.  When the unit is off or in
   fixed-output mode the service type flips to
   ``com.victronenergy.dcdc.*``.  This driver switches between the two
   roles at runtime to stay in parity with ``dbus-victron-orion-tr``.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import struct
import subprocess
import threading
import time
from typing import Any, Dict, Optional

import dbus

from ble_device import BleDevice
from ble_role import BleRole
from dbus_ble_service import DbusBleService
from dbus_role_service import DbusRoleService
from dbus_settings_service import DbusSettingsService
# The vendored ``victron_ble`` package (see ``ext/victron_ble``) is the
# reference decoder and matches the layout used by the upstream Victron
# Energy tooling.  It has been patched to use ``cryptography`` instead of
# PyCryptodome so it runs on the stock Venus OS image.
from victron_ble.devices import detect_device_type  # type: ignore
from victron_ble.exceptions import (  # type: ignore
    AdvertisementKeyMismatchError,
)

from orion_tr_gatt import AsyncGATTWriter
from ble_charger_common import (
    ChargerCommonMixin,
    CHARGE_CURRENT_DEADBAND_A,
    CHARGE_VOLTAGE_DEADBAND_V,
    battery_voltage_from_model,
    bluez_device_name as _orion_bluez_device_name,
    encode_u16_le_scaled,
    serial_from_advertised_name as _orion_serial_from_advertised_name,
)
from orion_tr_key_settings import (
    advertisement_key_setting_path,
    get_advertisement_key,
    get_firmware_version,
    get_preferred_adapter,
    set_advertisement_key,
    set_firmware_version,
    set_preferred_adapter,
)
from orion_tr_pin import resolve_pairing_passkey
from scan_control import pause_scanning, resume_scanning
from ve_types import VE_UN8

logger = logging.getLogger(__name__)

VICTRON_MANUFACTURER_ID = 0x02E1
ORION_PRODUCT_ID_MIN = 0xA3C0
ORION_PRODUCT_ID_MAX = 0xA3DF

# VREGs verified by direct GATT probe (firmware 1.10 on bench unit
# FF:13:42:2B:7A:4B, "Orion Smart 12V/24V-15A DC-DC Converter").
#
# Confirmed implemented:
#   0x0100  product id (u32)
#   0x0102  app version (u32)            firmware "1.10" -> 0x011700FF
#   0x010C  long name + serial           tstr
#   0x0200  DEVICE_MODE                  u8: 1 = ON, 4 = OFF (writable)
#   0x0207  DEVICE_OFF_REASON            u32 flags (read-only on this fw)
#   0xEDDB                                u16 LE; 26.90 V on bench
#                                         (likely default absorption v
#                                         for 24 V profile)
#   0xEDE2                                u16 LE; 26.80 V on bench
#                                         (likely default float v
#                                         for 24 V profile)
#   0xEDE9                                u16 LE; tracked the live
#                                         /Dc/0/Voltage = 19.50 V — looks
#                                         like the *active* output
#                                         setpoint, not a stored config
#   0xEDF1                                u8 = 0x00 (not USER) — battery
#                                         type, same role as IP22 0xEDF1
#   0xEDFE                                u8 = 0x00 (Adaptive mode)
#   0xEDFB, 0xEDFC                        u16 LE; values 200, 1000
#                                         (units / meaning unconfirmed)
#
# **Not yet mapped** to Orion-TR equivalents of the IP22 charge profile:
#   - max-current setpoint (IP22 = 0xEDF0)
#   - absorption-voltage setpoint (IP22 = 0xEDF7)
#   - float-voltage setpoint (IP22 = 0xEDF6)
#   - Charger / PSU function-select VREG
#
# Probe sweeps of 0xEDF0-0xEDFF returned no responses on this firmware,
# suggesting the Orion-TR uses a different layout than the IP22 (likely
# 0xEDDx / 0xEDEx given which VREGs *did* respond).  The constants
# below are placeholders pending a more thorough live probe.
VREG_DEVICE_MODE = 0x0200
# Charge-profile VREGs — mapped via direct GATT write/read probes on the
# bench unit (FF:13:42:2B:7A:4B, firmware 1.10).  Layout matches the
# IP22 charger here, including the USER battery-type gate that voltage
# writes are conditioned on.
VREG_BATTERY_TYPE       = 0xEDF1   # u8;  0xFF = USER (unlocks voltage writes)
VREG_FLOAT_VOLTAGE      = 0xEDF6   # u16 LE, 0.01 V (write-probe: 27.10 V took)
VREG_ABSORPTION_VOLTAGE = 0xEDF7   # u16 LE, 0.01 V (write-probe: 28.50 V took)
BATTERY_TYPE_USER = 0xFF
# Max-current VREG: probed exhaustively (0xEDF0, 0xEDF8-0xEDFF, 0xEDD0-
# 0xEDEE) — Orion-TR firmware 1.10 returns ack code 1 (unknown register)
# for 0xEDF0 (the IP22's charge-current VREG) and exposes no equivalent
# at any tested address.  The hardware max is fixed by the model
# variant (15 A on the 12V/24V-15A bench unit); the "Current limit"
# field exposed by the vendor app maps somewhere we haven't located.
# Until found,
# /Settings/ChargeCurrentLimit and /Link/ChargeCurrent are persisted to
# settings and surfaced on D-Bus, but no GATT write is emitted — see
# _orion_persist_only_write below.  The DVCC stop-charging mechanism
# (drop /Link/ChargeVoltage to <= battery resting V) goes through
# 0xEDF7, which IS wired, so BMS control still works end-to-end.
VREG_BATTERY_MAX_CURRENT: Optional[int] = None  # TODO: locate on this firmware

# gui-v2 renders alternator units under "DC Sources" based on /ProductId:
# a value >= 0xA3E0 is treated as "real alternator" and uses
# PageAlternatorModel.  0xA3F0 matches what the standalone service uses
# when flipping to alternator mode.
ALTERNATOR_PRODUCT_ID = 0xA3F0

# Fallback product names for Orion-TR product IDs not (yet) in the
# vendored victron_ble MODEL_ID_MAPPING.  Sourced from the standalone
# dbus-victron-orion-tr driver.
_ORION_PRODUCT_NAMES = {
    0xA3C0: "Orion-TR Smart 12/12-18A",
    0xA3C1: "Orion-TR Smart 12/24-10A",
    0xA3C2: "Orion-TR Smart 12/48-6A",
    0xA3D0: "Orion-TR Smart 24/12-20A",
    0xA3D1: "Orion-TR Smart 24/24-12A",
    0xA3D2: "Orion-TR Smart 24/48-6A",
    0xA3D5: "Orion-TR Smart 48/24-12A",
    0xA3D6: "Orion-TR Smart 48/48-6A",
}

# VE.Direct OperationMode values that indicate charger-style operation.
_CHARGER_MODES = {3, 4, 5, 6}  # BULK, ABSORPTION, FLOAT, STORAGE

_gatt_writer: Optional[AsyncGATTWriter] = None

# Single in-flight guard for the subprocess provisioner.  ``threading.Lock``
# is enough — the provisioner thread simply blocks on ``_provision_lock``
# while the subprocess runs, and ``_provision_busy`` tells the device
# callback whether it should skip another attempt.
_provision_lock = threading.Lock()
_provision_busy = False

# Absolute path to the CLI provisioner so we don't depend on PATH.
_KEY_CLI_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "orion_tr_key_cli.py")

def is_orion_tr_manufacturer_data(manufacturer_data: bytes) -> bool:
    if len(manufacturer_data) < 4:
        return False
    pid = struct.unpack("<H", manufacturer_data[2:4])[0]
    return ORION_PRODUCT_ID_MIN <= pid <= ORION_PRODUCT_ID_MAX

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
                 timeout_s: float = 45.0,
                 preferred_adapter: Optional[str] = None,
                 ) -> Optional[Dict[str, Any]]:
    """Invoke :mod:`orion_tr_key_cli` and return its parsed JSON payload.

    Running in a separate process keeps the provisioning flow isolated
    from this service's long-lived D-Bus and BlueZ state, which was
    observed to produce corrupt CCCD writes on the second and later
    provisioning attempt within the same service lifetime.  The CLI
    mirrors the known-good reference test harness verbatim.

    Returns a dict with at least ``key`` (32-hex string) and optionally
    ``firmware`` (raw hex bytes of VREG ``0x0140``), or ``None`` if the
    subprocess failed.
    """
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
            timeout=timeout_s + 15.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("orion key-provisioner subprocess timed out for %s",
                       mac)
        return None
    except Exception:
        logger.exception("failed to spawn orion key-provisioner subprocess")
        return None

    if result.returncode != 0:
        logger.warning("orion key-provisioner subprocess exited %d: %s",
                       result.returncode, (result.stderr or "").strip())
        return None

    raw = (result.stdout or "").strip()
    try:
        payload = json.loads(raw)
    except Exception:
        logger.warning("orion key-provisioner produced non-JSON output: %r",
                       raw)
        return None

    key = str(payload.get("key", "")).strip().lower()
    if len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
        logger.warning("orion key-provisioner returned invalid key: %r",
                       key)
        return None
    payload["key"] = key
    return payload

def _format_firmware_version(raw_hex: Optional[str]) -> Optional[str]:
    """Decode VREG ``0x0140`` bytes into a ``"major.minor"`` string.

    Victron product-info firmware registers use hex-BCD encoding: each
    nibble is a decimal digit, and the low 16 bits are the
    ``MAJOR.MINOR`` version number.  On a 4-byte value the high byte
    carries a release-type marker (``0x40`` = Release, ``0x50`` = Beta)
    which we expose via a trailing ``~beta``/``~dev`` tag when present.

    * 2 bytes LE ``48 01`` → value ``0x0148`` → ``"1.48"``
    * 4 bytes LE ``10 01 00 40`` → low 16 bits ``0x0110`` → ``"1.10"``
    * 4 bytes LE ``10 01 00 50`` → ``"1.10~beta"``

    Falls back to the raw hex when the encoding doesn't match so at
    least something shows in the UI.
    """
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
        suffix = {
            0x40: "",      # Release
            0x50: "~beta",
            0xF0: "~dev",
        }.get(kind, "")
        return base + suffix
    # Fallback: raw hex so the user can see *something*.
    return raw_hex

def _parse_temperature(raw_hex: Optional[str]) -> Optional[float]:
    """Decode VREG ``0xEDDB`` (charger temperature) into degrees Celsius.

    The register returns a signed 16-bit LE value in units of 0.01 °C.
    (Observed values: ``0x0a14`` = 2580 → 25.8 °C — consistent with room
    temperature.  The Orion-TR uses direct Celsius, not Kelvin.)
    Returns ``None`` when the value is the invalid sentinel ``0x7FFF``.
    """
    if not raw_hex:
        return None
    try:
        blob = bytes.fromhex(raw_hex)
    except ValueError:
        return None
    if len(blob) < 2:
        return None
    raw = int.from_bytes(blob[:2], "little", signed=True)
    if raw == 0x7FFF or raw == -1:
        return None
    return round(raw / 100.0, 1)

def _format_mac_colons(dev_mac: str) -> str:
    s = dev_mac.lower().replace(":", "")
    return ":".join(s[i : i + 2] for i in range(0, 12, 2)).upper()

def _bluez_device_name(dev_mac: str) -> Optional[str]:
    """Return the BlueZ-reported advertised name for ``dev_mac``, if known.

    Searches all adapters for the device (not just hci0) and reads
    ``org.bluez.Device1.Name``.  Falls back to ``Device1.Alias`` if the
    name is missing, and to ``None`` if BlueZ does not yet have a record.
    """
    mac_suffix = "/dev_" + _format_mac_colons(dev_mac).replace(":", "_")
    try:
        bus = _shared_bus()
        om = dbus.Interface(
            bus.get_object("org.bluez", "/", introspect=False),
            "org.freedesktop.DBus.ObjectManager")
        objects = om.GetManagedObjects()
        for path in objects:
            if not str(path).endswith(mac_suffix):
                continue
            if "org.bluez.Device1" not in objects[path]:
                continue
            obj = bus.get_object("org.bluez", path, introspect=False)
            props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
            for prop in ("Name", "Alias"):
                try:
                    val = str(props.Get("org.bluez.Device1", prop))
                except dbus.DBusException:
                    continue
                if val:
                    return val
    except Exception:
        return None
    return None

class BleDeviceOrionTR(ChargerCommonMixin, BleDevice):
    """Orion-TR Smart DC-DC / buck-boost on encrypted Victron manufacturer data."""

    # Used in /Settings/Devices/<ns>_<mac>/* paths (see ble_charger_common).
    SETTINGS_NS_PREFIX = "orion_tr"

    # /Settings/Devices/orion_tr_<mac>/<suffix> -> role-service path for
    # values restored at service start.  The Orion-TR alternator role
    # publishes the same three writable settings as the IP22 charger.
    PERSISTED_SETTING_SUFFIXES_TO_PATHS = {
        "ChargeCurrentLimit": "/Settings/ChargeCurrentLimit",
        "AbsorptionVoltage":  "/Settings/AbsorptionVoltage",
        "FloatVoltage":       "/Settings/FloatVoltage",
    }

    @staticmethod
    def matches_manufacturer_data(manufacturer_data: bytes) -> bool:
        return is_orion_tr_manufacturer_data(manufacturer_data)

    def __init__(self, dev_mac: str):
        self._adv_key_hex: Optional[str] = None
        self._dbus_settings = DbusSettingsService()
        self._pairing_passkey: int = resolve_pairing_passkey(self._dbus_settings)
        self._mode_busy = False
        # Monotonic timestamp of last provisioning kick-off, for backoff.
        self._last_provision_attempt: float = 0.0
        # When we see a KeyMismatch we stop trusting the stored key until a
        # fresh provisioning round writes a new one.  Without this guard we
        # would re-read the stale key from settings on every advertisement.
        self._stored_key_invalid = False
        # YYYY-MM-DD of the last successful daily refresh; drives the
        # "once per morning when in range" GATT read.
        self._last_daily_refresh_date: Optional[str] = None
        self._current_role_name: Optional[str] = None
        # Cached battery-type byte; if it isn't already USER (0xFF)
        # absorption / float voltage writes will be rejected on
        # firmwares that gate those writes the way the IP22 does.
        self._battery_type_is_user: Optional[bool] = None
        # GATT queue / history / DVCC engagement / persistence state.
        self._init_charger_common()
        super().__init__(dev_mac)

    # ------------------------------------------------------------------
    # Device configuration
    # ------------------------------------------------------------------

    def configure(self, manufacturer_data: bytes):
        pid = struct.unpack("<H", manufacturer_data[2:4])[0]
        self._adv_key_hex = get_advertisement_key(self._dbus_settings,
                                                  self.info["dev_mac"])
        # Base ``_load_configuration`` re-reads ``self.MANUFACTURER_ID`` and
        # overwrites ``info["manufacturer_id"]`` from it.  The class-level
        # ``MANUFACTURER_ID`` is intentionally ``None`` (so the autoloader
        # leaves 0x02E1 to ``BleDeviceVictronEnergy`` / SolarSense); shadow
        # it via an instance attribute so the base validation still passes.
        self.MANUFACTURER_ID = VICTRON_MANUFACTURER_ID
        # Prefer the name the device actually advertises (e.g.
        # ``"Orion Smart HQ20326VVVJ"``) over a hard-coded label so the
        # gui-v2 Settings → Devices row matches what the user sees on
        # their unit.  Falls back to ``Orion-TR Smart`` when BlueZ has
        # not surfaced a name yet (e.g. first pass before bonding).
        adv_name = _bluez_device_name(self.info["dev_mac"])
        product_name = adv_name or "Orion-TR Smart"
        device_name_base = adv_name or "Orion-TR"
        # Firmware version: cached from a prior GATT read, falls back to
        # the placeholder the base class expects if we haven't paired yet.
        firmware_raw = get_firmware_version(self._dbus_settings,
                                            self.info["dev_mac"])
        firmware_version = _format_firmware_version(firmware_raw) or "1.0.0"
        self.info.update(
            {
                "manufacturer_id": VICTRON_MANUFACTURER_ID,
                "product_id": pid,
                "product_name": product_name,
                "device_name": device_name_base,
                "dev_prefix": "orion_tr",
                "firmware_version": firmware_version,
                # Start in dcdc; flip happens after the first successful decode.
                "roles": {"dcdc": {}},
                "regs": [
                    {
                        "name": "_orion_placeholder",
                        "type": VE_UN8,
                        "offset": 0,
                        "roles": [None],
                    }
                ],
                "settings": [],
                "alarms": [],
            }
        )
        self._current_role_name = "dcdc"

    def init(self):
        super().init()
        # Seed CustomName from the BLE-advertised name so the device
        # list shows the user's own label (e.g. "24v Front Bay") instead
        # of the long model spec.  Only set if the user hasn't already
        # chosen a custom name via the UI.
        adv_name = _bluez_device_name(self.info["dev_mac"])
        if adv_name:
            for role_service in self._role_services.values():
                current = role_service["/CustomName"]
                if not current:
                    self._publish_value(role_service, "/CustomName", adv_name)

    def check_manufacturer_data(self, manufacturer_data: bytes) -> bool:
        return self.matches_manufacturer_data(manufacturer_data)

    # ------------------------------------------------------------------
    # Main advertisement handler
    # ------------------------------------------------------------------

    def handle_manufacturer_data(self, manufacturer_data: bytes):
        if not DbusBleService.get().is_device_enabled(self.info):
            return

        if self._stored_key_invalid:
            # Waiting on a re-provision; avoid decoding with a known-bad key.
            self._maybe_provision_key()
            return

        key = self._adv_key_hex or get_advertisement_key(
            self._dbus_settings, self.info["dev_mac"])
        if key:
            self._adv_key_hex = key

        if not key:
            # First time we see this device — kick off provisioning once;
            # subsequent advertisements will decode normally as soon as the
            # key has been written to settings.
            self._maybe_provision_key()
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
            logger.exception("%s: Orion advertisement decode error",
                             self._plog)
            return

        if parsed is None:
            return

        self._ensure_role_for_state(int(parsed["device_state"]))
        self._publish(parsed)

        # Receiving an advertisement means the unit is in range right
        # now — good moment to piggyback an opportunistic firmware refresh
        # if we're in the morning window and haven't done it today.
        self._maybe_daily_refresh()

    @staticmethod
    def _decode_advertisement(key_hex: str, manufacturer_data: bytes):
        """Decrypt + parse a Victron DC-DC advertisement via ``victron_ble``.

        Returns a small dict with the same shape the rest of the driver
        expects so the callers can stay agnostic of the ``victron_ble``
        ``DeviceData`` objects.
        """
        device_cls = detect_device_type(manufacturer_data)
        if device_cls is None:
            return None
        parser = device_cls(key_hex)
        parsed = parser.parse(manufacturer_data)

        charge_state = parsed.get_charge_state()
        charger_error = parsed.get_charger_error()
        off_reason = parsed.get_off_reason()

        # victron_ble's model-id table may not cover all Orion-TR product
        # IDs (e.g. 0xA3D5 48V models).  Fall back to our own table.
        model_name = parsed.get_model_name()
        if model_name and model_name.startswith("<Unknown"):
            pid = struct.unpack("<H", manufacturer_data[2:4])[0]
            model_name = _ORION_PRODUCT_NAMES.get(pid, model_name)

        return {
            "device_state": int(charge_state.value) if charge_state is not None else 0,
            "charger_error": int(charger_error.value) if charger_error is not None else 0,
            "input_voltage": parsed.get_input_voltage(),
            "output_voltage": parsed.get_output_voltage(),
            "off_reason": int(off_reason.value) if off_reason is not None else 0,
            "model_name": model_name,
        }

    # ------------------------------------------------------------------
    # Key provisioning lifecycle
    # ------------------------------------------------------------------

    # Minimum time between provisioning attempts when the previous one
    # did not deliver a key.  Pairing + GATT + timeout can run ~45 s so a
    # tight loop would lock out the adapter.
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

        # Yield hci0 to the provisioner subprocess so BleakScanner and
        # the mode-write path don't step on its GATT burst.  Released in
        # the worker thread regardless of outcome.
        pause_scanning("orion-tr key provisioning")
        _provision_busy = True

        # Check if we have a preferred adapter from a prior successful connect
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
                        "key; will retry after backoff",
                        self._plog)
                    return
                self._persist_provisioning_result(payload)
            finally:
                _provision_busy = False
                resume_scanning("orion-tr key provisioning")

        threading.Thread(
            target=worker, name=f"orion-tr-keyprov-{mac_colon}",
            daemon=True).start()

    def _persist_provisioning_result(self, payload: Dict[str, Any]) -> None:
        """Write key + firmware from a CLI payload into settings + info."""
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
                logger.info("%s: firmware version %s recorded",
                            self._plog, pretty)
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
                logger.info("%s: hardware version %s recorded",
                            self._plog, hw_version)
            except Exception:
                logger.exception(
                    "%s: failed to set hardware version", self._plog)

        temperature_raw = payload.get("temperature")
        if temperature_raw:
            try:
                temp_c = _parse_temperature(temperature_raw)
                if temp_c is not None:
                    for role_service in self._role_services.values():
                        try:
                            self._publish_value(
                                role_service, "/Dc/0/Temperature",
                                temp_c, sensor_type="temperature")
                        except Exception:
                            pass
                    logger.info("%s: temperature %.1f °C recorded",
                                self._plog, temp_c)
            except Exception:
                logger.exception(
                    "%s: failed to parse temperature", self._plog)

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

    # Hour window (local time, 24h) during which we allow an opportunistic
    # pair-and-refresh.  The device must send an advertisement first — we
    # only act if it's in range, and at most once per calendar day.
    _DAILY_REFRESH_HOUR_MIN = 3
    _DAILY_REFRESH_HOUR_MAX = 5

    def _maybe_daily_refresh(self) -> None:
        global _provision_busy
        # Only if we already have a valid stored key — we never kick off
        # a daily refresh on a device we haven't successfully provisioned
        # yet; that path is handled by the key-missing logic above.
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

        # Mark attempted *before* kicking off so a repeating advert burst
        # during the window doesn't queue multiple refreshes.
        self._last_daily_refresh_date = today
        mac_colon = _format_mac_colons(self.info["dev_mac"])
        logger.info(
            "%s: daily morning refresh — reading firmware via GATT",
            self._plog)

        pref_adapter = get_preferred_adapter(self._dbus_settings,
                                             self.info["dev_mac"])
        pause_scanning("orion-tr daily refresh")
        _provision_busy = True

        def worker():
            global _provision_busy
            try:
                with _provision_lock:
                    payload = _run_key_cli(mac_colon,
                                           self._pairing_passkey,
                                           preferred_adapter=pref_adapter)
                if not payload:
                    logger.warning(
                        "%s: daily refresh did not produce a payload",
                        self._plog)
                    # Don't clobber _last_daily_refresh_date — we already
                    # set it; the next retry will be tomorrow.  If that's
                    # too strict we can wire a backoff here later.
                    return
                self._persist_provisioning_result(payload)
            finally:
                _provision_busy = False
                resume_scanning("orion-tr daily refresh")

        threading.Thread(
            target=worker, name=f"orion-tr-daily-{mac_colon}",
            daemon=True).start()

    # ------------------------------------------------------------------
    # Publishing decoded values
    # ------------------------------------------------------------------

    def _publish(self, parsed) -> None:
        # Lazily populate the BlueZ-derived serial once per process —
        # the encrypted advertisement payload doesn't carry one.  Use
        # the ``"serial" in self.info`` sentinel so a *negative* lookup
        # (no Victron-format token in the BlueZ name — typical when the
        # user has renamed the device in the BlueZ surface) is also
        # cached.  The earlier ``if not self.info.get("serial")`` form
        # re-fired forever in that case, paying a ``GetManagedObjects``
        # round-trip per advertisement.
        if "serial" not in self.info:
            self.info["serial"] = _orion_serial_from_advertised_name(
                _orion_bluez_device_name(self.info["dev_mac"])) or ""

        for role_service in list(self._role_services.values()):
            ble_svc = DbusBleService.get()
            if not ble_svc.is_device_role_enabled(
                    self.info, role_service.ble_role.NAME):
                continue

            # Wrap the whole per-role publish in a single vedbus
            # context: if multiple paths actually changed this ad, they
            # coalesce into one ItemsChanged emit.  Each
            # ``_publish_value`` underneath goes through SensorPublisher
            # for change-detection + heartbeat.
            with role_service:
                st = int(parsed["device_state"])
                # Voltage precision policy: when the unit acts as a charger
                # (alternator role) the user/DVCC need sub-10 mV precision
                # to track absorption/float regulation; the generic voltage
                # type at 0.01 V hides convergence behavior.  In dcdc/PSU
                # role those decisions don't apply, so the cheaper default
                # is fine.
                is_alternator = role_service.ble_role.NAME == "alternator"
                v_sensor_type = ("charger_voltage" if is_alternator
                                 else "voltage")
                if parsed.get("input_voltage") is not None:
                    self._publish_value(role_service, "/Dc/In/V",
                                        parsed["input_voltage"],
                                        sensor_type=v_sensor_type)
                if parsed.get("output_voltage") is not None:
                    self._publish_value(role_service, "/Dc/0/Voltage",
                                        parsed["output_voltage"],
                                        sensor_type=v_sensor_type)

                # ProductName = model spec from victron_ble's product-id table.
                model = parsed.get("model_name")
                if model and not model.startswith("<Unknown"):
                    self._publish_value(role_service, "/ProductName", model)

                err = int(parsed["charger_error"])
                self._last_advertised_state = st
                # Only the alternator role represents the device as a
                # charger — DVCC is not a meaningful contract for the
                # dcdc role (PSU mode), so suppress the /State=252
                # override and the charger /Alarms/* there.
                if is_alternator:
                    self._publish_value(role_service, "/State",
                                        self._derive_published_state(st))
                    self._publish_alarms(role_service, err)
                else:
                    self._publish_value(role_service, "/State", st)
                self._publish_value(role_service, "/ErrorCode", err)
                self._publish_value(role_service, "/DeviceOffReason",
                                    int(parsed["off_reason"]))

                # Keep /Mode in sync with the inferred mode, unless a write is
                # pending against the device.
                if not self._mode_busy:
                    self._publish_value(role_service, "/Mode",
                                        4 if st == 0 else 1)

                # /Serial — published on every role that declares it.
                try:
                    if self.info.get("serial"):
                        self._publish_value(role_service, "/Serial",
                                            self.info["serial"])
                except KeyError:
                    pass

                # /Settings/BatteryVoltage — derived from the model name
                # using the Orion-TR-aware shared parser.  An Orion-TR
                # Smart 12/24 has a 24 V output (battery side).
                battery_v = battery_voltage_from_model(
                    model, pid_table=_ORION_PRODUCT_NAMES,
                    pid=self.info.get("product_id"))
                if battery_v is not None:
                    try:
                        self._publish_value(role_service,
                                            "/Settings/BatteryVoltage",
                                            battery_v, sensor_type="voltage")
                    except KeyError:
                        pass

                # Force /ProductId to match the active service type.  gui-v2
                # keys its layout off /ProductId for alternator services.
                if is_alternator:
                    self._publish_value(role_service, "/ProductId",
                                        ALTERNATOR_PRODUCT_ID)
                else:
                    self._publish_value(role_service, "/ProductId",
                                        self.info["product_id"])

                # History accumulators — alternator role only (charging
                # context).  Tick from the *real* advertised state, not
                # the EXTERNAL_CONTROL override.
                if is_alternator:
                    # Orion-TR adv carries input/output voltage but not
                    # output current directly; pass None so ChargedAh
                    # only ticks once we have a real current source.
                    self._tick_history(state=st, current_a=None)
                    self._publish_history(role_service)

            role_service.connect()

    # ------------------------------------------------------------------
    # dcdc ↔ alternator flip
    # ------------------------------------------------------------------

    def _ensure_role_for_state(self, device_state: int) -> None:
        needed = "alternator" if device_state in _CHARGER_MODES else "dcdc"
        if needed == self._current_role_name:
            return
        logger.info("%s: device state %d — switching role from %r to %r",
                    self._plog, device_state,
                    self._current_role_name, needed)
        self._swap_role(needed)

    @staticmethod
    def _enabled_setting_path(dev_id_str: str, role_name: str) -> str:
        # Mirrors what dbus-ble-sensors-py uses for its per-role
        # Enabled flag: /Settings/Devices/<dev_id>/<role>/Enabled.
        return f"/Settings/Devices/{dev_id_str}/{role_name}/Enabled"

    def _carry_enabled_flag_to(self, new_role_name: str) -> None:
        """Persist Enabled=1 on the new role's settings path *before* it
        is registered, so register_role_service()'s on-startup callback
        sees the flag and immediately connects the new D-Bus service.

        Without this, role swaps from dcdc → alternator (or back) leave
        the new role disconnected from D-Bus until the user re-toggles
        Enabled in gui-v2.  We carry the flag from whichever existing
        role currently has it set."""
        previously_enabled = False
        for old_role_name in self._role_services:
            try:
                old_path = self._enabled_setting_path(
                    self.info["dev_id"], old_role_name)
                if int(self._dbus_settings.get_value(old_path) or 0) == 1:
                    previously_enabled = True
                    break
            except Exception:
                logger.exception(
                    "%s: could not read Enabled flag for %r",
                    self._plog, old_role_name)
        if not previously_enabled:
            return
        new_path = self._enabled_setting_path(
            self.info["dev_id"], new_role_name)
        try:
            self._dbus_settings.set_item(new_path, 1, 0, 1, silent=True)
            self._dbus_settings.set_value(new_path, 1)
            logger.info("%s: carried Enabled=1 across to role %r",
                        self._plog, new_role_name)
        except Exception:
            logger.exception(
                "%s: failed to carry Enabled flag to %r",
                self._plog, new_role_name)

    def _swap_role(self, new_role_name: str) -> None:
        # Mirror Enabled=1 across before tearing down the old role —
        # register_role_service() reads the flag at startup and uses it
        # to decide whether to immediately connect the new D-Bus service.
        self._carry_enabled_flag_to(new_role_name)

        # Tear down every existing role.  There should only be one at a
        # time for an Orion-TR, but be defensive.
        for name, role_service in list(self._role_services.items()):
            try:
                role_service.disconnect()
            except Exception:
                logger.exception("%s: disconnect failed for role %r",
                                 self._plog, name)
            try:
                DbusBleService.get().unregister_role_service(role_service)
            except Exception:
                logger.exception("%s: unregister failed for role %r",
                                 self._plog, name)
        self._role_services.clear()

        # Register the new role
        role_cls = BleRole.get_class(new_role_name)
        if role_cls is None:
            logger.error("%s: role %r not registered — keeping previous",
                         self._plog, new_role_name)
            return
        logger.info("%s: building new role %r", self._plog, new_role_name)
        role = role_cls({})
        try:
            role.check_configuration()
        except ValueError:
            logger.exception("%s: role %r configuration invalid",
                             self._plog, new_role_name)
            return
        try:
            role_service = DbusRoleService(self, role)
        except Exception:
            logger.exception("%s: DbusRoleService(%r) construction failed",
                             self._plog, new_role_name)
            return
        try:
            role_service.load_settings()
        except Exception:
            logger.exception("%s: role %r load_settings (init) failed",
                             self._plog, new_role_name)
            return
        try:
            self._role_services[new_role_name] = role_service
            DbusBleService.get().register_role_service(role_service)
        except Exception:
            logger.exception("%s: register_role_service(%r) failed",
                             self._plog, new_role_name)
            self._role_services.pop(new_role_name, None)
            return
        self._current_role_name = new_role_name
        self.info["roles"] = {new_role_name: {}}
        logger.info("%s: role swap to %r complete",
                    self._plog, new_role_name)

    # ------------------------------------------------------------------
    # Mode write (GATT)
    # ------------------------------------------------------------------

    def _orion_on_mode_write(self,
                             role_service: DbusRoleService,
                             value: int) -> bool:
        if value not in (1, 4):
            return False
        writer = _gatt()
        if writer.busy:
            logger.warning("%s: GATT writer busy", self._plog)
            return False

        self._mode_busy = True
        mac = _format_mac_colons(self.info["dev_mac"])
        mode_byte = 4 if value == 4 else 1

        # The mode-write path pairs, connects, writes VREG 0x0200 and
        # disconnects.  Like the key-provisioner it needs the adapter to
        # itself, so pause the scan loop for the duration.
        pause_scanning("orion-tr /Mode write")

        def on_done(success: bool):
            try:
                self._mode_busy = False
                if not success:
                    logger.error("%s: GATT mode write failed", self._plog)
            finally:
                resume_scanning("orion-tr /Mode write")

        writer.write_register(
            mac,
            self._pairing_passkey,
            VREG_DEVICE_MODE,
            bytes([mode_byte]),
            on_done=on_done,
        )
        return True

    # ------------------------------------------------------------------
    # DVCC + user-facing setting writes (GATT, queued)
    # ------------------------------------------------------------------
    #
    # Same VREG layout as the IP22 for the two voltage setpoints
    # (0xEDF7 absorption, 0xEDF6 float), gated on 0xEDF1 = USER (0xFF).
    # The max-current VREG (IP22 = 0xEDF0) is *not* implemented on this
    # firmware — see the note at the top of the file.  ChargeCurrent /
    # ChargeCurrentLimit handlers therefore persist to settings without
    # emitting a GATT write; the BMS off-mechanism (drop ChargeVoltage)
    # remains fully wired through 0xEDF7.

    def _orion_ensure_battery_type_user(self) -> None:
        if self._battery_type_is_user is True:
            return

        def _on_user_set(success: bool):
            if success:
                self._battery_type_is_user = True
            else:
                logger.error(
                    "%s: GATT BatteryType=USER write failed", self._plog)

        self._enqueue_write(
            VREG_BATTERY_TYPE, bytes([BATTERY_TYPE_USER]),
            on_complete=_on_user_set,
        )

    def _orion_voltage_write(self, vreg: int, value_volts,
                              suffix: str, role_path: str) -> bool:
        """Shared body for /Link/ChargeVoltage, /Settings/AbsorptionVoltage,
        and /Settings/FloatVoltage on the Orion-TR.  Validates, dedupes,
        ensures the USER battery-type guard, and queues the GATT write
        — same shape as the IP22 voltage handlers."""
        try:
            new_v = float(value_volts)
        except (TypeError, ValueError):
            return False
        if new_v <= 0 or new_v > 80:
            return False
        last = self._last_pushed_charge_voltage_v
        if last is not None and abs(new_v - last) < CHARGE_VOLTAGE_DEADBAND_V:
            self._persist_setting(suffix, new_v)
            return True
        value_bytes = encode_u16_le_scaled(new_v, 100)
        if value_bytes is None:
            return False
        self._orion_ensure_battery_type_user()

        def on_done(success: bool):
            if success:
                self._last_pushed_charge_voltage_v = new_v
                self._persist_setting(suffix, new_v)

        self._enqueue_write(vreg, value_bytes, on_complete=on_done)
        return True

    def _orion_persist_only_write(self, suffix: str, deadband: float,
                                   last_attr: str, role_path: str,
                                   value, value_min: float,
                                   value_max: float,
                                   reason: str) -> bool:
        """Used for ChargeCurrent paths until we locate the Orion-TR's
        max-current VREG (IP22's 0xEDF0 is unknown on this firmware).
        Validates, dedupes, persists to settings, and logs once at
        warning level so it's clear the value is captured but not
        pushed to the wire."""
        try:
            new = float(value)
        except (TypeError, ValueError):
            return False
        if not (value_min <= new <= value_max):
            return False
        last = getattr(self, last_attr, None)
        if last is not None and abs(new - last) < deadband:
            self._persist_setting(suffix, new)
            return True
        setattr(self, last_attr, new)
        self._persist_setting(suffix, new)
        logger.warning("%s: %s=%.3f stored — %s", self._plog,
                       role_path, new, reason)
        return True

    # /Link/ChargeCurrent — DVCC envelope.  Persist-only until the
    # Orion-TR max-current VREG is mapped (see file header).
    def _orion_on_link_charge_current_write(self, role_service, value):
        self._set_dvcc_engaged(role_service, True)
        return self._orion_persist_only_write(
            "ChargeCurrentLimit", CHARGE_CURRENT_DEADBAND_A,
            "_last_pushed_charge_current_a", "/Link/ChargeCurrent",
            value, 0.0, 1000.0,
            "Orion-TR max-current VREG not yet located on this firmware "
            "— BMS off-mechanism uses /Link/ChargeVoltage which IS wired")

    # /Link/ChargeVoltage — DVCC primary lever.  GATT-wired to 0xEDF7.
    def _orion_on_link_charge_voltage_write(self, role_service, value):
        self._set_dvcc_engaged(role_service, True)
        return self._orion_voltage_write(
            VREG_ABSORPTION_VOLTAGE, value,
            "AbsorptionVoltage", "/Link/ChargeVoltage")

    # /Settings/ChargeCurrentLimit — user-set cap.  Persist-only.
    def _orion_on_charge_current_limit_write(self, role_service, value):
        return self._orion_persist_only_write(
            "ChargeCurrentLimit", CHARGE_CURRENT_DEADBAND_A,
            "_last_pushed_charge_current_a",
            "/Settings/ChargeCurrentLimit",
            value, 0.0, 1000.0,
            "Orion-TR max-current VREG not yet located on this firmware")

    # /Settings/AbsorptionVoltage — GATT-wired to 0xEDF7 (same VREG as
    # /Link/ChargeVoltage; user-set vs DVCC-set converge on the wire).
    def _orion_on_absorption_voltage_write(self, role_service, value):
        return self._orion_voltage_write(
            VREG_ABSORPTION_VOLTAGE, value,
            "AbsorptionVoltage", "/Settings/AbsorptionVoltage")

    # /Settings/FloatVoltage — GATT-wired to 0xEDF6.
    def _orion_on_float_voltage_write(self, role_service, value):
        return self._orion_voltage_write(
            VREG_FLOAT_VOLTAGE, value,
            "FloatVoltage", "/Settings/FloatVoltage")
