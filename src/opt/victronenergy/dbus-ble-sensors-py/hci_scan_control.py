# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Drive BLE passive scanning directly via the kernel HCI socket.

Replaces our earlier approach of registering a BlueZ
``AdvertisementMonitor1`` to trigger passive scanning.  That approach
worked but had the side effect of forcing BlueZ to create
``/org/bluez/hciN/dev_*`` Device1 objects for every advertiser it
saw and emit a ``PropertiesChanged`` signal on every received
advertisement.  Over hours, dbus-daemon's heap allocator never
returned that memory to the OS and the Cerbo would slowly march
toward OOM.

Empirical observation (5-13-2026 audit): with bluez actively driven
by our monitor we measured ~95 MB/hr dbus-daemon RSS growth.  With
the equivalent ``hcitool lescan --passive --duplicates`` driving the
scan instead — and no AdvertisementMonitor registered — dbus-daemon
RSS stayed completely flat (zero growth in a 75 s window with the
same controller activity).  BlueZ doesn't auto-create Device1
objects from ads it sees when no monitor is registered and no
discovery is in progress.  GATT operations (e.g. Orion-TR key
provisioning) still work because bleak's
``Adapter1.ConnectDevice(mac)`` creates the device entry on demand.

This module is the Python equivalent of ``hcitool lescan
--passive --duplicates``.  We open an ``HCI_CHANNEL_RAW`` socket
(cooperative multi-user channel — the same one hcitool uses), install
the standard filter so Command Complete events come back to us, then
issue the three commands that put the controller into passive scan:

    1. LE Set Scan Enable (enable=0)            disable so we can reconfigure
    2. LE Set Scan Parameters (type=passive, ...)
    3. LE Set Scan Enable (enable=1, dup=0)     enable, report every ad

These commands have to be re-issued periodically because other things
on the system (notably ``shyion-switch`` doing active scans via
bleak) reset the controller's scan parameters.  See
:func:`enable_passive_scan` for the recovery model.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import select
import socket
import struct
import time

_log = logging.getLogger(__name__)

# ── Socket constants (Linux net/bluetooth UAPI) ────────────────────────────
_BT_FAMILY = 31           # AF_BLUETOOTH
_BT_HCI_PROTO = 1         # BTPROTO_HCI
_HCI_CHANNEL_RAW = 0      # cooperative; multiple processes may bind

# ── HCI packet types & event codes (Bluetooth Core Spec Vol 4 Part E) ─────
_HCI_CMD_PKT = 0x01
_HCI_EVT_PKT = 0x04
_EVT_CMD_COMPLETE = 0x0E
_EVT_CMD_STATUS = 0x0F
_EVT_LE_META = 0x3E

# ── Opcode group / command (Bluetooth Core Spec §7.8.10–11) ───────────────
_OGF_LE = 0x08
_OCF_LE_SET_SCAN_PARAMS = 0x000B
_OCF_LE_SET_SCAN_ENABLE = 0x000C

# Filter Accept List management (formerly "Whitelist") — Bluetooth Core
# Spec Vol 4 Part E §7.8.14–16.  The controller drops every advertisement
# whose source address isn't in this list when scan parameters specify
# filter policy 0x01.  List size is hardware-fixed (8–32 typical).
_OCF_LE_READ_ACCEPT_LIST_SIZE = 0x000F
_OCF_LE_CLEAR_ACCEPT_LIST = 0x0010
_OCF_LE_ADD_DEVICE_TO_ACCEPT_LIST = 0x0011

# Filter policy values for LE Set Scan Parameters
FILTER_POLICY_ACCEPT_ALL = 0x00
FILTER_POLICY_ACCEPT_LIST_ONLY = 0x01

# Address types (LE Add Device To Filter Accept List parameter byte 0)
ADDR_TYPE_PUBLIC = 0x00
ADDR_TYPE_RANDOM = 0x01

# ── setsockopt: install HCI event filter (bluez/lib/hci.h) ─────────────────
_SOL_HCI = 0
_HCI_FILTER = 2


class _HciSockAddr(ctypes.Structure):
    """``struct sockaddr_hci`` from ``net/bluetooth/hci_sock.h``.

    Python's ``socket.bind()`` on ``AF_BLUETOOTH/BTPROTO_HCI`` only
    accepts ``(dev_id,)`` and can't set ``hci_channel``
    (CPython issue 36132).  We call ``libc.bind()`` directly via
    ctypes as a workaround — same pattern as
    ``hci_advertisement_tap``.
    """
    _fields_ = [
        ("family", ctypes.c_ushort),
        ("dev_id", ctypes.c_ushort),
        ("channel", ctypes.c_ushort),
    ]


