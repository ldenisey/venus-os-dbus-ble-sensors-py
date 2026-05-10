"""
Charger role (Venus OS ``charger`` service type).

Used for Victron Blue Smart IP22 chargers reached over BLE so they appear
alongside VE.Direct Phoenix Smart IP43 units on the gui-v2 *DC Sources*
page and interact with the rest of the system through the standard
``com.victronenergy.charger`` D-Bus API.
"""
from ble_role import BleRole

class BleRoleCharger(BleRole):
    NAME = "charger"

    def __init__(self, config: dict = None):
        super().__init__()
        self.info.update(
            {
                "name": "charger",
                "dev_instance": 290,
                "settings": [],
                "alarms": [],
            }
        )

    def init(self, role_service):
        svc = role_service._dbus_service
        ble = role_service._ble_device

        def _bind(handler_name: str):
            """Build an onchangecallback that forwards to a method on the
            BLE device class if it exists, otherwise treats the write as a
            passive store-only path (D-Bus updates, no GATT)."""
            handler = getattr(ble, handler_name, None)
            if handler is None:
                return lambda _path, _value: True
            return lambda _path, value: bool(handler(role_service, value))

        with svc as s:
            # Output/battery side
            s.add_path("/Dc/0/Voltage", None)
            s.add_path("/Dc/0/Current", None)
            s.add_path("/Dc/0/Power", None)
            s.add_path("/Dc/0/Temperature", None)
            # Multi-output chargers (IP22 30A is single-output; 2/3 stay None)
            s.add_path("/Dc/1/Voltage", None)
            s.add_path("/Dc/1/Current", None)
            s.add_path("/Dc/2/Voltage", None)
            s.add_path("/Dc/2/Current", None)
            s.add_path("/NrOfOutputs", 1)

            # AC input
            s.add_path("/Ac/In/L1/I", None)
            s.add_path("/Ac/In/CurrentLimit", None)

            # Status.  /Mode and /DeviceOffReason are intentionally NOT
            # published on this role: the IP22 firmware has no writable
            # remote-on/off VREG, so a /Mode toggle would be decorative
            # at best and lie to the user at worst.  gui-v2's PageAcCharger
            # ListSwitch is gated on `dataItem.valid`, so omitting /Mode
            # makes the Switch row disappear cleanly instead of needing
            # a (charger-page-ignored) /Capabilities/HasNoDeviceOffMode
            # hint.
            s.add_path("/State", 0)
            s.add_path("/ErrorCode", 0)
            s.add_path("/Relay/0/State", 0)

            # /Serial — populated lazily on first telemetry tick from the
            # advertised BlueZ name (the encrypted payload itself doesn't
            # carry it).  Initial None lets vrmlogger / VRM cope with the
            # unknown-yet state.
            s.add_path("/Serial", None)

            # /Settings/BatteryVoltage — fixed nominal voltage per
            # product id (12 / 24 / 36 / 48 V).  Filled on first publish.
            s.add_path("/Settings/BatteryVoltage", None)

            # /History/Cumulative/User/OperationTime — ticks while in
            # a charging state.  Backed by com.victronenergy.settings.
            s.add_path("/History/Cumulative/User/OperationTime", 0)
            # /History/Cumulative/User/ChargedAh — initial *None* until
            # we've actually seen a current reading.  IP22's encrypted
            # advertisement carries output_current1 so this fills in on
            # the first telemetry tick.  See ChargerCommonMixin
            # _history_has_current_data for the lazy-publish gate.
            s.add_path("/History/Cumulative/User/ChargedAh", None)

            # /Alarms/* — charger-side alarms only (the charger's *own*
            # failure modes).  Severity 0=ok, 1=warning, 2=alarm.
            #
            # Intentionally absent:
            #   /Alarms/{High,Low}BatteryTemperature — battery-monitor /
            #     BMS paths; charger surfaces battery-temp errors via
            #     /ErrorCode (codes 1, 14) and suspends via /State
            #     instead of claiming authority over battery state.
            #   /Alarms/{LowVoltage, LowSoc, Overload, Ripple,
            #     LoadDisconnect, VecanDisconnected} — battery-monitor
            #     or VE.Bus / inverter paths.
            #
            # See the _CHARGER_ERROR_TO_ALARMS table in
            # ble_device_ip22_charger for the ChargerError -> path map.
            s.add_path("/Alarms/HighTemperature", 0)
            s.add_path("/Alarms/HighVoltage", 0)
            s.add_path("/Alarms/HighRipple", 0)
            s.add_path("/Alarms/Fan", 0)

            # ----------------------------------------------------------
            # DVCC contract — paths dbus-systemcalc-py writes onto a
            # charger to integrate it into the system.  Mirrors the set
            # an integrated VE.Bus / VE.Direct charger publishes.
            # ----------------------------------------------------------
            # /Link/NetworkStatus: 4 = "stand-alone" until DVCC takes over.
            # systemcalc flips this when a BMS / GX takes control.
            s.add_path("/Link/NetworkStatus", 4)

            # /Link/NetworkMode: bitmask DVCC writes to indicate which
            # links are active (1=ext control, 2=ext voltage, 4=BMS, ...).
            # We store the value AND flip /Link/NetworkStatus to reflect
            # DVCC engagement; IP22 firmware itself has no consumer VREG.
            s.add_path(
                "/Link/NetworkMode", 0,
                writeable=True,
                onchangecallback=_bind("_on_link_network_mode_write"))

            # /Link/ChargeCurrent: target current pushed by DVCC (amps).
            # Wired to VREG 0xEDF0 with a 0.1 A deadband so steady-state
            # DVCC re-publishes don't flap the GATT link.
            s.add_path(
                "/Link/ChargeCurrent", None,
                writeable=True,
                onchangecallback=_bind("_ip22_on_link_charge_current_write"))

            # /Link/ChargeVoltage: target absorption voltage pushed by
            # DVCC (volts).  Wired to VREG 0xEDF7 with 0.05 V deadband.
            # The IP22 requires battery-type = USER (VREG 0xEDF1 = 0xFF)
            # before 0xEDF7 accepts writes; the handler sets that
            # transparently on first use.
            s.add_path(
                "/Link/ChargeVoltage", None,
                writeable=True,
                onchangecallback=_bind("_ip22_on_link_charge_voltage_write"))

            # /Link/{TemperatureSense,VoltageSense,BatteryCurrent}: BMS
            # sense values DVCC pushes for temperature- and
            # voltage-compensated charging.  IP22 has no VREG consumer
            # for these; we surface them on D-Bus for systemcalc's own
            # bookkeeping but don't push to the wire.
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

            # /Settings/BmsPresent: DVCC writes 1 when a BMS is in the
            # system.  Same NetworkStatus side-effect as /Link/NetworkMode.
            s.add_path(
                "/Settings/BmsPresent", 0,
                writeable=True,
                onchangecallback=_bind("_on_settings_bms_present_write"))

            # /Settings/ChargeCurrentLimit — writable via GATT 0xEDF0.
            # Same VREG as /Link/ChargeCurrent above; both paths land at
            # 0xEDF0.  /Link/ChargeCurrent is the DVCC-side override,
            # /Settings/ChargeCurrentLimit is the user-set cap (gui-v2
            # settings page).  Persisted to com.victronenergy.settings.
            s.add_path(
                "/Settings/ChargeCurrentLimit", None,
                writeable=True,
                onchangecallback=_bind("_ip22_on_charge_current_limit_write"))

            # /Settings/AbsorptionVoltage — writable via GATT 0xEDF7
            # (with automatic 0xEDF1=USER guard).  Persisted.
            s.add_path(
                "/Settings/AbsorptionVoltage", None,
                writeable=True,
                onchangecallback=_bind("_ip22_on_absorption_voltage_write"))

            # /Settings/FloatVoltage — writable via GATT 0xEDF6
            # (with automatic 0xEDF1=USER guard).  Persisted.
            s.add_path(
                "/Settings/FloatVoltage", None,
                writeable=True,
                onchangecallback=_bind("_ip22_on_float_voltage_write"))

        # All paths created — pull any persisted /Settings/Devices/...
        # values back into the role-service paths so a Cerbo reboot
        # restores the user's configured values without a fresh GATT
        # round-trip.
        if hasattr(ble, "load_persisted_charger_settings"):
            try:
                ble.load_persisted_charger_settings(role_service)
            except Exception:
                # The role service is otherwise functional; missing
                # persisted settings should not stop service registration.
                import logging as _logging
                _logging.exception(
                    "ip22 charger: load_persisted_charger_settings failed")
