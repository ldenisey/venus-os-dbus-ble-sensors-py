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
    mac: str  # lowercase no-separator, e.g. "aabbccddeeff"
    address_type: int
    rssi: int
    manufacturer_data: dict[int, bytes] = field(default_factory=dict)


def _format_mac(addr_bytes: bytes) -> str:
    """Convert 6 little-endian address bytes to lowercase hex (no separators)."""
    return addr_bytes[::-1].hex()


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


def _walk_ad_structures(data: bytes,
                        mfg_filter: frozenset[int] | None = None) -> dict[int, bytes]:
    """Parse AD structures and extract manufacturer-specific data entries.

    AD structure format (Bluetooth Core Spec Supplement, Part A):
        length (1 byte) — covers ad_type + ad_payload
        ad_type (1 byte)
        ad_payload (length - 1 bytes)

    Manufacturer Specific Data (ad_type 0xFF):
        company_id (2 bytes, little-endian)
        payload (remaining bytes)

    When *mfg_filter* is provided, only matching company IDs are included.
    """
    result: dict[int, bytes] = {}
    pos = 0
    end = len(data)
    while pos < end:
        ad_len = data[pos]
        pos += 1
        if ad_len == 0 or pos + ad_len > end:
            break
        ad_type = data[pos]
        if ad_type == _AD_TYPE_MANUFACTURER and ad_len >= 3:
            company = data[pos + 1] | (data[pos + 2] << 8)
            if mfg_filter is None or company in mfg_filter:
                result[company] = bytes(data[pos + 3 : pos + ad_len])
        pos += ad_len
    return result


def _parse_legacy_reports(payload: bytes, offset: int, adapter_idx: int,
                          mfg_filter: frozenset[int] | None = None,
                          ignored_macs: set[str] | None = None) -> list[TappedAdvertisement]:
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
        if offset + 10 > len(payload):
            break
        offset += 1  # event_type
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

        mac = _format_mac(addr_bytes)
        if ignored_macs is not None and mac in ignored_macs:
            continue

        mfg = _walk_ad_structures(ad_data, mfg_filter)
        if mfg:
            results.append(TappedAdvertisement(
                adapter_index=adapter_idx,
                mac=mac,
                address_type=addr_type,
                rssi=rssi,
                manufacturer_data=mfg,
            ))
    return results


def _parse_extended_reports(payload: bytes, offset: int, adapter_idx: int,
                            mfg_filter: frozenset[int] | None = None,
                            ignored_macs: set[str] | None = None) -> list[TappedAdvertisement]:
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
        if offset + 24 > len(payload):
            break
        event_type_lo = payload[offset]
        offset += 2
        data_status = (event_type_lo >> 5) & 0x03
        addr_type = payload[offset]
        offset += 1
        addr_bytes = payload[offset : offset + 6]
        offset += 6
        offset += 4  # primary_phy + secondary_phy + advertising_sid + tx_power
        rssi_raw = payload[offset]
        offset += 1
        rssi = rssi_raw - 256 if rssi_raw > 127 else rssi_raw
        offset += 9  # periodic_adv_interval + direct_address_type + direct_address
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

        mac = _format_mac(addr_bytes)
        if ignored_macs is not None and mac in ignored_macs:
            continue

        mfg = _walk_ad_structures(ad_data, mfg_filter)
        if mfg:
            results.append(TappedAdvertisement(
                adapter_index=adapter_idx,
                mac=mac,
                address_type=addr_type,
                rssi=rssi,
                manufacturer_data=mfg,
            ))
    return results


def parse_monitor_frame(raw: bytes,
                        mfg_filter: frozenset[int] | None = None,
                        ignored_macs: set[str] | None = None) -> list[TappedAdvertisement]:
    """Parse one monitor channel datagram into advertisement(s).

    Each datagram has a 6-byte header followed by the HCI payload.
    Only HCI events containing LE Advertising Reports are processed;
    all other traffic is silently discarded.

    When *mfg_filter* is provided, only advertisements containing a
    matching manufacturer company ID are returned.

    When *ignored_macs* is provided, advertisements from those MACs are
    dropped before AD structure parsing.
    """
    if len(raw) < _FRAME_HDR_SIZE + 3:
        return []

    # Fast-path: check discriminator bytes before unpacking the header.
    # raw[6] = event_code, raw[8] = subevent (within the HCI payload).
    if raw[6] != _EVT_LE_META:
        return []

    opcode, adapter_idx, payload_len = _FRAME_HDR.unpack_from(raw, 0)
    if opcode != _OP_HCI_EVENT_RX:
        return []

    subevent = raw[8]
    payload = raw[_FRAME_HDR_SIZE:]

    if subevent == _SUB_ADV_REPORT:
        return _parse_legacy_reports(payload, 3, adapter_idx, mfg_filter, ignored_macs)
    elif subevent == _SUB_EXT_ADV_REPORT:
        return _parse_extended_reports(payload, 3, adapter_idx, mfg_filter, ignored_macs)

    return []


def run_tap_loop(sock: socket.socket, callback, stop_event: threading.Event,
                 mfg_filter: frozenset[int] | None = None,
                 ignored_macs: set[str] | None = None):
    """Read monitor frames and invoke callback for each parsed advertisement.

    Blocks until stop_event is set.  The callback receives a single
    TappedAdvertisement argument and is called on the tap thread — the
    caller is responsible for bridging to the appropriate thread.

    When *mfg_filter* is provided, only advertisements with matching
    manufacturer company IDs are forwarded to the callback.

    When *ignored_macs* is provided, advertisements from those MACs are
    dropped before AD structure parsing.
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
        for adv in parse_monitor_frame(raw, mfg_filter, ignored_macs):
            try:
                callback(adv)
            except Exception:
                _log.exception("tap callback error for %s", adv.mac)

    try:
        sock.close()
    except OSError:
        pass
