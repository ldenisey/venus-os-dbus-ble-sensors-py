# Dbus BLE sensors py

> **This is a fork of [ldenisey/venus-os-dbus-ble-sensors-py](https://github.com/ldenisey/venus-os-dbus-ble-sensors-py) with the following PRs merged in ahead of upstream:**
> - [#2 Replace Bleak with raw HCI monitor channel](https://github.com/ldenisey/venus-os-dbus-ble-sensors-py/pull/2) — passive BLE scanning via `AdvertisementMonitor1`, no scan contention
> - [#3 Add SeeLevel 709-BTP3/BTP7 support](https://github.com/ldenisey/venus-os-dbus-ble-sensors-py/pull/3) — tank, temperature, and battery sensors
> - [#4 Add alarm delay setting to BleRoleTank](https://github.com/ldenisey/venus-os-dbus-ble-sensors-py/pull/4)
> - [#6 Fix Mopeka tank level scaling](https://github.com/ldenisey/venus-os-dbus-ble-sensors-py/pull/6) — execution order, butane formula, role consolidation
> - [#7 Cache D-Bus connections](https://github.com/ldenisey/venus-os-dbus-ble-sensors-py/pull/7) — prevent connection proliferation
> - [#8 Add curl-based install method](https://github.com/ldenisey/venus-os-dbus-ble-sensors-py/pull/8) — install without opkg or remounting the filesystem
> - [#11 Add BLE advertisement router](https://github.com/ldenisey/venus-os-dbus-ble-sensors-py/pull/11) — let external services receive routed BLE advertisements via D-Bus signals without standing up their own scanner; replaces the standalone `dbus-ble-advertisements` project

Venus OS dbus service for BLE device support. Replaces and extends [official dbus ble service](https://github.com/victronenergy/dbus-ble-sensors/tree/master) which does not allow collaboration for new devices support.

Devices currently supported :
| Brand          | model                      | Product page                                                                                                   |
| -------------- | -------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Teltonika      | EYE Sensor                 | https://www.teltonika-gps.com/products/accessories/sensors-beacons/eye-sensor-standard                         |
| Safiery        | Star Tank                  | https://safiery.com/product/tank-level-sensor-star-tank-phased-coherent-radar-battery/                         |
| Gobius         | Gobius C                   | https://gobiusc.com/fr/                                                                                        |
| Victron Energy | SolarSense 750             | https://www.victronenergy.com/upload/documents/Datasheet-SolarSense-750-EN.pdf                                 |
| Mopeka         | Mopeka Pro Check Universal | https://mopeka.com/consumer-solutions/#:~:text=Mopeka%20Pro%20Check%20Universal%20%E2%80%93%20Latest%20Version |
| Mopeka         | Mopeka Pro Check H2O       | https://mopeka.com/commercial-industry-based-solutions/water/#:~:text=Mopeka%20Pro%20Check                     |
| Mopeka         | Mopeka Pro Check LPG       | https://mopeka.com/consumer-solutions/#:~:text=Mopeka%20Pro%20Check,-Ideal%20for%20Residential                 |
| Mopeka         | Mopeka Pro 200             | https://mopeka.com/consumer-solutions/#:~:text=Mopeka%20Pro200                                                 |
| Mopeka         | Mopeka Pro Plus            | https://mopeka.com/consumer-solutions/#:~:text=Mopeka%20Pro%20Plus                                             |
| Mopeka         | Mopeka TD40                | https://mopeka.com/consumer-solutions/#:~:text=Mopeka%20TD40                                                   |
| Mopeka         | Mopeka TD200               | https://mopeka.com/commercial-industry-based-solutions/water/#:~:text=Mopeka%20TD40,%20TD200                   |
| Mopeka         | Mopeka Tank Check (M1001)  | https://mopeka.com/                                                                                            |
| Ruuvi          | Ruuvi Tag                  | https://ruuvi.com/ruuvitag/                                                                                    |
| Ruuvi          | Ruuvi Air                  | https://ruuvi.com/air/                                                                                         |
| Garnet         | SeeLevel 709-BTP3          | https://www.garnetinstruments.com/document/709-btp3-seelevel-ii-tank-monitor-2/                                |
| Garnet         | SeeLevel 709-BTP7          | https://www.garnetinstruments.com/document/709-btp7-seelevel-ii-tank-monitor/                                  |
| Victron Energy | Orion-TR Smart DC-DC       | https://www.victronenergy.com/dc-dc-converters/orion-tr-smart-dc-dc-charger-isolated                           |
| Victron Energy | Blue Smart IP22 Charger    | https://www.victronenergy.com/chargers/blue-smart-ip22-charger                                                 |

The two Victron chargers (Orion-TR Smart, Blue Smart IP22) are
**fully integrated chargers** from `dbus-systemcalc-py`'s point of
view — they participate in DVCC (`/Link/{ChargeCurrent,
ChargeVoltage, NetworkMode, NetworkStatus, *Sense, BatteryCurrent}`),
publish charger-side `/Alarms/*`, accumulate
`/History/Cumulative/User/*`, and persist user-set
`/Settings/{ChargeCurrentLimit, AbsorptionVoltage, FloatVoltage}`
to `com.victronenergy.settings`.  A real Victron BMS controls them
the same way it controls a USB-attached Phoenix Smart IP43.

Implementation notes:

- [`docs/advertisement-router.md`](docs/advertisement-router.md) —
  consumer guide for service authors building **new** Bluetooth
  integrations on top of this service.  Subscribe to a D-Bus signal,
  declare interest by manufacturer / product / MAC, and skip writing
  any scanning code.  Drop-in replacement for the standalone
  [TechBlueprints/dbus-ble-advertisements](https://github.com/TechBlueprints/dbus-ble-advertisements)
  project.
- [`docs/IP22-INTEGRATION.md`](docs/IP22-INTEGRATION.md) — IP22
  driver, role, DVCC contract, alarm derivation, history accumulators
- [`docs/ORION-TR-INTEGRATION.md`](docs/ORION-TR-INTEGRATION.md) —
  Orion-TR driver, dcdc ↔ alternator role swap, integrated-charger
  surface on both roles
- [`docs/hci-tap-architecture.md`](docs/hci-tap-architecture.md) —
  the advertisement-processing pipeline, from raw HCI monitor frame to
  device-class delivery
- `tests/` — self-contained pytest suite covering the shared
  infrastructure (`ble_charger_common`) and per-driver dispatch.
  Run via `./tests/run.sh` — no D-Bus, BlueZ, or hardware needed.
- `scripts/probe_charger_vregs.py` — VREG-discovery tool for
  extending support to new SKUs or firmware versions.

## Installation

Run this one-liner on your Venus OS device (SSH as root):

``` bash
curl -fsSL https://raw.githubusercontent.com/TechBlueprints/venus-os-dbus-ble-sensors-py/main/install.sh | bash
```

This installs to `/data/apps/dbus-ble-sensors-py/`, which persists across firmware updates automatically.

To update, re-run the same command.

To disable:

``` bash
bash /data/apps/dbus-ble-sensors-py/disable.sh
```

To re-enable after disabling or a firmware update:

``` bash
bash /data/apps/dbus-ble-sensors-py/enable.sh
```

To fully remove:

``` bash
bash /data/apps/dbus-ble-sensors-py/disable.sh
rm -rf /data/apps/dbus-ble-sensors-py
```

## Usage

Device scan and enabling is done through the GUI, as described in the official documentations, i.e. [Cerbo GX bluetooth](https://www.victronenergy.com/media/pg/Cerbo_GX/en/connecting-supported-non-victron-products.html#UUID-8def9c4a-f36e-7048-1b4f-7294538eb31b).  
In short devices can be enabled/disabled in *Settings* -> *Integrations* -> *Bluetooth Sensors* and configured in *Settings* -> *Devices* dedicated menu.

> [!NOTE]  
> Even though the configuration process is the same, the configuration themselves are NOT shared between this service and official ble service
> hence configuration will have to be reset when switching between the two.

## Development

For technical info and guide to add new devices, see [dedicated developer page](DEVELOPMENT.md).

## Vendored dependencies

### victron-ble (Unlicense)

This fork bundles a copy of [keshavdv/victron-ble](https://github.com/keshavdv/victron-ble) (version 0.9.3) under `src/opt/victronenergy/dbus-ble-sensors-py/ext/victron_ble/`.  It is used by the IP22 and Orion-TR drivers to decrypt Victron Instant Readout advertisements.

`victron-ble` is released into the public domain under [The Unlicense](https://unlicense.org/) — credit goes to Keshav Varma and contributors.

The vendored copy carries one local change in `devices/base.py`: AES-CTR decryption can fall back to Python's standard `cryptography` library (which ships with Venus OS as `python3-cryptography`) when `PyCryptodome` is not available.  The two code paths produce byte-identical output and a unit test in `tests/test_vendored_victron_ble.py` enforces this.  See `ext/victron_ble/VENDORED.md` for details.

### victron-bluetooth-safety (Apache 2.0)

This fork also bundles [TechBlueprints/victron-bluetooth-safety](https://github.com/TechBlueprints/victron-bluetooth-safety) under `src/opt/victronenergy/dbus-ble-sensors-py/ext/victron-bluetooth-safety/`.  `install.sh` deploys it to `/data/victron-bluetooth-safety/` and applies it during installation.

It patches Venus OS's `vesmart-server` to stop a hardcoded 60-second timer that disconnects **every** connected BLE device on **every** adapter — see [victronenergy/venus#1587](https://github.com/victronenergy/venus/issues/1587).  Without this patch, third-party BLE services (this one included) cannot maintain stable scans or connections on a Cerbo running `vesmart-server`.

The fix is applied entirely with `mount --bind` — the rootfs is **never modified**.  A `/data/rc.local` boot hook re-establishes the bind mount on every reboot, including after Venus OS firmware updates, by re-deriving a patched copy from whatever upstream `gattserver.py` is currently shipping.  This follows the [Venus OS wiki guidance](https://github.com/victronenergy/venus/wiki/howto-add-a-driver-to-Venus#how-to-make-changes-that-dont-get-lost-on-a-firmware-update) for persistent customizations.

`install.sh` runs the bundled installer in `--mode patch` (preserves VictronConnect over BLE).  An alternative `--mode disable` is documented in `ext/victron-bluetooth-safety/VENDORED.md` for setups that don't need VictronConnect.

``` bash
sh /data/victron-bluetooth-safety/victron-bluetooth-safety.sh status
sh /data/victron-bluetooth-safety/victron-bluetooth-safety.sh uninstall
```

Uninstall is a single `umount` plus removal of the `rc.local` block — there is no patched file to revert.  See `ext/victron-bluetooth-safety/VENDORED.md` for the source SHA and update procedure.
