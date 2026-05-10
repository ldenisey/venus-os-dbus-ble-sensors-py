# Blue Smart IP22 Charger BLE Integration

Status: **integrated-charger pass landed on `feature/ip22-smart-charger`.**
The IP22 is published as a standard `com.victronenergy.charger.*` D-Bus
service that participates in the DVCC contract — `dbus-systemcalc-py`
treats it the same as a USB-connected VE.Direct IP43 reference unit.

## External-control mode (BMS-driven systems)

When a BMS is in the system and DVCC is dictating setpoints, the
gui-v2 and `dbus-systemcalc-py` contract is for the charger to
publish `/State = 252` (`OperationMode.EXTERNAL_CONTROL`).  The IP22
firmware itself doesn't know it's externally controlled — from its
point of view we're just bumping `0xEDF0` (max current) and `0xEDF7`
(absorption voltage) — so the override has to happen on the publish
side.

Implementation:

- `_dvcc_engaged` flips to `True` whenever any of the following
  arrives on D-Bus: `/Link/NetworkMode != 0`, `/Settings/BmsPresent ==
  1`, `/Link/ChargeCurrent`, `/Link/ChargeVoltage`.
- `_set_dvcc_engaged()` flips `/Link/NetworkStatus` between `4`
  (stand-alone) and `1` (active) and **immediately re-derives**
  `/State` using the last advertised state — no waiting for the next
  telemetry adv (which on this device can be ~20 s away).
- `_derive_published_state(advertised)` returns `252` when DVCC is
  engaged and the device is powered (advertised state != 0); off
  stays off regardless.
- The internal history accumulators (`OperationTime`, `ChargedAh`)
  are still ticked from the **real** advertised state, not the
  overridden value, so they keep counting correctly while externally
  controlled.

End-to-end behaviour from gui-v2's point of view: a BMS arrival
flips `/Settings/BmsPresent` to 1, `/Link/NetworkStatus` to 1, and
`/State` to 252 within one D-Bus round-trip.  DVCC then writes
`/Link/ChargeVoltage` and `/Link/ChargeCurrent`, which the queued
GATT writer pushes onto `0xEDF7` / `0xEDF0` (with the one-shot
`0xEDF1=USER` guard for `0xEDF7`).  When the BMS releases control,
`/State` falls back to the advertised value automatically.

### How a BMS actually stops charging (and why this works on the IP22)

In real Victron deployments the primary DVCC lever is
**`/Link/ChargeVoltage`**, not `/Link/ChargeCurrent`.  When a BMS
needs to stop charging — full cell, temperature out of range, fault,
disconnect signal — it lowers `ChargeVoltage` to at or below the
battery's resting voltage.  The charger sees its target is already
met and tapers off naturally.  `ChargeCurrent` is the maximum-allowed
envelope; the BMS treats it as a ceiling, not as a stop control.

This works **completely on the IP22** through our existing wiring:

- `/Link/ChargeVoltage` → VREG `0xEDF7` accepts any value the IP22
  can resolve (10 mV resolution).  Drop the target to e.g. 12.5 V on
  a 12 V system and the IP22 stops actively pushing energy in within
  one charge-cycle tick.  Restore it to 14.4 V and charging resumes.
- The 0.05 V deadband won't get in the way of "stop" transitions —
  every realistic BMS voltage change (full → idle, idle → resume)
  is well above 50 mV.
- The IP22's lack of a dedicated remote-on/off VREG (`0x0200` /
  `0x0202` are not implemented on this firmware) is therefore not a
  practical limitation — the standard BMS off-mechanism doesn't need
  one.  Because the firmware can't be remotely switched, the role
  also intentionally omits `/Mode` from the D-Bus surface; gui-v2's
  `PageAcCharger.qml` ListSwitch is gated on `dataItem.valid`, so the
  Switch row disappears cleanly when the path is absent.

