"""Passive tap of BLE advertisements via the Linux HCI monitor channel.

Opens a raw Bluetooth socket bound to HCI_CHANNEL_MONITOR (channel 2) which
receives a read-only copy of ALL HCI traffic between the host and every
Bluetooth controller — the same mechanism that btmon uses.  This bypasses
BlueZ's AdvertisementMonitor1 filtering, allowing us to see advertisements
from devices (such as Mopeka sensors) that omit the Flags AD type.

The tap does not issue any commands and does not interfere with BlueZ or
tie up any adapter.

Packet formats are derived from the Bluetooth Core Specification (public
standard) and Linux kernel UAPI headers (userspace API).
"""

import ctypes
import ctypes.util
import logging
import os
import select
import socket
import struct
import threading
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)

# ── Socket constants ──────────────────────────────────────────────────────
# From the Linux kernel UAPI (net/bluetooth/bluetooth.h).
# We use raw integer values because CPython is often compiled without
# Bluetooth socket support (socket.AF_BLUETOOTH may not exist).
_BT_FAMILY = 31          # AF_BLUETOOTH
_BT_RAW = socket.SOCK_RAW
_BT_HCI_PROTO = 1        # BTPROTO_HCI
_MONITOR_CHANNEL = 2  # HCI monitor — passive read-only tap of all HCI traffic
_ALL_CONTROLLERS = 0xFFFF  # receive from every adapter

# ── Monitor frame opcodes (kernel UAPI) ──────────────────────────────────
_OP_HCI_EVENT_RX = 3  # HCI event received from controller

# ── HCI event codes (Bluetooth Core Spec Vol 4, Part E, §7.7) ────────────
_EVT_LE_META = 0x3E

# ── LE Meta subevent codes (Bluetooth Core Spec Vol 4, Part E, §7.7.65) ──
_SUB_ADV_REPORT = 0x02
_SUB_EXT_ADV_REPORT = 0x0D

# ── AD type codes (Bluetooth Core Spec Supplement, Part A, §1) ────────────
_AD_TYPE_MANUFACTURER = 0xFF

# ── Monitor frame header: opcode(u16le), adapter(u16le), payload_len(u16le)
_FRAME_HDR = struct.Struct("<HHH")
_FRAME_HDR_SIZE = _FRAME_HDR.size  # 6

# ── Receive buffer ────────────────────────────────────────────────────────
_RECV_BUF = 4096


# ── ctypes structure for sockaddr_hci ─────────────────────────────────────
# Python's socket.bind() for AF_BLUETOOTH/BTPROTO_HCI only accepts (dev_id,)
# and cannot set hci_channel (CPython issue 36132).  We call libc.bind()
# directly via ctypes as a workaround.

class _HciSocketAddress(ctypes.Structure):
    _fields_ = [
        ("family", ctypes.c_ushort),
        ("dev_id", ctypes.c_ushort),
        ("channel", ctypes.c_ushort),
    ]


@dataclass(slots=True)
class TappedAdvertisement:
    """One parsed BLE advertisement from the monitor channel."""
    adapter_index: int
    mac: str  # uppercase colon-separated, e.g. "AA:BB:CC:DD:EE:FF"
    address_type: int
    rssi: int
    manufacturer_data: dict[int, bytes] = field(default_factory=dict)


def _format_mac(addr_bytes: bytes) -> str:
    """Convert 6 little-endian address bytes to uppercase colon-separated MAC."""
    return ":".join(f"{b:02X}" for b in reversed(addr_bytes))


