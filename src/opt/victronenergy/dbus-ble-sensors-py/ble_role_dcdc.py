"""
DC-DC converter role (Venus OS ``dcdc`` service type).

Used for Victron Orion-TR Smart devices in fixed-output / PSU mode so
they appear under the standard DC-DC device page in gui-v2
(``PageDcDcConverter.qml``).  When the device transitions into a
charger algorithm (bulk / absorption / float / storage) the parent
device class swaps to :mod:`ble_role_alternator`.

This role mirrors the integrated-charger surface the alternator role
publishes — DVCC paths, charger-side alarms, history accumulators,
identity (``/Serial``), and the user-facing settings — so a BMS / GX
can drive an Orion-TR even while it is operating as a power supply.
The bound handlers come from ``ChargerCommonMixin`` (passive ones)
and ``BleDeviceOrionTR`` (the GATT-bound writes); a missing handler
on the BLE device class degrades gracefully to a passive store-only
callback.
"""
from ble_role import BleRole
from ve_types import *

class BleRoleDcdc(BleRole):
    NAME = "dcdc"

    def __init__(self, config: dict = None):
        super().__init__()
        self.info.update(
            {
                "name": "dcdc",
                "dev_instance": 130,
                "settings": [],
                "alarms": [],
            }
        )

    def init(self, role_service):
        svc = role_service._dbus_service
        ble = role_service._ble_device

        def _bind(handler_name: str):
            handler = getattr(ble, handler_name, None)
            if handler is None:
                return lambda _path, _value: True
            return lambda _path, value: bool(handler(role_service, value))

        with svc as s:
            # ----------------------------------------------------------
            # Telemetry
            # ----------------------------------------------------------
            s.add_path("/Dc/In/V", None)
            s.add_path("/Dc/In/I", None)
            s.add_path("/Dc/In/P", None)
            s.add_path("/Dc/0/Voltage", None)
            s.add_path("/Dc/0/Current", None)
            s.add_path("/Dc/0/Power", None)
            s.add_path("/Dc/0/Temperature", None)
            s.add_path("/State", 0)
            s.add_path("/ErrorCode", 0)
            s.add_path("/DeviceOffReason", 0)
            s.add_path("/Relay/0/State", 0)

            # /Mode — Orion-TR firmware *does* implement VREG 0x0200,
            # so this stays writable.  Same handler as the alternator
            # role.
            def on_mode(path, value):
                return ble._orion_on_mode_write(role_service, int(value))
            s.add_path("/Mode", 1,
                       writeable=True, onchangecallback=on_mode)

            # /Serial — populated lazily on first telemetry tick.
            s.add_path("/Serial", None)

            # /Settings/BatteryVoltage — derived from the model name.
            s.add_path("/Settings/BatteryVoltage", None)

            # /History/Cumulative/User/OperationTime — ticks while the
            # device is in a charging state (won't accumulate during
            # PSU mode where /State = 11).
            s.add_path("/History/Cumulative/User/OperationTime", 0)
            # /History/Cumulative/User/ChargedAh — initial *None* because
            # the Orion-TR's encrypted advertisement does not carry
            # current data.  The mixin only writes this path when it
            # has seen a real (non-None) current reading, so the value
            # stays None and gui-v2 / VRM render it as "--" rather than
            # misleadingly charting 0 Ah.
            s.add_path("/History/Cumulative/User/ChargedAh", None)

            # Charger-side alarms (own failure modes only).
            s.add_path("/Alarms/HighTemperature", 0)
            s.add_path("/Alarms/HighVoltage", 0)
            s.add_path("/Alarms/HighRipple", 0)
            s.add_path("/Alarms/Fan", 0)

            # ----------------------------------------------------------
            # DVCC contract — same paths as the alternator role.  In PSU
            # mode the user can still want a BMS to control the output
            # voltage envelope (e.g. a Lithium house bank fed via
            # Orion-TR Smart from an alternator).
            # ----------------------------------------------------------
            s.add_path("/Link/NetworkStatus", 4)
            s.add_path(
                "/Link/NetworkMode", 0,
                writeable=True,
                onchangecallback=_bind("_on_link_network_mode_write"))
            s.add_path(
                "/Link/ChargeCurrent", None,
                writeable=True,
                onchangecallback=_bind("_orion_on_link_charge_current_write"))
            s.add_path(
                "/Link/ChargeVoltage", None,
                writeable=True,
                onchangecallback=_bind("_orion_on_link_charge_voltage_write"))
            s.add_path("/Link/TemperatureSense", None,
                       writeable=True,
                       onchangecallback=_bind("_on_link_passive_write"))
            s.add_path("/Link/VoltageSense", None,
                       writeable=True,
                       onchangecallback=_bind("_on_link_passive_write"))
            s.add_path("/Link/BatteryCurrent", None,
                       writeable=True,
                       onchangecallback=_bind("_on_link_passive_write"))
            s.add_path("/Link/TemperatureSenseActive", 0)
            s.add_path("/Link/VoltageSenseActive", 0)

            s.add_path(
                "/Settings/BmsPresent", 0,
                writeable=True,
                onchangecallback=_bind("_on_settings_bms_present_write"))

            # User-facing writable settings (persisted).
            s.add_path(
                "/Settings/ChargeCurrentLimit", None,
                writeable=True,
                onchangecallback=_bind(
                    "_orion_on_charge_current_limit_write"))
            s.add_path(
                "/Settings/AbsorptionVoltage", None,
                writeable=True,
                onchangecallback=_bind(
                    "_orion_on_absorption_voltage_write"))
            s.add_path(
                "/Settings/FloatVoltage", None,
                writeable=True,
                onchangecallback=_bind(
                    "_orion_on_float_voltage_write"))

        # Restore persisted user-set values onto the role-service paths.
        if hasattr(ble, "load_persisted_charger_settings"):
            try:
                ble.load_persisted_charger_settings(role_service)
            except Exception:
                import logging as _logging
                _logging.exception(
                    "orion-tr dcdc: load_persisted_charger_settings failed")
