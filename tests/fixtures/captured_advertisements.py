"""
Real BLE advertisement payloads captured from our bench devices.

Each entry is the **manufacturer-data** portion (everything after the
`0xFF` AD-type marker and the 2-byte manufacturer ID `0x02E1`),
hex-encoded.  Capture method: ``btmon`` while the service was running
on dev-cerbo, then ``awk`` filtered by MAC and ``AD Data:`` line.

Source files / commits:

  - IP22 short beacon (off-state-equivalent frame)         btmon_orion.log
  - IP22 full telemetry adv (BSC IP22 12/30, ed47:4d:2a:7c:2a)
                                                          btmon_long.log
  - IP22 full telemetry adv (BSC IP22 12/30, f286:c3:32:4c:d2)
                                                          btmon4.log
  - Orion-TR Smart 12V/24V-15A (HQ20326VVVJ, ff13:42:2b:7a:4b)
                                                          btmon_orion.log

The encryption key is per-device.  These fixtures cover the
*structural* path (length-based dispatch, manufacturer-id filter,
product-id range checks) without needing the cleartext payload.

Tests that want to exercise the full ``victron_ble`` decode chain
should provide their own (manufacturer_data, encryption_key) pair —
the bench keys are not committed here for obvious reasons.
"""

# ---------------------------------------------------------------------------
# IP22 — 4-byte "short beacon" (no encrypted payload)
# ---------------------------------------------------------------------------
# Layout: 10 00 30 a3
#  ^^ ^^  - record-type marker (0x10 0x00)
#        ^^ ^^  - product-id 0xA330 little-endian
IP22_SHORT_BEACON_HEX = "100030a3"

# ---------------------------------------------------------------------------
# IP22 — full encrypted telemetry advertisement
# ---------------------------------------------------------------------------
# Layout: 10 00 30 a3 08 <encrypted blob>
#                    ^^ - record-type 0x08 (AcCharger)
# 21-byte payload total: 5-byte header + 16-byte encrypted body
IP22_FULL_TELEMETRY_HEX_SAMPLES = [
    # captured from f286c3324cd2 over a 25 s window
    "100030a308d3bb4cda2a2bb26f26706f6f1bc8a3260f00",
    "100030a3087fc04ca0bfd43b51e788dfd9269ad8cc",
    "100030a308a9474c638128ad95690f7f434153d69f",
    "100030a30881474c3b2b5fce7f94817c8fbc5e3310",
    "100030a308aa474c707326fc5b558d0885e7d80a76",
]

# ---------------------------------------------------------------------------
# Orion-TR Smart — full encrypted telemetry advertisement
# ---------------------------------------------------------------------------
# Layout: 10 00 c9 a3 04 <encrypted blob>
#                    ^^ - record-type 0x04 (DcDcConverter)
#         ^^ ^^  - product-id 0xA3C9 little-endian (Orion Smart 12V/24V-15A)
ORION_TR_FULL_TELEMETRY_HEX_SAMPLES = [
    # captured from ff13422b7a4b
    "1000c9a30491eea90710f7ac25557b908586",
    "1000c9a3043ef3a914d7e6ad2ba85472d483",
    "1000c9a3040ef3a96cce81b2055a4de23d13",
    "1000c9a304ddf3a9f4c965e6eee67a87694f",
    "1000c9a304b9f1a9cc0ecae3ac951f2b1af4",
]

# ---------------------------------------------------------------------------
# BlueZ-advertised device names — exact strings observed via
# org.bluez.Device1.Name on our bench.  Used to test the
# /Serial extraction regex against real-world variants.
# ---------------------------------------------------------------------------
BLUEZ_NAMES = {
    "ip22_long":  "BSC IP22 12/30...HQ2133XMU6Y",
    "ip22_short": "Blue Smart BL HQ2133XMU6Y",
    "ip22_other": "BSC IP22 12/30...HQ2133CG4QA",
    "orion_tr":   "Orion Smart HQ20326VVVJ",
    "no_serial":  "Phoenix Smart IP43 Charger 24|16 (1+1) 120-240V",
}

# ---------------------------------------------------------------------------
# IP22 product-id table excerpt — names format `"... Charger {V}|{A} ..."`.
# Used by /Settings/BatteryVoltage derivation tests.
# ---------------------------------------------------------------------------
IP22_PRODUCT_NAMES = {
    0xA330: "Blue Smart IP22 Charger 12|30 (1)",
    0xA331: "Blue Smart IP22 Charger 12|30 (3)",
    0xA332: "Blue Smart IP22 Charger 24|16 (1)",
    0xA337: "Blue Smart IP22 Charger 24|8 (1)",
    0xA33B: "Blue Smart IP22 Charger 12|10 (1)",
    # 36 V and 48 V Phoenix-Smart-style examples (not IP22 themselves but
    # the same naming convention)
    0xA340: "Phoenix Smart IP43 Charger 36|15 (1) 120-240V",
    0xA341: "Phoenix Smart IP43 Charger 48|13 (1) 120-240V",
}

# ---------------------------------------------------------------------------
# Orion-TR product-id table excerpt — different naming convention
# (``"Orion Smart {Vin}V/{Vout}V-{A}A DC-DC Converter"``).  Used to
# test the per-product battery-voltage derivation.
# ---------------------------------------------------------------------------
ORION_TR_PRODUCT_NAMES = {
    0xA3C0: "Orion-TR Smart 12/12-18A",
    0xA3C1: "Orion-TR Smart 12/24-10A",
    0xA3C2: "Orion-TR Smart 12/48-6A",
    0xA3C9: "Orion Smart 12V/24V-15A DC-DC Converter",
    0xA3D0: "Orion-TR Smart 24/12-20A",
    0xA3D5: "Orion-TR Smart 48/24-12A",
    0xA3D6: "Orion-TR Smart 48/48-6A",
}

# ---------------------------------------------------------------------------
# Captured GATT-side responses (CBOR-framed, hex-encoded).  Each entry
# is the byte stream observed on DATA_LAST after a probe write.
# ---------------------------------------------------------------------------
GATT_PROBE_RESPONSES = {
    # Push response: opcode 0x08, seq 0x00, vreg-marker 0x19, 16-bit
    # vreg id, then a CBOR value.
    "ip22_state_absorption": "08001902014104",     # 0x0201 = u8 4 (ABSORPTION)
    "ip22_edf0_max_current": "08001905edf04200b400",   # bstr 2 bytes 0x00b4 = 18.0 A
    "orion_edf6_float":      "080019edf6428c0a",   # bstr 2 bytes 27.00 V
    "orion_edf7_absorption": "080019edf742180b",   # bstr 2 bytes 28.40 V
    "orion_edf1_user":       "080019edf141ff",     # bstr 1 byte 0xFF (USER)
    # Error response: opcode 0x09, seq 0x00, marker 0x19, vreg, code byte
    "err_unknown":     "0900190200200001",   # code 1 (unknown register) for 0x0200
    "err_param":       "0900190200ed00f602", # code 2 (param error)
    "err_readonly":    "09001902010103",     # code 3 (read-only) for 0x0201
}
