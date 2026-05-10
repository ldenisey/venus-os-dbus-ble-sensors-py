#!/usr/bin/env python3
"""
Probe a Victron BLE charger / DC-DC for VREG implementation status.

Runs on a Cerbo (or any host with a paired Victron device on BlueZ),
talks the same CBOR-framed VE.Direct HEX protocol the driver uses, and
emits a report of which VREGs respond, with what value, and what kind
of write a 1-byte sentinel triggers (code 1 = unknown register, code
2 = parameter / size error → register exists, code 3 = read-only,
empty = write accepted).

Use it to:

  - Locate the Orion-TR's max-current VREG (gap #1)
  - Find the Orion-TR's Function (Charger / PSU) VREG (gap #4)
  - Confirm IP22 optional charge-profile VREGs before wiring writable
    settings paths (gap #9 — Equalize voltage/duration, AbsorptionMaxTime,
    BulkMaxTime, RebulkVoltage)

Usage:

  ./scripts/probe_charger_vregs.py --mac ED:47:4D:2A:7C:2A --range 0xEDD0-0xEDFF
  ./scripts/probe_charger_vregs.py --mac FF:13:42:2B:7A:4B --candidates current
  ./scripts/probe_charger_vregs.py --mac ED:47:4D:2A:7C:2A --candidates ip22-optional

The script needs the BLE adapter to itself, so stop ``dbus-ble-sensors-py``
first:

  svc -d /service/dbus-ble-sensors-py
  ./scripts/probe_charger_vregs.py …
  svc -u /service/dbus-ble-sensors-py
"""
from __future__ import annotations

import argparse
import binascii
import struct
import sys
import time

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

SVC_306B   = "306b0001-b081-4037-83dc-e59fcc3cdfd0"
CTRL_UUID  = "306b0002-b081-4037-83dc-e59fcc3cdfd0"
DLAST_UUID = "306b0003-b081-4037-83dc-e59fcc3cdfd0"
DBULK_UUID = "306b0004-b081-4037-83dc-e59fcc3cdfd0"
C6_UUID    = "97580006-ddf1-48be-b73e-182664615d8e"
C3_UUID    = "97580003-ddf1-48be-b73e-182664615d8e"

# --- Candidate VREG sets ----------------------------------------------------

CANDIDATES = {
    # Orion-TR / IP22 universals — already known
    "core": [
        0x0200, 0x0202, 0x0207, 0xEDF0, 0xEDF1, 0xEDF6, 0xEDF7,
    ],
    # Where to look for the Orion-TR's max-current VREG that IP22 puts
    # at 0xEDF0.  Sweeps the surrounding charge-profile region.
    "current": [
        0xEDB0, 0xEDB1, 0xEDB2, 0xEDB3, 0xEDC0, 0xEDC1, 0xEDC2,
        0xEDC3, 0xEDC4, 0xEDC5, 0xEDD0, 0xEDD2, 0xEDD4, 0xEDD6,
        0xEDD8, 0xEDDC, 0xEDDD, 0xEDE0, 0xEDE3, 0xEDE5, 0xEDE7,
        0xEDEA, 0xEDED, 0xEDEE, 0xEDF8, 0xEDF9, 0xEDFD, 0xEDFF,
        0x0270, 0x0271, 0x2003,
    ],
    # Where the Function (Charger / PSU) VREG might live.  Mode-style
    # VREGs commonly cluster in 0x02xx and the front-half of 0xEDxx.
    "function": [
        0x0203, 0x0204, 0x0205, 0x0206, 0x0208, 0x020A, 0x020B,
        0x020F, 0x0210, 0x0211, 0x0220, 0x0221,
        0xEDD3, 0xEDDF, 0xEDE6,
    ],
    # IP22 optional charge-profile registers — verify before wiring
    # writable settings paths.  Solar-charger-class layout suggests
    # these but it isn't guaranteed on AC-charger firmware.
    "ip22-optional": [
        0xEDF3, 0xEDF4, 0xEDF5, 0xEDFA, 0xEDFB, 0xEDFC, 0xEDFD,
        0xEDFE, 0xEDFF,
    ],
}

# --- BLE plumbing -----------------------------------------------------------

def _pump(ms: int, ctx: GLib.MainContext) -> None:
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.005)