def create_tap_socket() -> socket.socket:
    """Open a raw HCI socket bound to the monitor channel.

    Returns a non-blocking Python socket ready for select()/recv().
    Raises OSError if the socket cannot be opened or bound.
    """
    sock = socket.socket(_BT_FAMILY, _BT_RAW, _BT_HCI_PROTO)

    libc_path = ctypes.util.find_library("c")
    if libc_path is None:
        # Embedded Linux (e.g. Venus OS) may lack ldconfig; try well-known paths
        for p in ("/lib/libc.so.6", "/usr/lib/libc.so.6"):
            if os.path.exists(p):
                libc_path = p
                break
    if libc_path is None:
        sock.close()
        raise OSError("libc not found")
    libc = ctypes.CDLL(libc_path, use_errno=True)

    addr = _HciSocketAddress(_BT_FAMILY, _ALL_CONTROLLERS, _MONITOR_CHANNEL)
    rc = libc.bind(
        ctypes.c_int(sock.fileno()),
        ctypes.pointer(addr),
        ctypes.c_int(ctypes.sizeof(addr)),
    )
    if rc != 0:
        errno = ctypes.get_errno()
        sock.close()
        raise OSError(errno, f"bind to monitor channel failed: {os.strerror(errno)}")

    sock.setblocking(False)
    return sock


def _walk_ad_structures(data: bytes) -> dict[int, bytes]:
    """Parse AD structures and extract manufacturer-specific data entries.

    AD structure format (Bluetooth Core Spec Supplement, Part A):
        length (1 byte) — covers ad_type + ad_payload
        ad_type (1 byte)
        ad_payload (length - 1 bytes)

    Manufacturer Specific Data (ad_type 0xFF):
        company_id (2 bytes, little-endian)
        payload (remaining bytes)
    """
    result: dict[int, bytes] = {}
    pos = 0
    end = len(data)
    while pos < end:
        if pos + 1 > end:
            break
        ad_len = data[pos]
        pos += 1
        if ad_len == 0 or pos + ad_len > end:
            break
        ad_type = data[pos]
        ad_payload = data[pos + 1 : pos + ad_len]
        pos += ad_len
        if ad_type == _AD_TYPE_MANUFACTURER and len(ad_payload) >= 2:
            company = ad_payload[0] | (ad_payload[1] << 8)
            result[company] = bytes(ad_payload[2:])
    return result


def _parse_legacy_reports(payload: bytes, offset: int, adapter_idx: int) -> list[TappedAdvertisement]:
    """Parse LE Advertising Report (subevent 0x02).

    Per-report layout (Bluetooth Core Spec Vol 4, Part E, §7.7.65.2):
        event_type   (1 byte)
        address_type (1 byte)
        address      (6 bytes, little-endian)
        data_length  (1 byte)
        data         (data_length bytes)
        rssi         (1 byte, signed)
    """
    if offset >= len(payload):
        return []
    num = payload[offset]
    offset += 1
    results: list[TappedAdvertisement] = []
    for _ in range(num):
        if offset + 10 > len(payload):  # minimum: 1+1+6+1+0+1
            break
        # event_type = payload[offset]
        offset += 1
        addr_type = payload[offset]
        offset += 1
        addr_bytes = payload[offset : offset + 6]
        offset += 6
        data_len = payload[offset]
        offset += 1
        if offset + data_len + 1 > len(payload):
            break
        ad_data = payload[offset : offset + data_len]
        offset += data_len
        rssi_raw = payload[offset]
        offset += 1
        rssi = rssi_raw - 256 if rssi_raw > 127 else rssi_raw

        mfg = _walk_ad_structures(ad_data)
        if mfg:
            results.append(TappedAdvertisement(
                adapter_index=adapter_idx,
                mac=_format_mac(addr_bytes),
                address_type=addr_type,
                rssi=rssi,
                manufacturer_data=mfg,
            ))
    return results


