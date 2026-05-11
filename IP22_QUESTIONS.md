# Queued Questions / Notes for Clint — IP22 Integration

Branch: `feature/ip22-smart-charger` (4 commits, local only — not pushed).

## Working (ready to review)

- `ED:47:4D:2A:7C:2A` (BSC IP22 12/30 HQ2133XMU6Y) is fully integrated:
  advertisement decode, GATT `/Mode` write toggles confirmed on the
  device (reg `0x0200`, values 1 and 4). Service registers as
  `com.victronenergy.charger.ip22_ed474d2a7c2a` and publishes
  `/Dc/0/Voltage=13.6`, `/Dc/0/Current=0.2`, `/State=11` (Power
  Supply) while the device is running.

## Open questions for you

1. **Charger-vs-PSU mode selection UX.** The IP43 reference exposes
   ED:47 might turn up an equivalent — do you want the IP22 driver to
   surface a writable `/Settings/PowerSupplyMode` switch, or should I
   either way; just scope.)

2. **Charge-setpoint parity with VE.Direct IP43.** IP43 accepts
   `/Link/ChargeVoltage`, `/Link/ChargeCurrent`,
   `/Settings/ChargeCurrentLimit` writes and participates in DVCC. The
   IP22 driver declares those paths read-only today. Happy to wire
   them through GATT (`VREG 0xED..` / `VREG 0xEC..` — names TBD via
   probe) if you want the IP22 to act as a DVCC-controlled slave.

## Known issue (not blocking)

- **F2:86 Pair() fails every attempt (AuthenticationCanceled).** RSSI
  holds at -80 dBm on `hci0`; `hci2` does not see the device at all.
  ED:47 sits at the same RSSI but bonded fine on its second attempt.
  Best guess: a packet loss during SMP passkey-entry on the marginal
  link. Either move the unit closer to dev-cerbo for a one-time
  bonding, or swap the `hci0` antenna — after that the adv key will
  persist and nothing else needs the paired link except one daily
  refresh in the 03:00–05:00 window.
