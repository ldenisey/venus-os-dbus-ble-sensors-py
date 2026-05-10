"""
Shared infrastructure for BLE-attached Victron chargers.

Originally extracted from ``ble_device_ip22_charger`` so the
Orion-TR driver (when in charger / alternator-regulator role) can reuse
the same per-device GATT write queue, settings persistence, history
accumulators, charger-alarm derivation, and DVCC engagement logic
without duplicating the implementations.

The shared surface is split into three layers:

1. **Module-level constants** (``STATE_EXTERNAL_CONTROL``,
   ``CHARGER_ALARM_PATHS``, deadbands, etc.) — pure values, no behaviour.
2. **Module-level pure helpers** (``serial_from_advertised_name``,
   ``alarms_for_error``, ``settings_path``, ``encode_u16_le_scaled``) —
   stateless utilities the device classes call directly.
3. **``ChargerCommonMixin``** — instance-state-bearing methods (GATT
   queue, persistence, history, alarms, DVCC engagement).  Subclasses
   set ``SETTINGS_NS_PREFIX`` (e.g. ``"ip22"`` or ``"orion_tr"``) and
   call ``_init_charger_common()`` from their ``__init__``.

Callers must already provide on the instance:

  * ``self.info["dev_mac"]`` — colon-less hex MAC.
  * ``self._dbus_settings`` — a ``DbusSettingsService`` instance.
  * ``self._pairing_passkey`` — int passkey for GATT auth.
  * ``self._plog`` — log prefix string.
  * ``self._role_services`` — dict of role-name -> DbusRoleService.
  * ``self._mode_busy`` — bool flag set during in-flight /Mode writes.

These are already populated by both ``BleDeviceIP22Charger`` and
``BleDeviceOrionTR`` (the latter via ``ble_device_orion_tr``).
"""
from __future__ import annotations

import logging
import re
import time
from typing import Callable, Optional

import dbus
from gi.repository import GLib

from orion_tr_gatt import AsyncGATTWriter
from scan_control import pause_scanning, resume_scanning

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# OperationMode value gui-v2 / dbus-systemcalc-py recognise as
# "this charger is being externally controlled by the GX / a BMS".
STATE_EXTERNAL_CONTROL = 252

# /State values that count as "actively charging" for the
# OperationTime accumulator.  Bulk / Absorption / Float / Storage /
# Equalize-manual / Recondition.
HISTORY_TICK_STATES: frozenset[int] = frozenset({3, 4, 5, 6, 7, 247})

# Deadbands suppress GATT churn when DVCC re-publishes the same
# setpoint every cycle.  Tolerances match the resolution chargers
# typically resolve to: 0.1 A and 0.01 V on Victron 0.1A / 0.01V
# u16-LE encodings.
CHARGE_CURRENT_DEADBAND_A = 0.1
CHARGE_VOLTAGE_DEADBAND_V = 0.05

# Persist accumulators no more than once per minute so we don't beat
# up flash on the ~1 Hz advertisement interval.
HISTORY_FLUSH_INTERVAL_S = 60.0

# How long to wait between drain retries when the shared
# AsyncGATTWriter is busy with a previous write.
PENDING_DRAIN_INTERVAL_MS = 1500

# Charger-side /Alarms/* paths — the charger's *own* failure modes.
# Battery-monitor / inverter alarms are intentionally absent (a charger
# isn't the authority on battery state).  Battery-temperature errors
# (ChargerError 1 / 14) surface only via /ErrorCode and gui-v2's
# ChargerError::getDescription() text.
CHARGER_ALARM_PATHS: tuple[str, ...] = (
    "/Alarms/HighTemperature",   # heatsink / internal temp
    "/Alarms/HighVoltage",       # output-bus over-voltage
    "/Alarms/HighRipple",        # AC-input ripple
    "/Alarms/Fan",               # cooling-fan failure
)