`ChargeCurrent` clamping (the IP22 won't accept a max-current below
~7.5 A on the 12/30 SKU) is therefore a non-issue for BMS control:
the BMS stops the charger via voltage, not by trying to set
`ChargeCurrent = 0`.  We still pass the BMS-supplied current envelope
straight through to `0xEDF0` so the charger respects the BMS's max
when it *is* charging.

## Charger alarms

`/ErrorCode` carries the raw `victron_ble.ChargerError` enum value as
the source of truth.  On every `_publish` / `_publish_off_state` call,
`_publish_alarms()` translates that code into the charger-side
`/Alarms/*` paths (severity 0=ok, 1=warning, 2=alarm) per the
`_CHARGER_ERROR_TO_ALARMS` map:

| ChargerError | Alarm path | Severity |
|---|---|---|
| 2 VOLTAGE_HIGH | `/Alarms/HighVoltage` | 2 |
| 11 HIGH_RIPPLE | `/Alarms/HighRipple` | 2 |
| 17 / 22 / 23 / 26 (charger / internal-temp / overheated) | `/Alarms/HighTemperature` | 2 |
| 24 FAN | `/Alarms/Fan` | 2 |

The off-state path clears all four paths to 0 so a stale alarm doesn't
linger after the unit is switched off.

**Intentionally not published:**

- `/Alarms/HighBatteryTemperature`, `/Alarms/LowBatteryTemperature` —
  these are *battery-monitor / BMS* paths (the thing that owns battery
  state).  When the charger sees a battery-temperature error
  (`ChargerError 1` or `14`) it suspends charging and surfaces that
  through `/State` and `/ErrorCode`; gui-v2's
  `ChargerError::getDescription()` already turns those codes into
  "Battery temperature too high / too low" text in the alarms-and-errors
  panel.  The charger isn't the authority on battery state, so it
  shouldn't republish a battery-state condition as a charger-side
  alarm.
- `/Alarms/LowVoltage`, `/Alarms/LowSoc`, `/Alarms/Overload`,
  `/Alarms/Ripple`, `/Alarms/LoadDisconnect`,
  `/Alarms/VecanDisconnected` — battery-monitor / VE.Bus / inverter
  alarms; not properties of an AC charger.

Other charger errors without a dedicated alarm path on the charger
contract (`18` over-current, `20` bulk-time, `21` current-sensor, `27`
short-circuit, `28` converter-issue) remain visible via `/ErrorCode`
and gui-v2's description table.

## Identity, settings, and history