def _open_session(mac: str, passkey: int):
    """Open a paired GATT session and run the CTRL handshake.  Returns
    (dev, dlast, all_data, pump) — the caller drives reads/writes via
    dlast and inspects accumulated responses in all_data."""
    bus = dbus.SystemBus()
    om = dbus.Interface(
        bus.get_object("org.bluez", "/"),
        "org.freedesktop.DBus.ObjectManager")
    ctx = GLib.MainContext.default()
    pump = lambda ms: _pump(ms, ctx)

    suf = "/dev_" + mac.replace(":", "_")
    for path in sorted(om.GetManagedObjects().keys()):
        path = str(path)
        if path.endswith(suf):
            try:
                dbus.Interface(bus.get_object("org.bluez", path),
                               "org.bluez.Device1").Disconnect()
            except dbus.DBusException:
                pass
    pump(1000)

    dev_path = None
    objs = om.GetManagedObjects()
    for path in sorted(objs.keys()):
        path = str(path)
        if path.endswith(suf) and "org.bluez.Device1" in objs[path]:
            dev_path = path
            break
    if dev_path is None:
        sys.exit(f"device {mac} not found in BlueZ — is it advertising?")

    dev = dbus.Interface(bus.get_object("org.bluez", dev_path),
                         "org.bluez.Device1")
    props = dbus.Interface(bus.get_object("org.bluez", dev_path),
                           "org.freedesktop.DBus.Properties")

    for _ in range(5):
        try:
            dev.Connect()
            break
        except dbus.DBusException:
            pump(1000)

    for _ in range(40):
        pump(300)
        if bool(props.Get("org.bluez.Device1", "ServicesResolved")):
            break
    pump(400)

    objs = om.GetManagedObjects()
    chars: dict[str, str] = {}
    for path, ifs in objs.items():
        path = str(path)
        if not path.startswith(dev_path):
            continue
        if "org.bluez.GattCharacteristic1" not in ifs:
            continue
        cp = ifs["org.bluez.GattCharacteristic1"]
        u = str(cp.get("UUID", ""))
        si = objs.get(str(cp.get("Service", "")), {}).get(
            "org.bluez.GattService1", {})
        if u.startswith("306b") and str(si.get("UUID", "")) == SVC_306B:
            chars[u] = path
        elif u.startswith("9758"):
            chars[u] = path

    ci6 = dbus.Interface(bus.get_object("org.bluez", chars[C6_UUID]),
                         "org.bluez.GattCharacteristic1")
    ci3 = dbus.Interface(bus.get_object("org.bluez", chars[C3_UUID]),
                         "org.bluez.GattCharacteristic1")
    ctrl = dbus.Interface(bus.get_object("org.bluez", chars[CTRL_UUID]),
                          "org.bluez.GattCharacteristic1")
    dlast = dbus.Interface(bus.get_object("org.bluez", chars[DLAST_UUID]),
                           "org.bluez.GattCharacteristic1")
    dbulk = dbus.Interface(bus.get_object("org.bluez", chars[DBULK_UUID]),
                           "org.bluez.GattCharacteristic1")

    nonce = bytes(ci6.ReadValue({}))
    ci6.WriteValue(list(struct.pack("<I",
                                    binascii.crc32(nonce) & 0xFFFFFFFF)),
                   {"type": "request"})
    pump(500)
    nonce2 = bytes(ci6.ReadValue({}))
    try:
        ci3.WriteValue(list(nonce2 + struct.pack("<I", passkey)),
                       {"type": "request"})
        pump(500)
    except dbus.DBusException:
        pass

    buf = bytearray()
    all_data = bytearray()

    def on_last(_i, ch, _inv):
        if "Value" not in ch:
            return
        d = bytes(int(x) for x in ch["Value"])
        nonlocal buf
        full = bytes(buf) + d
        buf = bytearray()
        all_data.extend(full)

    def on_bulk(_i, ch, _inv):
        if "Value" not in ch:
            return
        d = bytes(int(x) for x in ch["Value"])
        buf.extend(d)

    bus.add_signal_receiver(
        on_last, dbus_interface="org.freedesktop.DBus.Properties",
        signal_name="PropertiesChanged", path=chars[DLAST_UUID])
    bus.add_signal_receiver(
        on_bulk, dbus_interface="org.freedesktop.DBus.Properties",
        signal_name="PropertiesChanged", path=chars[DBULK_UUID])
    for cc in (ctrl, dlast, dbulk):
        try:
            cc.StartNotify()
        except dbus.DBusException:
            pass
    pump(300)
    try:
        bytes(ctrl.ReadValue({}))
    except dbus.DBusException:
        pass
    pump(150)
    ctrl.WriteValue(list(b"\xFA\x80\xFF"), {"type": "command"})
    pump(250)
    ctrl.WriteValue(list(b"\xF9\x80"), {"type": "command"})
    pump(400)
    dlast.WriteValue(list(b"\x03\x00\x9F\x19\xED\xDB\xFF"),
                     {"type": "command"})
    pump(800)
    return dev, dlast, all_data, pump