# Map: ChargerError code -> {alarm_path: severity (0=ok, 1=warn, 2=alarm)}.
CHARGER_ERROR_TO_ALARMS: dict[int, dict[str, int]] = {
    2:  {"/Alarms/HighVoltage": 2},
    11: {"/Alarms/HighRipple": 2},
    17: {"/Alarms/HighTemperature": 2},   # TEMPERATURE_CHARGER
    22: {"/Alarms/HighTemperature": 2},   # INTERNAL_TEMPERATURE_A
    23: {"/Alarms/HighTemperature": 2},   # INTERNAL_TEMPERATURE_B
    24: {"/Alarms/Fan": 2},
    26: {"/Alarms/HighTemperature": 2},   # OVERHEATED
}

# Standard Victron serial-number tokens look like ``HQ2133XMU6Y`` —
# two letters + 8-12 alphanumerics.  Used to harvest /Serial from a
# BlueZ advertised device name when the encrypted advertisement payload
# itself doesn't carry a serial.
_SERIAL_TOKEN_RE = re.compile(r"(HQ[0-9A-Z]{8,12})")

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def serial_from_advertised_name(name: Optional[str]) -> Optional[str]:
    """Extract the Victron serial token (``HQxxxxxxxxx``) from a BlueZ
    advertised name like ``"BSC IP22 12/30...HQ2133XMU6Y"`` or
    ``"Orion Smart HQ20326VVVJ"``.  Returns ``None`` if no token is
    found."""
    if not name:
        return None
    m = _SERIAL_TOKEN_RE.search(name)
    return m.group(1) if m else None

def alarms_for_error(error_code: int) -> dict[str, int]:
    """Return the ``{alarm_path: severity}`` dict to apply for a
    ChargerError code.  Paths *not* present in the result should be
    cleared to 0 by the caller — that's how an alarm de-asserts when
    the underlying error goes away."""
    return CHARGER_ERROR_TO_ALARMS.get(int(error_code), {})

def settings_path(ns_prefix: str, dev_mac: str, suffix: str) -> str:
    """Build a ``/Settings/Devices/<ns>_<mac_no_separators>/<suffix>``
    path.  ``ns_prefix`` is per-driver (``"ip22"``, ``"orion_tr"``);
    ``dev_mac`` may include colons or not."""
    s = dev_mac.lower().replace(":", "")
    return f"/Settings/Devices/{ns_prefix}_{s}/{suffix}"

def format_mac_colons(dev_mac: str) -> str:
    """Convert a colon-less hex MAC to the ``AA:BB:CC:DD:EE:FF`` form
    BlueZ uses on its D-Bus surface."""
    s = dev_mac.lower().replace(":", "")
    return ":".join(s[i:i + 2] for i in range(0, 12, 2)).upper()

def bluez_device_name(dev_mac: str) -> Optional[str]:
    """Read ``Name`` (or ``Alias``) from BlueZ for a device by MAC.
    Returns the first non-empty value across all adapters or ``None``
    if no Device1 entry is found."""
    mac_suffix = "/dev_" + format_mac_colons(dev_mac).replace(":", "_")
    try:
        bus = (dbus.SessionBus() if False else dbus.SystemBus())
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

_NOMINAL_BATTERY_VOLTAGES = (12, 24, 36, 48)

# Match the Orion-TR Smart naming: ``"Orion Smart 12V/24V-15A …"`` or
# the older shorthand ``"Orion-TR Smart 12/24-10A"``.  The second
# voltage is the *output* side, which is the battery the Orion-TR
# regulates against — that's what we want for /Settings/BatteryVoltage.
_ORION_NAME_RE = re.compile(
    r"Orion[-\w ]*\s+(\d+)\s*V?\s*/\s*(\d+)\s*V?\s*-\s*\d+\s*A?",
    re.IGNORECASE,
)