- `/Serial` is populated lazily from the BlueZ-advertised name on first
  publish (the encrypted advertisement payload doesn't carry a serial).
  The `_SERIAL_TOKEN_RE` matches the standard Victron `HQ` prefix.
- `/Settings/BatteryVoltage` is fixed per product id (`12 / 24 / 36 / 48 V`)
  by parsing the `_IP22_PRODUCT_NAMES` model-spec string (`"... 12|30 (1)"`
  → 12).
- `/Settings/{ChargeCurrentLimit,AbsorptionVoltage,FloatVoltage}` are
  writable on D-Bus and persisted to `com.victronenergy.settings` under
  `/Settings/Devices/ip22_<mac>/{ChargeCurrentLimit,AbsorptionVoltage,FloatVoltage}`.
  The role calls `BleDeviceIP22Charger.load_persisted_charger_settings()`
  after `add_path()` so a Cerbo reboot restores the last-known values
  onto the role-service paths without a fresh GATT round-trip (the
  device retains its own copy across power cycles).
- `/History/Cumulative/User/{OperationTime,ChargedAh}` are local
  accumulators ticked from `_tick_history()` on every `_publish` /
  `_publish_off_state` call.  OperationTime ticks while `/State` is in
  `{3,4,5,6,7,247}` (bulk/abs/float/storage/eq/recond); ChargedAh
  integrates positive `Dc/0/Current` over time.  Gaps longer than 600 s
  (e.g. service restart) are discarded so we don't credit phantom
  charging.  Values flush to settings every `_HISTORY_FLUSH_INTERVAL_S`
  (60 s) so we don't beat up flash on the 1 Hz adv interval.

## DVCC contract (what makes this an integrated charger, not just a sensor)

`dbus-systemcalc-py` writes onto the following paths to drive a charger;
the role service exposes all of them and the IP22 BLE driver wires the
two that map to writable VREGs through a per-device GATT write queue:

| D-Bus path | Direction | IP22 backing |
|---|---|---|
| `/Link/NetworkStatus` | read | published; flips between `4` (stand-alone) and `1` (DVCC active) when `/Link/NetworkMode != 0`, `/Settings/BmsPresent == 1`, or any `/Link/ChargeCurrent`/`/Link/ChargeVoltage` write arrives |
| `/State` | read | published; while DVCC is active and the device is on, **overridden to `252` (`OperationMode.EXTERNAL_CONTROL`)** so gui-v2 / dbus-systemcalc-py see "externally controlled" instead of bulk/abs/float — see "External-control mode" below |
| `/Link/NetworkMode` | write | stored only — IP22 has no consumer VREG |
| `/Link/ChargeCurrent` | write | GATT → VREG `0xEDF0` (u16 LE, 0.1 A); 0.1 A deadband |
| `/Link/ChargeVoltage` | write | GATT → VREG `0xEDF7` (u16 LE, 0.01 V); 0.05 V deadband |
| `/Link/TemperatureSense` | write | stored only |
| `/Link/VoltageSense` | write | stored only |
| `/Link/BatteryCurrent` | write | stored only |
| `/Settings/BmsPresent` | write | stored only |
| `/Settings/ChargeCurrentLimit` | write | GATT → VREG `0xEDF0` (user-set cap; same VREG as the DVCC override) |

`/Mode` is intentionally **not** published on the role surface.  The
IP22 firmware doesn't expose a writable on/off VREG, so a `/Mode` path
would be a decoration that lies to the user — gui-v2 wires its Switch
widget straight to `/Mode` and a write would never reach hardware.
Omitting the path entirely makes the gui-v2 Switch row disappear
cleanly (it gates on `dataItem.valid`).  See "On/off mechanism" below
for the full reasoning.

Voltage writes on the IP22 require `VREG 0xEDF1` (battery type) to be
`0xFF` (USER); otherwise `0xEDF7` rejects with ack code `02`.  The
driver flips that sentinel transparently on the first `/Link/ChargeVoltage`
write and caches the success so subsequent voltage updates skip the
extra round-trip.

The `_pending_writes` slot map in `BleDeviceIP22Charger` collapses
DVCC's once-per-cycle re-publish into a single per-VREG outstanding
write, then drains them serially through the shared single-slot
`AsyncGATTWriter`.  Steady-state DVCC traffic where every cycle pushes
the same setpoint produces zero GATT round-trips after the initial
push (verified live: three consecutive `/Link/ChargeCurrent=22.5` +
`/Link/ChargeVoltage=14.4` SetValue calls produced no GATT writes; a
follow-up `24.0 / 14.6` produced exactly two writes: `0xEDF0=f000` and
`0xEDF7=b405`, with the cached USER battery type skipping `0xEDF1`).

## Device model

| Parameter | Value |
|---|---|
| Manufacturer ID | `0x02E1` |
| Product IDs | `0xA330`–`0xA33F` (Blue Smart IP22 charger family) |
| Advertisement mode byte | `0x08` (AcCharger, per `victron_ble`) |
| GATT pairing | SMP passkey-entry bonding, default PIN `014916` |
| GATT service | `306b0001-b081-4037-83dc-e59fcc3cdfd0` (shared with Orion-TR) |

At power-off the IP22 drops its encrypted advertisement payload and only
broadcasts the 4-byte product-id prefix.  The driver treats that as a
synthetic "state = off" frame so the service tracks the on/off toggle
without gapping.

## Advertisement payload

The stock `victron_ble.devices.AcCharger` decoder parses IP22 payloads
without modification.  Fields surfaced today:

| GUI path | Source | Notes |
|---|---|---|
| `/State` | `charge_state` | Off / Bulk / Absorption / Float / Power Supply |
| `/ErrorCode` | `charger_error` | Victron charger-error enum |
| `/Dc/0/Voltage` | `output_voltage1` | Primary output volts |
| `/Dc/0/Current` | `output_current1` | Primary output amps |
| `/Dc/0/Power` | `v1 * i1` | Computed locally |
| `/Dc/1/{Voltage,Current}` | `output_{voltage,current}2` | Multi-output SKUs |
| `/Dc/2/{Voltage,Current}` | `output_{voltage,current}3` | Multi-output SKUs |
| `/Dc/0/Temperature` | `temperature` | Not populated by 12|30 SKU tested |
| `/Ac/In/L1/I` | `ac_current` | Not populated by 12|30 SKU tested |

## GATT control path

GATT writes from this driver land on the charge-profile VREGs only —
absorption voltage (`0xEDF7`), float voltage (`0xEDF6`), battery type
(`0xEDF1`), and battery max current (`0xEDF0`).  All of them go through
the per-device write queue in `ChargerCommonMixin._enqueue_write`,
which pauses the passive scan loop, opens a GATT session, drains the
slot, and resumes scanning.

There is no `/Mode` write path — the firmware probed (`fw 0.162` on
the bench unit) does not implement `0x0200` (`DEVICE_MODE`) or any
other writable on/off register, so the role intentionally omits
`/Mode`.  See "On/off mechanism" below.

## Key provisioning

The CLI at `orion_tr_key_cli.py` handles the first-time SMP bond + PUK/
PIN auth + VREG `0xEC65` read for any device that exposes the standard
Victron `306b`/`9758` services, so it is reused verbatim.  Keys land in a
dedicated settings namespace (`/Settings/Devices/ip22_<mac>/`) via
`ip22_key_settings.py` to keep the Orion-TR and IP22 trees separate.

Confirmed working: `ED:47:4D:2A:7C:2A` (HQ2133XMU6Y) bonded on the
second Pair() attempt with passkey `014916` (the default cerbo inbound
PIN) and returned a valid advertisement key.

## Known gaps / future work

- **No remote on/off VREG on this firmware.**  Direct GATT probes
  against the bench unit (firmware `0.162`) covered every plausible
  register:

  | VREG | Read | Write | Notes |
  |---|---|---|---|
  | `0x0100` | ✓ | n/a | Product id `0x00FFA330` (BSC IP22 12/30) |
  | `0x0102` | ✓ | n/a | App version `0x365FF` ≈ fw 3.65 |
  | `0x010A` | ✓ | n/a | Serial (`HQ2133XMU6Y`) |
  | `0x010B` | ✓ | n/a | Model name |
  | `0x010C` | ✓ | n/a | Long name |
  | `0x0140` | ✓ | n/a | Capabilities bitmask `0x40C100FC` |
  | `0x0200` (DEVICE_MODE) | ✗ code 1 | ✗ code 1 | **Not implemented** — the Orion-TR uses this; IP22 firmware has no equivalent |
  | `0x0201` | ✓ | ✗ code 3 | Device State — read-only |
  | `0x0202` | ✗ code 1 | ✗ code 1 | **Not implemented** (BlueSolar remote-control mask) |
  | `0x0207` (DeviceOffReason) | ✓ | ✗ code 3 | Read-only on this firmware |
  | `0xEDF0` | ✓ | ✓ (clamps) | **Battery max current**, 0.1 A; writes ≥ ~7.5 A take, lower values clamp to the firmware minimum |
  | `0xEDF1` | ✓ | ✓ | Battery type; `0xFF` = USER (gates voltage writes) |
  | `0xEDF6` | ✓ | ✓ when `EDF1=USER` | Float voltage, 0.01 V |
  | `0xEDF7` | ✓ | ✓ when `EDF1=USER` | Absorption voltage, 0.01 V |
  | `0xEDFC` | ✓ | ✓ | Bulk time limit |
  | `0xEDFE` | ✓ | ✗ code 3 | Adaptive mode — read-only |

  Range scans across `0x0000`-`0x02FF`, `0x0E00`-`0x0FFF`,
  `0xEC00`-`0xECFF`, and `0xEDA0`-`0xEDFF` surfaced no other writable
  on/off candidate.  CBOR-layer error codes observed are `1` =
  unknown register, `2` = bad value / size, `3` = read-only.

  Both the [pvtex/Victron_BlueSmart_IP22](https://github.com/pvtex/Victron_BlueSmart_IP22)
  and [wasn-eu/Victron_BlueSmart_IP22](https://github.com/wasn-eu/Victron_BlueSmart_IP22)
  open-source drivers reach the same conclusion: the only practical
  BLE control over an IP22 is the charge-current limit (`0xEDF0`).
  This driver therefore omits `/Mode` from the role surface (so
  gui-v2 doesn't draw a Switch widget that wouldn't reach hardware)
  and relies on `/Link/ChargeVoltage` → `0xEDF7` for BMS-style stop
  control — see "On/off mechanism" above for why voltage drop is the
  real off-mechanism on Victron-style chargers.

- **Charger vs Power Supply mode toggle.**  On VE.Direct IP43
  chargers this is only exposed through the vendor's mobile app, not
  over the D-Bus service, so no standard path exists.  A VREG
  enumeration pass may surface one — pending exploration.
- **Charge-setpoint writes.** `/Settings/ChargeCurrentLimit` is now
  wired through `BleDeviceIP22Charger._ip22_on_charge_current_limit_write`
  → VREG `0xEDF0` (commit `aa7c137`).  Setting it to a value at or below
  the firmware's hardware minimum (~7.5 A) clamps to that minimum rather
  than turning the unit off — see "On/off mechanism" below.
  `/Link/ChargeVoltage` / `/Link/ChargeCurrent` are still declared on the
  role service but not yet wired; deferred until DVCC pulls actually
  arrive against this driver.
- **Short-frame "off" override.** Some IP22 firmwares interleave the
  4-byte product-id beacon with the encrypted telemetry advertisement as
  a power-saving rotation even while the charger is running.  An older
  version of `handle_manufacturer_data` interpreted any short frame as a
  hard "off" snapshot, which constantly clobbered live telemetry.  The
  driver now keeps a `_last_full_telemetry_at` timestamp and only honours
  the short frame as off-state once the IP22 has gone quiet for
  `_OFF_FRAME_GRACE_S` (30 s).
- **On/off mechanism (final answer).**  The IP22 firmware on the bench
  unit (3.65, advertised as `0.162`) does not implement `0x0200`
  (`DEVICE_MODE`) or `0x0202` (BlueSolar remote-control mask).  `0x0207`
  (`DeviceOffReason`) is read-only.  No alternative writable on/off
  VREG was found over multiple range probes (`0x0000`-`0x02FF`,
  `0x0E00`-`0x0FFF`, `0xEC00`-`0xECFF`, `0xEDA0`-`0xEDFF`,
  `0x0140`-`0x017F`).  Both the
  [pvtex](https://github.com/pvtex/Victron_BlueSmart_IP22) and
  [wasn-eu](https://github.com/wasn-eu/Victron_BlueSmart_IP22) reference
  drivers come to the same conclusion: the only practical control over
  IP22 BLE is the charge-current limit (`0xEDF0`), which is what this
  driver exposes via `/Settings/ChargeCurrentLimit`.  `/Mode` is
  intentionally absent from the role surface so gui-v2's PageAcCharger
  Switch widget (gated on `dataItem.valid`) hides cleanly instead of
  rendering a toggle that doesn't reach hardware.  BMS-style stop
  control still works through `/Link/ChargeVoltage` → `0xEDF7` — see
  the "On/off mechanism" section earlier.
- **Marginal-RSSI pairing.** The second IP22 on the bench (F2:86, RSSI
  -80 dBm) consistently fails Pair() with `AuthenticationCanceled`.
  Moving it closer to the cerbo or using a USB BLE adapter with a
  better antenna is the workaround; no driver change needed.

- **HCI-tap pipeline drops post-init advs on the bench unit (ED:47).**
  After service start, ED:47 surfaces the very first short-beacon adv
  through `_process_advertisement` and then goes silent from the
  driver's point of view, even though `btmon` confirms ~165 short
  beacons + ~3 full-telemetry advs per minute are still leaving the
  device.  The other IP22 on the bench (F2:86) gets full telemetry
  through cleanly, so the integration pipeline itself is healthy —
  the suppression is specific to ED:47 and points at either
  `AsyncTap`'s `last_mfg_data` dedup or BlueZ's AdvMonitor
  PropertiesChanged firing pattern for that particular MAC.  This
  hits *every* /Dc/0/* and /State path, not just alarms; treating it
  as a transport-layer task in a follow-up rather than gating the
  integrated-charger work on it.

  **Diagnostic:** set `IP22_ADV_TRACE=1` in the service's run script
  (e.g. `start-dbus-ble-sensors-py.sh`) and the IP22 driver emits a
  log line every 30 s with the count of advertisement arrivals
  bucketed by manufacturer-data length:

  ```
  INFO:ble_device_ip22_charger:ed474d2a7c2a - BSC IP22 12/30...HQ2133XMU6Y::
       adv-trace counts (last 30 s+) {4: 1}
  ```

  If the count for the suppressed unit stays at `{4: 1}` over
  multiple windows while `btmon` shows steady traffic, the issue is
  in the BlueZ → service hop (AdvMonitor dedup or PropertiesChanged
  filtering); if it grows but length-21 advs never appear, the HCI
  tap parser is dropping them.  Either localises the bug.

  **Status as of 2026-04-27 deep-dive:** the `_on_advertisement`
  dedup at `dbus_ble_sensors.py:347-364` keys on MAC alone and stores
  the last forwarded raw bytes for that MAC.  Identical bytes within
  `DEDUP_KEEPALIVE_SECONDS` (900 s) are dropped — correct behaviour
  for repeated short beacons (which are byte-identical) but also the
  reason only the *first* short-beacon reaches the driver after
  service start.  Encrypted telemetry advs have a rotating counter so
  every byte is unique, so the dedup does not suppress them — if
  they're not arriving, the issue is upstream (HCI tap parser or
  BlueZ).  The HCI tap parser at `hci_advertisement_tap.py:248-249`
  drops extended-adv reports with `data_status != 0` (chained or
  truncated payloads); this only matters for chained extended advs
  which Victron BLE chargers don't appear to use — legacy advs
  delivered via the extended-report mechanism carry `data_status =
  0` regardless.

  Re-confirming the bug requires the bench unit to be actively
  charging (full-telemetry advs leaving the device).  In the current
  bench state ED:47 emits only short beacons — the dedup correctly
  forwards one off-state snapshot and suppresses the rest.

- **Optional charge-profile settings not yet wired.**
  `/Settings/{EqualizationVoltage, EqualizationDuration,
  AbsorptionMaxTime, BulkMaxTime, RebulkVoltage}` are reachable on
  Solar-charger-class layouts but their existence on AC-charger
  firmware needs verification first — an unknown-VREG write costs a
  full pause-scan / connect / disconnect cycle.  Run
  `scripts/probe_charger_vregs.py --mac <ip22> --candidates ip22-optional`
  to confirm which addresses respond before extending the role.

- **`/History/Cumulative/User/ChargedAh` cap on Orion-TR.**  IP22's
  encrypted advertisement carries `output_current1`, so ChargedAh
  ticks correctly there.  The Orion-TR's `DcDcConverterData` doesn't
  expose current at all, so its history accumulator sees
  `current_a=None` and only OperationTime advances.  Closing this
  gap requires GATT-polling a current register on a slow loop — see
  `sample-driver/research/ORION-TR-INTEGRATED-CHARGER.md` §3 for
  the design discussion.