def _decode_value(data: bytes, off: int):
    """CBOR-decode a single value at offset `off` of `data`.  Returns
    a tuple (kind, value_repr) or None if the data runs out."""
    if off >= len(data):
        return None
    h = data[off]
    if 0x00 <= h <= 0x17:
        return ("uint", h)
    if h == 0x18 and off + 1 < len(data):
        return ("uint", data[off + 1])
    if h == 0x19 and off + 2 < len(data):
        return ("uint", struct.unpack(">H", bytes(data[off + 1:off + 3]))[0])
    if h == 0x1A and off + 4 < len(data):
        return ("uint", struct.unpack(">I", bytes(data[off + 1:off + 5]))[0])
    if 0x40 <= h <= 0x57:
        ln = h & 0x1F
        if off + 1 + ln <= len(data):
            return ("bstr", bytes(data[off + 1:off + 1 + ln]).hex())
    if h == 0x58 and off + 1 < len(data):
        ln = data[off + 1]
        if off + 2 + ln <= len(data):
            return ("bstr", bytes(data[off + 2:off + 2 + ln]).hex())
    if 0x60 <= h <= 0x77:
        ln = h & 0x1F
        if off + 1 + ln <= len(data):
            try:
                return ("tstr", bytes(
                    data[off + 1:off + 1 + ln]).decode("ascii"))
            except UnicodeDecodeError:
                pass
    return ("?", f"h={h:02x}")

def _probe_one(dlast, all_data: bytearray, pump, reg: int,
               write_sentinel: bool = False, pump_ms: int = 500):
    """Probe one VREG.  Returns a dict describing what we observed."""
    pre_len = len(all_data)
    if write_sentinel:
        # 1-byte SetValue — distinguishes unknown (code 1) from
        # everything else.  Crucially, we don't want this to actually
        # take effect, so use a value the firmware will reject.
        payload = bytes([0x06, 0x00, 0x9F, 0x19,
                         (reg >> 8) & 0xFF, reg & 0xFF,
                         0x40,  # bstr length 0
                         0xFF])
    else:
        payload = bytes([0x05, 0x00, 0x9F, 0x19,
                         (reg >> 8) & 0xFF, reg & 0xFF,
                         0xFF])
    try:
        dlast.WriteValue(list(payload), {"type": "command"})
    except dbus.DBusException as exc:
        return {"reg": reg, "lost": str(exc)}
    pump(pump_ms)
    new_data = bytes(all_data[pre_len:])
    push_pat = bytes([0x08, 0x00, 0x19, (reg >> 8) & 0xFF, reg & 0xFF])
    err_pat  = bytes([0x09, 0x00, 0x19, (reg >> 8) & 0xFF, reg & 0xFF])
    pi = new_data.find(push_pat)
    ei = new_data.find(err_pat)
    if pi >= 0:
        v = _decode_value(new_data, pi + 5)
        return {"reg": reg, "kind": "push", "value": v}
    if ei >= 0:
        code = new_data[ei + 5] if ei + 5 < len(new_data) else 0
        return {"reg": reg, "kind": "error", "code": code}
    return {"reg": reg, "kind": "silent"}

def _expand_range(spec: str) -> list[int]:
    if "-" in spec:
        a, b = spec.split("-", 1)
        return list(range(int(a, 0), int(b, 0) + 1))
    return [int(spec, 0)]

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mac", required=True,
                   help="Device MAC address (AA:BB:CC:DD:EE:FF)")
    p.add_argument("--passkey", type=int, default=14916,
                   help="GATT passkey (default 14916 — Cerbo default PIN)")
    p.add_argument("--range",
                   help="Inclusive 0xAAAA-0xBBBB or single 0xAAAA")
    p.add_argument("--candidates", choices=sorted(CANDIDATES.keys()),
                   help="Use a named candidate set "
                        "(core / current / function / ip22-optional)")
    p.add_argument("--write-sentinel", action="store_true",
                   help="Use 1-byte SetValue instead of GetValue.  Returns "
                        "code 2 for registers that exist but didn't accept "
                        "the size — useful when the device only responds "
                        "to writes (some firmwares).")
    p.add_argument("--pump-ms", type=int, default=500,
                   help="ms to pump after each request (default 500)")
    args = p.parse_args()

    if args.range:
        regs = _expand_range(args.range)
    elif args.candidates:
        regs = CANDIDATES[args.candidates]
    else:
        sys.exit("must provide --range or --candidates")

    print(f"Probing {len(regs)} VREG(s) on {args.mac} "
          f"({'write sentinel' if args.write_sentinel else 'GetValue'}, "
          f"{args.pump_ms} ms each)...")
    print()

    dev, dlast, all_data, pump = _open_session(args.mac, args.passkey)
    try:
        for reg in regs:
            r = _probe_one(dlast, all_data, pump, reg,
                           write_sentinel=args.write_sentinel,
                           pump_ms=args.pump_ms)
            tag = "0x{:04X}".format(r["reg"])
            if "lost" in r:
                print(f"  {tag}: connection lost — {r['lost']}")
                break
            kind = r["kind"]
            if kind == "push":
                v = r["value"]
                print(f"  {tag}: {v[0]} = {v[1]}")
            elif kind == "error":
                code = r["code"]
                meaning = {
                    1: "unknown register",
                    2: "parameter / size error → register EXISTS",
                    3: "read-only → register EXISTS",
                }.get(code, f"code {code}")
                if code != 1:
                    print(f"  {tag}: ERR code {code} ({meaning})")
            else:
                print(f"  {tag}: silent (no response)")
    finally:
        try:
            dev.Disconnect()
        except dbus.DBusException:
            pass

if __name__ == "__main__":
    main()