def battery_voltage_from_model(model_name: Optional[str],
                                pid_table: Optional[dict[int, str]] = None,
                                pid: Optional[int] = None) -> Optional[int]:
    """Extract the nominal battery voltage (12 / 24 / 36 / 48) from a
    Victron model-name string, with fallback to a per-driver
    ``pid -> name`` table.

    Recognises two naming conventions:

      * **IP22 / Phoenix Smart chargers** —
        ``"... Charger {V}|{A} ..."`` or ``"... Charger {V}/{A} ..."``.
        The first integer is the battery voltage.

      * **Orion-TR Smart DC-DC** —
        ``"Orion [-TR] Smart {Vin}[V]/{Vout}[V]-{A}A …"``.  The
        *output* voltage is the battery side (an Orion-TR Smart 12/24
        regulates a 24 V house bank from a 12 V starter battery).

    Returns ``None`` when the name doesn't match either convention or
    the resolved voltage isn't one of the canonical Victron rails.
    Pass ``pid_table`` + ``pid`` so the off-state / short-beacon path
    can still resolve a value when no model name is in scope.
    """
    if model_name is None and pid_table is not None and pid is not None:
        model_name = pid_table.get(pid)
    if not model_name:
        return None

    # Orion-TR pattern first — it's more specific (requires the
    # "Orion" keyword) so it doesn't accidentally consume IP22 names.
    m = _ORION_NAME_RE.search(model_name)
    if m:
        try:
            v_out = int(m.group(2))
            if v_out in _NOMINAL_BATTERY_VOLTAGES:
                return v_out
        except ValueError:
            pass
        # Some 12V/12V or 24V/24V variants — the output is still the
        # battery side, even though it equals input.
        try:
            v_out = int(m.group(2))
            if v_out in _NOMINAL_BATTERY_VOLTAGES:
                return v_out
        except ValueError:
            pass

    # IP22 / Phoenix Smart pattern: "... Charger {V}{|or/}{A} ..."
    if "Charger" in model_name:
        for sep in ("|", "/"):
            if sep not in model_name:
                continue
            tail = model_name.split("Charger", 1)
            if len(tail) != 2:
                continue
            spec = tail[1].strip().split()[0]
            if sep not in spec:
                continue
            head = spec.split(sep, 1)[0]
            try:
                v = int(head)
                if v in _NOMINAL_BATTERY_VOLTAGES:
                    return v
            except ValueError:
                pass

    return None

def encode_u16_le_scaled(value: float, scale: int,
                         max_value: int = 0xFFFF) -> Optional[bytes]:
    """Encode ``value * scale`` as a little-endian u16 byte pair,
    clamping range checks at ``max_value``.  Returns ``None`` if the
    scaled value is out of range — the caller should reject the write.

    Examples:
        encode_u16_le_scaled(14.4, 100)   # 14.40 V -> 0xA0 0x05  (1440)
        encode_u16_le_scaled(18.0, 10)    # 18.0 A  -> 0xB4 0x00  (180)
    """
    try:
        n = int(round(float(value) * scale))
    except (TypeError, ValueError):
        return None
    if n < 0 or n > max_value:
        return None
    return bytes([n & 0xFF, (n >> 8) & 0xFF])

# ---------------------------------------------------------------------------
# Mixin
# ---------------------------------------------------------------------------

