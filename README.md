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

If your are satisfied and want to make it persistent to firmware upgrades, you can [install mod-persist](./VenusOS-Mod_persist.md.md#how-to-install-it) then :
``` bash
persist-opkg install dbus-ble-sensors-py
```