def _parse_extended_reports(payload: bytes, offset: int, adapter_idx: int) -> list[TappedAdvertisement]:
    """Parse LE Extended Advertising Report (subevent 0x0D).

    Per-report layout (Bluetooth Core Spec Vol 4, Part E, §7.7.65.13):
        event_type               (2 bytes, little-endian)
        address_type             (1 byte)
        address                  (6 bytes, little-endian)
        primary_phy              (1 byte)
        secondary_phy            (1 byte)
        advertising_sid          (1 byte)
        tx_power                 (1 byte, signed)
        rssi                     (1 byte, signed)
        periodic_adv_interval    (2 bytes)
        direct_address_type      (1 byte)
        direct_address           (6 bytes)
        data_length              (1 byte)
        data                     (data_length bytes)
    """
    if offset >= len(payload):
        return []
    num = payload[offset]
    offset += 1
    results: list[TappedAdvertisement] = []
    for _ in range(num):
        if offset + 24 > len(payload):  # minimum: 2+1+6+1+1+1+1+1+2+1+6+1 = 24
            break
        # event_type (2 bytes): bits 5-6 of low byte = data_status
        event_type_lo = payload[offset]
        offset += 2
        data_status = (event_type_lo >> 5) & 0x03
        addr_type = payload[offset]
        offset += 1
        addr_bytes = payload[offset : offset + 6]
        offset += 6
        # primary_phy(1) + secondary_phy(1) + advertising_sid(1) + tx_power(1)
        offset += 4
        rssi_raw = payload[offset]
        offset += 1
        rssi = rssi_raw - 256 if rssi_raw > 127 else rssi_raw
        # periodic_adv_interval(2) + direct_address_type(1) + direct_address(6)
        offset += 9
        if offset + 1 > len(payload):
            break
        data_len = payload[offset]
        offset += 1
        if offset + data_len > len(payload):
            break
        ad_data = payload[offset : offset + data_len]
        offset += data_len

        if data_status != 0:
            continue

        mfg = _walk_ad_structures(ad_data)
        if mfg:
            results.append(TappedAdvertisement(
                adapter_index=adapter_idx,
                mac=_format_mac(addr_bytes),
                address_type=addr_type,
                rssi=rssi,
                manufacturer_data=mfg,
            ))
    return results


def parse_monitor_frame(raw: bytes) -> list[TappedAdvertisement]:
    """Parse one monitor channel datagram into advertisement(s).

    Each datagram has a 6-byte header followed by the HCI payload.
    Only HCI events containing LE Advertising Reports are processed;
    all other traffic is silently discarded.
    """
    if len(raw) < _FRAME_HDR_SIZE:
        return []

    opcode, adapter_idx, payload_len = _FRAME_HDR.unpack_from(raw, 0)

    if opcode != _OP_HCI_EVENT_RX:
        return []

    payload = raw[_FRAME_HDR_SIZE:]
    if len(payload) < 2:
        return []

    event_code = payload[0]
    # param_total_len = payload[1]  # not needed — we use payload length

    if event_code != _EVT_LE_META:
        return []

    if len(payload) < 3:
        return []

    subevent = payload[2]

    if subevent == _SUB_ADV_REPORT:
        return _parse_legacy_reports(payload, 3, adapter_idx)
    elif subevent == _SUB_EXT_ADV_REPORT:
        return _parse_extended_reports(payload, 3, adapter_idx)

    return []


def run_tap_loop(sock: socket.socket, callback, stop_event: threading.Event):
    """Read monitor frames and invoke callback for each parsed advertisement.

    Blocks until stop_event is set.  The callback receives a single
    TappedAdvertisement argument and is called on the tap thread — the
    caller is responsible for bridging to the appropriate thread.
    """
    while not stop_event.is_set():
        try:
            readable, _, _ = select.select([sock], [], [], 1.0)
        except (OSError, ValueError):
            break
        if not readable:
            continue
        try:
            raw = sock.recv(_RECV_BUF)
        except BlockingIOError:
            continue
        except (OSError, ValueError):
            break
        if not raw:
            break
        for adv in parse_monitor_frame(raw):
            try:
                callback(adv)
            except Exception:
                _log.exception("tap callback error for %s", adv.mac)

    try:
        sock.close()
    except OSError:
        pass
