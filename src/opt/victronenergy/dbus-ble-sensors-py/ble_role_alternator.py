"""
Alternator role (Venus OS ``alternator`` service type).

Used for Victron Orion-TR Smart devices when they are operating in a
charger algorithm (bulk / absorption / float / storage).  In that state
the stock ``dbus-victron-orion-tr`` service publishes under
``com.victronenergy.alternator.*`` so the unit appears on the
*DC Sources* page (see ``gui-v2/pages/settings/devicelist/dc-in/
PageAlternator.qml``).  This mirrors that behaviour inside
``dbus-ble-sensors-py``.

When the device is off or running in fixed-output (PSU) mode it should
instead be published as a ``dcdc`` service — see :mod:`ble_role_dcdc`.
The parent device (``BleDeviceOrionTR``) is responsible for swapping
the role in response to state changes.

The DVCC / alarms / history / settings surface mirrors the IP22
charger role: any handler the BLE device class provides via
``ChargerCommonMixin`` is bound automatically; missing handlers
degrade to a "store on D-Bus, no GATT" passive callback (see
``_bind`` below).
"""
from ble_role import BleRole

class BleRoleAlternator(BleRole):
    NAME = "alternator"

    def __init__(self, config: dict = None):
        super().__init__()
        self.info.update(
            {
                "name": "alternator",
                "dev_instance": 130,
                "settings": [],
                "alarms": [],
            }
        )

    def init(self, role_service):
        svc = role_service._dbus_service
        ble = role_service._ble_device

        def _bind(handler_name: str):
            """Build an onchangecallback that forwards to a method on
            the BLE device class if it exists, otherwise treats the
            write as a passive store-only path (D-Bus updates, no
            GATT).  Same convention the IP22 charger role uses."""
            handler = getattr(ble, handler_name, None)
            if handler is None:
                return lambda _path, _value: True
            return lambda _path, value: bool(handler(role_service, value))

        with svc as s:
            # ----------------------------------------------------------
            # Telemetry — sourced from the encrypted advertisement
            # decoder in ble_device_orion_tr._publish.
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

            # /Mode — Orion-TR firmware *does* implement VREG 0x0200
            # (1 = ON, 4 = OFF), so this stays writable.  The IP22 had
            # to publish /Capabilities/HasNoDeviceOffMode = 1 because
            # its firmware doesn't support remote on/off; the Orion-TR
            # does, so we omit that capability flag here.
            def on_mode(path, value):
                return ble._orion_on_mode_write(role_service, int(value))
            s.add_path("/Mode", 1,
                       writeable=True, onchangecallback=on_mode)

            # /Serial — populated lazily on first telemetry tick from
            # the BlueZ-advertised name (encrypted payload doesn't
            # carry a serial).
            s.add_path("/Serial", None)

            # /Settings/BatteryVoltage — fixed nominal voltage if the
            # device class can derive one from product id; otherwise
            # stays None and gui-v2 falls back to its detection.
            s.add_path("/Settings/BatteryVoltage", None)

            # /History/Cumulative/User/OperationTime — ticks while the
            # device is in a charging state.  Backed by
            # com.victronenergy.settings.
            s.add_path("/History/Cumulative/User/OperationTime", 0)
            # /History/Cumulative/User/ChargedAh — initial *None*
            # because the Orion-TR's encrypted advertisement does not
            # carry output current.  ChargerCommonMixin._publish_history
            # only writes this path when it has seen real current data,
            # so the value stays None and gui-v2 / VRM render it as
            # "--" rather than misleadingly charting 0 Ah.
            s.add_path("/History/Cumulative/User/ChargedAh", None)

            # /Alarms/* — charger's own failure modes only.  Battery
            # state belongs on a battery-monitor / BMS, not here.
            # See _CHARGER_ERROR_TO_ALARMS in ble_charger_common for
            # the ChargerError -> path map.
            s.add_path("/Alarms/HighTemperature", 0)
            s.add_path("/Alarms/HighVoltage", 0)
            s.add_path("/Alarms/HighRipple", 0)
            s.add_path("/Alarms/Fan", 0)

            # ----------------------------------------------------------
            # DVCC contract — paths dbus-systemcalc-py writes onto a
            # charger.  Handlers come from ChargerCommonMixin (passive
            # ones) and ble_device_orion_tr (the GATT-bound writes).
            # ----------------------------------------------------------
            # /Link/NetworkStatus: 4 = stand-alone, 1 = DVCC active.
            s.add_path("/Link/NetworkStatus", 4)

            # /Link/NetworkMode: bitmask DVCC writes to indicate which
            # links are active.  Stored only — also flips
            # /Link/NetworkStatus to track engagement.
            s.add_path(
                "/Link/NetworkMode", 0,
                writeable=True,
                onchangecallback=_bind("_on_link_network_mode_write"))

            # /Link/ChargeCurrent: target current pushed by DVCC (A).
            # Wired to the Orion-TR's max-current VREG when known.
            s.add_path(
                "/Link/ChargeCurrent", None,
                writeable=True,
                onchangecallback=_bind("_orion_on_link_charge_current_write"))

            # /Link/ChargeVoltage: target absorption voltage (V).
            # Wired to the Orion-TR's absorption-voltage VREG.  This
            # is the primary off-mechanism a real Victron BMS uses —
            # drop the target below battery-resting voltage and the
            # charger naturally tapers off.
            s.add_path(
                "/Link/ChargeVoltage", None,
                writeable=True,
                onchangecallback=_bind("_orion_on_link_charge_voltage_write"))

            # /Link/{TemperatureSense,VoltageSense,BatteryCurrent}: BMS
            # sense values DVCC pushes for temperature- and
            # voltage-compensated charging.  Stored only unless the
            # device class wires them to a consumer VREG.
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
            # system.  Same NetworkStatus side-effect as NetworkMode.
            s.add_path(
                "/Settings/BmsPresent", 0,
                writeable=True,
                onchangecallback=_bind("_on_settings_bms_present_write"))

            # ----------------------------------------------------------
            # User-facing settings (writable, persisted to
            # com.victronenergy.settings).  Handler names match the IP22
            # ones the device class would expose if it implements them.
            # ----------------------------------------------------------
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

        # Restore persisted user-set values onto the role-service paths
        # so a Cerbo reboot doesn't drop the configured charge profile.
        # The mixin no-ops if no values are stored or the device class
        # doesn't inherit it.
        if hasattr(ble, "load_persisted_charger_settings"):
            try:
                ble.load_persisted_charger_settings(role_service)
            except Exception:
                import logging as _logging
                _logging.exception(
                    "orion-tr alternator: load_persisted_charger_settings failed")
