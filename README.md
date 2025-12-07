# Dbus BLE sensors py

Venus OS dbus service for BLE device support. Replaces and extends [official dbus ble service](https://github.com/victronenergy/dbus-ble-sensors/tree/master) which does not allow collaboration for new devices support.

Devices currently supported :
- [Teltonika Eye Sensor](https://www.teltonika-gps.com/products/accessories/sensors-beacons/eye-sensor-standard)
- [Safiery Star Tank](https://safiery.com/product/tank-level-sensor-star-tank-phased-coherent-radar-battery/)
- [Gobius C](https://gobiusc.com/fr/)
- [Victron SolarSense 750](https://www.victronenergy.com/upload/documents/Datasheet-SolarSense-750-EN.pdf)

## Installation

Add the [venus-os-configuration opkg feed](./VenusOS-Opkg_configuration.md#adding-custom-feed), then :
``` bash
opkg install dbus-ble-sensors-py
```

To make it persistent to Venus OS firmware upgrades, you can [install mod-persist](./VenusOS-Mod_persist.md.md#how-to-install-it) then :
``` bash
persist-opkg install dbus-ble-sensors-py
```

## Usage

This service replaces official dbus ble service while keeping its configuration process accessible in official documentation.  
Devices can be enable/disabled in *Settings* -> *Integrations* -> *Bluetooth Sensors*. Once enabled, they can be configured in
*Settings* -> *Devices* menu.

> [!NOTE]  
> Even though the configuration process is the same, the configuration themselves are NOT shared between this service and official ble service
> hence configuration of devices managed by both will have to be reapplied upon installation/desintallation.

## Development

See [dedicated developer page](DEVELOPMENT.md).