def open_hci_raw(adapter_index: int) -> socket.socket:
    """Open a writable ``HCI_CHANNEL_RAW`` socket on the given adapter.

    Installs an HCI filter that delivers Command Complete, Command
    Status, and LE Meta events back to us.  Without the filter, the
    kernel routes all event packets to other open sockets (typically
    bluez's) and our send-and-wait flow times out.  Mirrors hcitool.

    The socket coexists with bluez — bluez normally uses
    ``HCI_CHANNEL_USER`` (exclusive) only when it wants raw control,
    which is rare; in the steady-state Venus configuration we observed,
    bluez does its work through the kernel mgmt-api and leaves the RAW
    channel available for cooperative tools like us and hcitool.
    """
    s = socket.socket(_BT_FAMILY, socket.SOCK_RAW, _BT_HCI_PROTO)
    addr = _HciSockAddr(_BT_FAMILY, adapter_index, _HCI_CHANNEL_RAW)
    libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
    if libc.bind(s.fileno(), ctypes.byref(addr), ctypes.sizeof(addr)) != 0:
        err = ctypes.get_errno()
        s.close()
        raise OSError(err, f"bind() failed on hci{adapter_index} HCI_CHANNEL_RAW")

    # Filter: deliver event packets only, and only the event codes we
    # need to drive the command/response handshake.
    type_mask = 1 << _HCI_EVT_PKT
    em0 = (1 << _EVT_CMD_COMPLETE) | (1 << _EVT_CMD_STATUS)
    em1 = 1 << (_EVT_LE_META - 32)
    # ``struct hci_filter`` is { type_mask: ulong, event_mask[2]: ulong,
    # opcode: u16 }.  We pack three 32-bit words + the opcode and pad
    # to 16 bytes which the kernel accepts on 32-bit ARM (and is
    # forward-compatible on 64-bit, since the leading 32-bit values
    # are interpreted regardless of word size).
    flt = struct.pack("<IIIH", type_mask, em0, em1, 0)
    flt += b'\x00' * (16 - len(flt))
    if libc.setsockopt(s.fileno(), _SOL_HCI, _HCI_FILTER,
                       ctypes.c_char_p(flt), len(flt)) != 0:
        err = ctypes.get_errno()
        s.close()
        raise OSError(err, "setsockopt(HCI_FILTER) failed")
    return s


def _hci_cmd(ogf: int, ocf: int, params: bytes) -> bytes:
    """Pack an HCI command for transmission over the RAW socket.

    Wire format (from the Bluetooth Core Spec, Volume 2 Part E):
      pkt_type(1) | opcode_LE(2) | param_len(1) | params
    """
    opcode = (ogf << 10) | ocf
    return bytes([_HCI_CMD_PKT]) + struct.pack("<HB", opcode, len(params)) + params


def _send_and_wait_complete(s: socket.socket, ogf: int, ocf: int,
                            params: bytes, timeout: float = 2.0) -> int:
    """Send an HCI command and wait for its matching Command Complete.

    Returns the controller's status byte (0 = success).  Raises
    :class:`TimeoutError` if the matching reply doesn't arrive within
    *timeout* seconds, or if the kernel returned a non-event packet
    we can't interpret.

    The status byte is the controller's per-command result code (BT
    Core Spec Volume 1 Part F).  Most often we'll see ``0x00`` for
    success or ``0x0C`` (Command Disallowed) if the requested state
    transition is invalid in the current controller state.
    """
    opcode = (ogf << 10) | ocf
    s.send(_hci_cmd(ogf, ocf, params))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r, _, _ = select.select([s], [], [], max(0.0, deadline - time.monotonic()))
        if not r:
            break
        data = s.recv(258)
        if len(data) < 7 or data[0] != _HCI_EVT_PKT:
            continue
        if data[1] != _EVT_CMD_COMPLETE:
            continue
        # CmdComplete params: Num_HCI_Cmd_Packets(1) | Opcode(2 LE) | Status(1) | ...
        evt_opcode = struct.unpack("<H", data[4:6])[0]
        if evt_opcode != opcode:
            continue
        return data[6]
    raise TimeoutError(f"HCI Command Complete for opcode 0x{opcode:04x} "
                       f"did not arrive within {timeout}s")