class ChargerCommonMixin:
    """Behaviour shared between every BLE-driven Victron charger driver
    in this service.

    Subclasses must:
      * set ``SETTINGS_NS_PREFIX`` to a short identifier used in
        ``/Settings/Devices/<ns>_<mac>/`` paths.
      * call ``self._init_charger_common()`` from their ``__init__``.

    Instances are expected to already provide ``self.info``,
    ``self._plog``, ``self._dbus_settings``, ``self._pairing_passkey``,
    ``self._role_services``, and ``self._mode_busy`` — see the module
    docstring.
    """

    # Subclasses override.  Used as the namespace component of every
    # /Settings/Devices/<ns>_<mac>/* path the charger persists.
    SETTINGS_NS_PREFIX: str = "charger"

    # GATT writer instance held at module scope by the device file —
    # subclasses can override to supply a different writer factory if
    # they ever need one.
    @staticmethod
    def _gatt_writer() -> AsyncGATTWriter:
        # Late import to avoid a circular dependency: every charger
        # device module manages its own ``_gatt_writer`` singleton.
        # Default fallback creates one against the system bus.
        global _DEFAULT_GATT
        try:
            _DEFAULT_GATT
        except NameError:
            _DEFAULT_GATT = AsyncGATTWriter(dbus.SystemBus())
        return _DEFAULT_GATT

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_charger_common(self) -> None:
        # GATT write queue — collapses DVCC bursts for the same VREG
        # and serialises against the single-slot AsyncGATTWriter.
        self._pending_writes: dict[
            int, tuple[bytes, Optional[Callable[[bool], None]]]] = {}
        self._pending_drain_scheduled = False

        # DVCC engagement state.  /Link/NetworkStatus + /State both
        # follow this — see _set_dvcc_engaged + _derive_published_state.
        self._dvcc_engaged = False
        self._last_advertised_state: int = 0

        # History accumulators.  See _tick_history + _publish_history.
        self._history_op_time_s: float = 0.0
        self._history_charged_ah: float = 0.0
        self._history_last_tick: Optional[float] = None
        self._history_last_flush: float = 0.0
        # Flag flipped True the first time _tick_history sees a real
        # (non-None) current reading — used by _publish_history to
        # decide whether to write /ChargedAh.  The Orion-TR's
        # encrypted advertisement doesn't carry current, so its history
        # accumulator never sees a real value and /ChargedAh stays at
        # its initial role-declared value (None) — which gui-v2 / VRM
        # render as "unknown" rather than "the user charged 0 Ah".
        self._history_has_current_data = False

        # DVCC dedupe state — last value we successfully GATT-pushed.
        self._last_pushed_charge_current_a: Optional[float] = None
        self._last_pushed_charge_voltage_v: Optional[float] = None

    # ------------------------------------------------------------------
    # Settings paths
    # ------------------------------------------------------------------

    def _settings_path(self, suffix: str) -> str:
        return settings_path(self.SETTINGS_NS_PREFIX,
                             self.info["dev_mac"], suffix)

    def _persist_setting(self, suffix: str, value) -> None:
        """Save a float-typed value into
        ``/Settings/Devices/<ns>_<mac>/<suffix>``.  Creates the entry
        silently on first write."""
        path = self._settings_path(suffix)
        try:
            self._dbus_settings.set_item(path, float(value), silent=True)
            self._dbus_settings.set_value(path, float(value))
        except Exception:
            logger.exception("%s: failed to persist %s=%r",
                             self._plog, path, value)

    def _try_get_setting(self, suffix: str):
        try:
            return self._dbus_settings.try_get_value(
                self._settings_path(suffix))
        except Exception:
            return None

    # ------------------------------------------------------------------
    # GATT write queue
    # ------------------------------------------------------------------

    def _enqueue_write(self, vreg: int, value_bytes: bytes,
                       on_complete: Optional[Callable[[bool], None]] = None
                       ) -> None:
        # Newer write to the same VREG supersedes any pending one — DVCC
        # only cares about the latest setpoint anyway.
        self._pending_writes[vreg] = (value_bytes, on_complete)
        self._kick_pending_writes()

    def _kick_pending_writes(self) -> None:
        if not self._pending_writes:
            return
        writer = self._gatt_writer()
        if writer.busy:
            self._schedule_drain()
            return

        vreg, (value_bytes, on_complete) = next(
            iter(self._pending_writes.items()))
        del self._pending_writes[vreg]

        mac = format_mac_colons(self.info["dev_mac"])
        pause_scanning(f"{self.SETTINGS_NS_PREFIX} GATT write 0x{vreg:04X}")

        def on_done(success: bool):
            try:
                if not success:
                    logger.error("%s: GATT write 0x%04X failed",
                                 self._plog, vreg)
                if on_complete is not None:
                    try:
                        on_complete(success)
                    except Exception:
                        logger.exception(
                            "%s: pending-write completion callback failed",
                            self._plog)
            finally:
                resume_scanning(
                    f"{self.SETTINGS_NS_PREFIX} GATT write 0x{vreg:04X}")
                self._schedule_drain()

        writer.write_register(
            mac, self._pairing_passkey,
            vreg, value_bytes,
            on_done=on_done,
        )

    def _schedule_drain(self) -> None:
        if self._pending_drain_scheduled or not self._pending_writes:
            return
        self._pending_drain_scheduled = True

        def _on_drain_tick():
            self._pending_drain_scheduled = False
            try:
                self._kick_pending_writes()
            except Exception:
                logger.exception("%s: pending-write drain tick failed",
                                 self._plog)
            return False  # one-shot

        GLib.timeout_add(PENDING_DRAIN_INTERVAL_MS, _on_drain_tick)

    # ------------------------------------------------------------------
    # History accumulators
    # ------------------------------------------------------------------

    def _tick_history(self, state: int, current_a: Optional[float]) -> None:
        """Increment OperationTime and ChargedAh based on elapsed time
        since the last advertisement.  ``state`` is the *raw advertised*
        state (not the DVCC-overridden value); ``current_a`` is the
        current Dc/0/Current reading.  We only count when the charger is
        in an active charge state and when current is positive."""
        now = time.monotonic()
        last = self._history_last_tick
        self._history_last_tick = now
        if last is None:
            return
        dt = now - last
        if dt <= 0 or dt > 600.0:
            # Drop unrealistically-long gaps so service-restart pauses
            # don't credit the user with phantom charging.
            return
        if state in HISTORY_TICK_STATES:
            self._history_op_time_s += dt
        if current_a is not None:
            self._history_has_current_data = True
            if current_a > 0.0:
                self._history_charged_ah += (current_a * dt) / 3600.0

    def _publish_history(self, role_service) -> None:
        role_service["/History/Cumulative/User/OperationTime"] = int(
            self._history_op_time_s)
        # Only write /ChargedAh if we've actually seen current data —
        # see _history_has_current_data note in _init_charger_common.
        # Devices whose advertisement decoder doesn't expose current
        # (e.g. Orion-TR Smart) leave the path at its declared default
        # (None), which gui-v2 renders as "--" rather than "0 Ah".
        if self._history_has_current_data:
            role_service["/History/Cumulative/User/ChargedAh"] = round(
                self._history_charged_ah, 2)

        now = time.monotonic()
        if now - self._history_last_flush < HISTORY_FLUSH_INTERVAL_S:
            return
        self._history_last_flush = now
        try:
            self._persist_setting("History/OperationTime",
                                  float(self._history_op_time_s))
            if self._history_has_current_data:
                self._persist_setting("History/ChargedAh",
                                      float(self._history_charged_ah))
        except Exception:
            logger.exception("%s: history flush failed", self._plog)

    # ------------------------------------------------------------------
    # Charger-side alarms
    # ------------------------------------------------------------------

    def _publish_alarms(self, role_service, error_code: int) -> None:
        """Map the current ChargerError code onto charger-side
        /Alarms/* paths, clearing any path that the new error doesn't
        assert."""
        active = alarms_for_error(error_code)
        for path in CHARGER_ALARM_PATHS:
            severity = active.get(path, 0)
            try:
                if role_service[path] != severity:
                    role_service[path] = severity
            except KeyError:
                logger.debug("%s: alarm path %s missing from role",
                             self._plog, path)

    # ------------------------------------------------------------------
    # DVCC engagement + /State override
    # ------------------------------------------------------------------

    def _derive_published_state(self, advertised_state: int) -> int:
        """Return the value that should appear on /State right now.
        ``STATE_EXTERNAL_CONTROL`` (252) when DVCC is engaged and the
        device is powered, otherwise the raw advertised state.  Off
        stays off."""
        if advertised_state == 0:
            return 0
        if self._dvcc_engaged:
            return STATE_EXTERNAL_CONTROL
        return advertised_state

    def _set_dvcc_engaged(self, role_service, engaged: bool) -> None:
        """Track DVCC engagement: flip ``/Link/NetworkStatus`` between
        ``4`` (stand-alone) and ``1`` (DVCC active), and immediately
        re-derive ``/State`` using the cached advertised state so we
        don't wait for the next telemetry tick."""
        new_status = 1 if engaged else 4
        try:
            current = role_service["/Link/NetworkStatus"]
        except Exception:
            current = None
        was_engaged = self._dvcc_engaged
        self._dvcc_engaged = engaged
        if current != new_status:
            role_service["/Link/NetworkStatus"] = new_status

        if was_engaged != engaged:
            new_state = self._derive_published_state(
                self._last_advertised_state)
            try:
                if role_service["/State"] != new_state:
                    role_service["/State"] = new_state
            except Exception:
                logger.debug("%s: /State refresh on engage-change failed",
                             self._plog)

    # ------------------------------------------------------------------
    # /Link/NetworkMode + /Settings/BmsPresent passive handlers
    # ------------------------------------------------------------------

    def _on_link_network_mode_write(self, role_service, value) -> bool:
        try:
            mode = int(value)
        except (TypeError, ValueError):
            return False
        try:
            bms_present = int(role_service["/Settings/BmsPresent"] or 0)
        except Exception:
            bms_present = 0
        self._set_dvcc_engaged(role_service,
                               mode != 0 or bms_present == 1)
        return True

    def _on_settings_bms_present_write(self, role_service, value) -> bool:
        try:
            bms_present = int(value)
        except (TypeError, ValueError):
            return False
        try:
            mode = int(role_service["/Link/NetworkMode"] or 0)
        except Exception:
            mode = 0
        self._set_dvcc_engaged(role_service,
                               bms_present == 1 or mode != 0)
        return True

    @staticmethod
    def _on_link_passive_write(_role_service, _value) -> bool:
        # Generic "store on D-Bus, no GATT" handler for sense paths
        # the device firmware has no consumer VREG for.
        return True

    # ------------------------------------------------------------------
    # Settings persistence reload
    # ------------------------------------------------------------------

    # Subclass overrides the suffix list — these are the IP22 / Orion-TR
    # union of currently-restored values.  Subclasses may shorten it.
    PERSISTED_SETTING_SUFFIXES_TO_PATHS: dict[str, str] = {
        "ChargeCurrentLimit":  "/Settings/ChargeCurrentLimit",
        "AbsorptionVoltage":   "/Settings/AbsorptionVoltage",
        "FloatVoltage":        "/Settings/FloatVoltage",
    }

    def load_persisted_charger_settings(self, role_service) -> None:
        """Pull saved /Settings/Devices/<ns>_<mac>/* values back into
        the role-service paths so a Cerbo reboot restores user-set
        values without a fresh GATT round-trip.  We do *not* GATT-write
        anything here — the device retains its own copy across power
        cycles, and DVCC will re-push setpoints anyway."""
        for suffix, role_path in self.PERSISTED_SETTING_SUFFIXES_TO_PATHS.items():
            v = self._try_get_setting(suffix)
            if v is None:
                continue
            try:
                role_service[role_path] = float(v)
            except Exception:
                logger.exception("%s: seed %s from settings failed",
                                 self._plog, role_path)

        # History accumulators — load once into in-memory state.
        v = self._try_get_setting("History/OperationTime")
        if v is not None:
            try:
                self._history_op_time_s = float(v)
            except (TypeError, ValueError):
                pass
        v = self._try_get_setting("History/ChargedAh")
        if v is not None:
            try:
                self._history_charged_ah = float(v)
            except (TypeError, ValueError):
                pass