# Default scan parameters.  Interval and Window are in units of 0.625 ms.
# 0x0010 == 16 * 0.625 = 10 ms.  With interval == window, the controller
# scans continuously — same as the bluez "background passive scan"
# defaults.  These match what hcitool's ``lescan --passive`` requests.
_DEFAULT_SCAN_INTERVAL = 0x0010
_DEFAULT_SCAN_WINDOW = 0x0010


def enable_passive_scan(adapter_index: int,
                        interval: int = _DEFAULT_SCAN_INTERVAL,
                        window: int = _DEFAULT_SCAN_WINDOW,
                        filter_policy: int = FILTER_POLICY_ACCEPT_ALL) -> bool:
    """Configure and enable passive LE scanning on the given adapter.

    Opens a short-lived HCI_CHANNEL_RAW socket, issues the three
    standard commands (disable → set params → enable), and closes the
    socket.  Returns True on success.

    Logs and returns False on any failure rather than raising — callers
    are typically GLib timer callbacks that need to keep firing.

    The disable→params→enable sequence is required because the
    controller refuses LE Set Scan Parameters while scanning is
    already enabled (it returns Command Disallowed = 0x0C).  We
    tolerate that on the initial disable (in case scanning was off
    anyway) but require success on the parameter set and final enable.

    ``filter_policy`` selects whether the controller delivers every
    advertisement it hears (``FILTER_POLICY_ACCEPT_ALL`` — default)
    or only those whose source MAC is in the Filter Accept List
    (``FILTER_POLICY_ACCEPT_LIST_ONLY``).  Populate the accept list
    via :func:`clear_accept_list` + :func:`add_device_to_accept_list`
    *before* calling this with the restrictive policy.
    """
    try:
        s = open_hci_raw(adapter_index)
    except OSError as exc:
        _log.warning(f"hci{adapter_index}: open_hci_raw failed: {exc}")
        return False

    try:
        # 1. Disable so we can update parameters.  Status 0x0C just
        #    means scanning wasn't on — fine.
        try:
            status = _send_and_wait_complete(
                s, _OGF_LE, _OCF_LE_SET_SCAN_ENABLE,
                struct.pack("<BB", 0x00, 0x00))
            if status not in (0x00, 0x0C):
                _log.warning(f"hci{adapter_index}: scan disable status=0x{status:02x}")
        except TimeoutError as exc:
            _log.warning(f"hci{adapter_index}: scan disable timed out: {exc}")

        # 2. Set passive scan parameters.
        params = struct.pack("<BHHBB",
                             0x00,            # Scan_Type 0 = passive
                             interval,        # LE_Scan_Interval
                             window,          # LE_Scan_Window
                             0x00,            # Own_Address_Type 0 = public
                             filter_policy)   # Scanning_Filter_Policy
        status = _send_and_wait_complete(
            s, _OGF_LE, _OCF_LE_SET_SCAN_PARAMS, params)
        if status != 0x00:
            # 0x0C = Command Disallowed (scan already on under another
            # driver's control); log at debug to avoid filling the log
            # on systems where bluez or another service owns one of
            # the adapters.
            level = logging.DEBUG if status == 0x0C else logging.WARNING
            _log.log(level,
                f"hci{adapter_index}: LE Set Scan Parameters status=0x{status:02x}")
            return False

        # 3. Enable scanning, no duplicate filtering (we want every ad).
        status = _send_and_wait_complete(
            s, _OGF_LE, _OCF_LE_SET_SCAN_ENABLE,
            struct.pack("<BB", 0x01, 0x00))
        if status != 0x00:
            level = logging.DEBUG if status == 0x0C else logging.WARNING
            _log.log(level,
                f"hci{adapter_index}: LE Set Scan Enable status=0x{status:02x}")
            return False

        return True
    finally:
        s.close()


def read_accept_list_size(adapter_index: int) -> 'int | None':
    """Return the controller's Filter Accept List capacity.

    Returns ``None`` if the read fails — typically because the
    controller doesn't support the LE_Read_Filter_Accept_List_Size
    command on this firmware revision.  Callers should treat None as
    "feature unavailable, fall back to accept-all".
    """
    try:
        s = open_hci_raw(adapter_index)
    except OSError as exc:
        _log.warning(f"hci{adapter_index}: open_hci_raw failed: {exc}")
        return None
    try:
        opcode = (_OGF_LE << 10) | _OCF_LE_READ_ACCEPT_LIST_SIZE
        s.send(_hci_cmd(_OGF_LE, _OCF_LE_READ_ACCEPT_LIST_SIZE, b''))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            r, _, _ = select.select([s], [], [], max(0.0, deadline - time.monotonic()))
            if not r:
                break
            data = s.recv(258)
            if len(data) < 8 or data[0] != _HCI_EVT_PKT:
                continue
            if data[1] != _EVT_CMD_COMPLETE:
                continue
            if struct.unpack("<H", data[4:6])[0] != opcode:
                continue
            status = data[6]
            if status != 0x00:
                _log.warning(f"hci{adapter_index}: Read Accept List Size status=0x{status:02x}")
                return None
            return data[7]  # one-byte size
        _log.warning(f"hci{adapter_index}: Read Accept List Size timed out")
        return None
    finally:
        s.close()


def clear_accept_list(adapter_index: int) -> bool:
    """Empty the controller's Filter Accept List.

    LE Clear Filter Accept List can only be issued while scanning is
    disabled — caller is responsible for that.  The cleanest pattern:
    disable scan → clear → add MACs → set params with filter policy →
    enable scan.  See :func:`apply_accept_list` for the full sequence.
    """
    try:
        s = open_hci_raw(adapter_index)
    except OSError as exc:
        _log.warning(f"hci{adapter_index}: open_hci_raw failed: {exc}")
        return False
    try:
        status = _send_and_wait_complete(
            s, _OGF_LE, _OCF_LE_CLEAR_ACCEPT_LIST, b'')
        if status != 0x00:
            _log.warning(f"hci{adapter_index}: Clear Accept List status=0x{status:02x}")
            return False
        return True
    finally:
        s.close()


def _mac_str_to_le_bytes(mac: str) -> bytes:
    """Convert a 12-hex-char no-separator MAC to little-endian 6 bytes.

    Example: ``'00a0508d9569'`` → ``b'\\x69\\x95\\x8d\\x50\\xa0\\x00'``
    (the controller expects LE byte order, opposite of the usual
    human-readable colon notation).
    """
    if len(mac) != 12:
        raise ValueError(f"expected 12 hex chars, got {len(mac)}: {mac!r}")
    return bytes.fromhex(mac)[::-1]


def add_device_to_accept_list(adapter_index: int, address_type: int,
                              mac: str) -> bool:
    """Add a single MAC + address_type pair to the controller's accept list.

    ``mac`` is a 12-char lowercase no-separator hex string (the format
    our HCI tap produces).  ``address_type`` is 0 (public) or 1
    (random / random-static).

    LE Add Device To Filter Accept List can be issued while scanning
    is enabled or disabled (different from Clear/SetParams), so this
    is the cheapest of the three accept-list management commands.
    """
    if address_type not in (ADDR_TYPE_PUBLIC, ADDR_TYPE_RANDOM):
        _log.warning(f"add_device_to_accept_list: unexpected address_type {address_type}")
        return False
    try:
        s = open_hci_raw(adapter_index)
    except OSError as exc:
        _log.warning(f"hci{adapter_index}: open_hci_raw failed: {exc}")
        return False
    try:
        try:
            params = bytes([address_type]) + _mac_str_to_le_bytes(mac)
        except ValueError as exc:
            _log.warning(f"add_device_to_accept_list: bad MAC {mac!r}: {exc}")
            return False
        status = _send_and_wait_complete(
            s, _OGF_LE, _OCF_LE_ADD_DEVICE_TO_ACCEPT_LIST, params)
        if status != 0x00:
            # 0x12 = Invalid Parameters, 0x07 = Memory Capacity Exceeded
            # — both worth logging at warn so the caller can react.
            _log.warning(
                f"hci{adapter_index}: Add {mac}/{address_type} to accept list "
                f"status=0x{status:02x}")
            return False
        return True
    finally:
        s.close()


def apply_accept_list(adapter_index: int,
                      devices: 'list[tuple[str, int]]',
                      interval: int = _DEFAULT_SCAN_INTERVAL,
                      window: int = _DEFAULT_SCAN_WINDOW) -> bool:
    """Atomically replace the controller's accept list and (re)enable scanning
    in accept-list-only mode.

    Disables scanning, clears the list, adds every (mac, address_type)
    in ``devices``, then re-enables scanning with
    ``FILTER_POLICY_ACCEPT_LIST_ONLY``.  All in one HCI socket open
    so we minimise the scan-disabled window.

    Skips entries that fail to add (e.g. if the controller's list
    overflows) but still completes the rest.  Returns True if the
    final scan-enable succeeded.
    """
    try:
        s = open_hci_raw(adapter_index)
    except OSError as exc:
        _log.warning(f"hci{adapter_index}: open_hci_raw failed: {exc}")
        return False
    try:
        # Disable scanning so Clear/SetParams are accepted.
        try:
            _send_and_wait_complete(
                s, _OGF_LE, _OCF_LE_SET_SCAN_ENABLE,
                struct.pack("<BB", 0x00, 0x00))
        except TimeoutError:
            pass

        # Clear & repopulate.
        status = _send_and_wait_complete(
            s, _OGF_LE, _OCF_LE_CLEAR_ACCEPT_LIST, b'')
        if status != 0x00:
            _log.warning(f"hci{adapter_index}: Clear Accept List status=0x{status:02x}")
            return False

        added = 0
        for mac, addr_type in devices:
            try:
                params = bytes([addr_type]) + _mac_str_to_le_bytes(mac)
            except ValueError as exc:
                _log.warning(f"apply_accept_list: bad MAC {mac!r}: {exc}")
                continue
            try:
                status = _send_and_wait_complete(
                    s, _OGF_LE, _OCF_LE_ADD_DEVICE_TO_ACCEPT_LIST, params)
                if status != 0x00:
                    _log.warning(
                        f"hci{adapter_index}: Add {mac}/{addr_type} status=0x{status:02x}")
                    continue
                added += 1
            except TimeoutError as exc:
                _log.warning(f"hci{adapter_index}: Add {mac} timed out: {exc}")
                continue

        # Set parameters with filter_policy=1 then re-enable.
        params = struct.pack(
            "<BHHBB",
            0x00,        # passive
            interval,
            window,
            0x00,        # public own_addr_type
            FILTER_POLICY_ACCEPT_LIST_ONLY,
        )
        status = _send_and_wait_complete(
            s, _OGF_LE, _OCF_LE_SET_SCAN_PARAMS, params)
        if status != 0x00:
            # 0x0C = Command Disallowed.  Typically means scanning is
            # already on under another driver's control; that's
            # informational not exceptional, so log at debug.  Higher
            # layer (DbusBleSensors._start_passive_scan) handles
            # user-facing notification with streak throttling.
            level = logging.DEBUG if status == 0x0C else logging.WARNING
            _log.log(level,
                f"hci{adapter_index}: Set Scan Params (accept-list) status=0x{status:02x}")
            return False

        status = _send_and_wait_complete(
            s, _OGF_LE, _OCF_LE_SET_SCAN_ENABLE,
            struct.pack("<BB", 0x01, 0x00))
        if status != 0x00:
            level = logging.DEBUG if status == 0x0C else logging.WARNING
            _log.log(level,
                f"hci{adapter_index}: Set Scan Enable (accept-list) status=0x{status:02x}")
            return False

        # Steady-state re-apply happens every periodic tick.  Log at
        # debug here; the higher-level caller in DbusBleSensors logs
        # at info on transitions (policy change, first enable).
        _log.debug(f"hci{adapter_index}: accept-list scan active "
                   f"({added}/{len(devices)} devices in list)")
        return True
    finally:
        s.close()


def disable_passive_scan(adapter_index: int) -> bool:
    """Disable LE scanning on the given adapter.

    Best-effort — returns False on failure but does not raise.  Called
    from the load-throttle trip path to give the controller a break
    when the Cerbo is near the watchdog limit, and from the service
    shutdown path so we don't leave the radio in scan mode after the
    process exits.
    """
    try:
        s = open_hci_raw(adapter_index)
    except OSError as exc:
        _log.warning(f"hci{adapter_index}: open_hci_raw failed during disable: {exc}")
        return False
    try:
        try:
            status = _send_and_wait_complete(
                s, _OGF_LE, _OCF_LE_SET_SCAN_ENABLE,
                struct.pack("<BB", 0x00, 0x00))
            if status not in (0x00, 0x0C):
                _log.warning(f"hci{adapter_index}: scan disable status=0x{status:02x}")
                return False
            return True
        except TimeoutError as exc:
            _log.warning(f"hci{adapter_index}: scan disable timed out: {exc}")
            return False
    finally:
        s.close()
