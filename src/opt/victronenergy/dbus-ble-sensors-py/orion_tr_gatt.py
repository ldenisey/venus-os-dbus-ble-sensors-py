#!/usr/bin/env python3
"""
BLE GATT Connection Manager for Victron Orion-TR Smart

Async GATT write operations for on/off control. Uses the shared D-Bus
SystemBus with async (reply_handler/error_handler) calls so the GLib
main loop is never blocked.

Protocol:
- Service UUID: 306b0001-b081-4037-83dc-e59fcc3cdfd0
- Control char (306b0002): Flow control
- Data last-chunk char (306b0003): Register read/write
"""

import logging
import struct
import time
from typing import Callable, Dict, Optional, Tuple

import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib

logger = logging.getLogger(__name__)

SERVICE_UUID = "306b0001-b081-4037-83dc-e59fcc3cdfd0"
CHAR_CONTROL = "306b0002-b081-4037-83dc-e59fcc3cdfd0"
CHAR_DATA_LAST = "306b0003-b081-4037-83dc-e59fcc3cdfd0"

OPCODE_CHUNK_SIZE = 0xFA
OPCODE_READY_TO_RECV = 0xF9

AGENT_INTERFACE = "org.bluez.Agent1"

def _cbor_uint(n: int) -> bytes:
    if n < 24:
        return bytes([n])
    elif n < 256:
        return bytes([0x18, n])
    elif n < 65536:
        return bytes([0x19, (n >> 8) & 0xFF, n & 0xFF])
    return bytes([0x1A, (n >> 24) & 0xFF, (n >> 16) & 0xFF,
                  (n >> 8) & 0xFF, n & 0xFF])

def _cbor_array(items: list) -> bytes:
    return bytes([0x9F]) + b"".join(items) + bytes([0xFF])

def _cbor_bstr(data: bytes) -> bytes:
    n = len(data)
    if n < 24:
        return bytes([0x40 | n]) + data
    return bytes([0x58, n]) + data

class _PairingAgent(dbus.service.Object):
    """BlueZ D-Bus pairing agent that provides the Victron default passkey."""

    def __init__(self, bus, path, passkey):
        super().__init__(bus, path)
        self._passkey = passkey

    @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method(AGENT_INTERFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        pass

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        logger.info("Pairing agent: providing passkey")
        return dbus.UInt32(self._passkey)

    @dbus.service.method(AGENT_INTERFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        pass

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        pass

    @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
    def Cancel(self):
        pass

def _find_bluez_device(bus, mac):
    """Find (device_path, adapter_path) for *mac* across all BlueZ adapters."""
    om = dbus.Interface(
        bus.get_object("org.bluez", "/", introspect=False),
        "org.freedesktop.DBus.ObjectManager")
    objects = om.GetManagedObjects()
    suffix = "/dev_" + mac.upper().replace(":", "_")
    for path in sorted(objects.keys()):
        if str(path).endswith(suffix) and "org.bluez.Device1" in objects[path]:
            s = str(path)
            adapter_path = s[:s.index(suffix)]
            return s, adapter_path
    return "/org/bluez/hci0" + suffix, "/org/bluez/hci0"

class AsyncGATTWriter:
    """
    Non-blocking GATT register writer.

    Uses async D-Bus calls so it never blocks the GLib main loop.
    Each step chains to the next via reply/error handlers.
    """

    def __init__(self, bus: dbus.SystemBus):
        self._bus = bus
        self._agent = None
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def write_register(self, mac: str, passkey: int, register_id: int,
                       value_bytes: bytes,
                       on_done: Optional[Callable] = None):
        """
        Start an async register write.

        Stops BLE scanning first to avoid BlueZ conflicts on the Cerbo,
        then performs the GATT write, then restarts scanning.

        Args:
            mac: Device MAC (e.g. "EF:C1:11:9D:A3:91")
            passkey: BLE pairing passkey
            register_id: VREG register ID
            value_bytes: Value bytes (little-endian)
            on_done: Callback(success: bool) called when complete
        """
        if self._busy:
            logger.warning("GATT writer busy, rejecting write for %s", mac)
            if on_done:
                on_done(False)
            return

        mac = mac.upper()
        self._busy = True

        device_path, adapter_path = _find_bluez_device(self._bus, mac)
        ctx = {
            "mac": mac,
            "passkey": passkey,
            "register_id": register_id,
            "value_bytes": value_bytes,
            "on_done": on_done,
            "device_path": device_path,
            "adapter_path": adapter_path,
            "agent_path": "/oriontr/agent/%s" % mac.replace(":", ""),
        }

        logger.info("GATT write starting for %s: reg=0x%04X val=%s",
                     mac, register_id, value_bytes.hex())

        # Step 0: Stop BLE scanning to free the adapter
        self._step_stop_adapter_scan(ctx)

    def _step_stop_adapter_scan(self, ctx):
        """Step 0: Stop BLE scanning before GATT operations."""
        try:
            adapter = dbus.Interface(
                self._bus.get_object("org.bluez", ctx["adapter_path"],
                                      introspect=False),
                "org.bluez.Adapter1")

            def on_stopped():
                logger.info("BLE scanning paused for GATT write")
                GLib.timeout_add(1000,
                                 lambda: self._step_check_paired(ctx))

            def on_stop_err(e):
                logger.debug("StopDiscovery: %s (may not be scanning)", e)
                GLib.timeout_add(500,
                                 lambda: self._step_check_paired(ctx))

            adapter.StopDiscovery(reply_handler=on_stopped,
                                  error_handler=on_stop_err)
        except Exception as e:
            logger.debug("Stop scan: %s", e)
            self._step_check_paired(ctx)

    def _done(self, ctx, success: bool):
        # Clean up agent
        if self._agent:
            try:
                agent_mgr = dbus.Interface(
                    self._bus.get_object("org.bluez", "/org/bluez",
                                         introspect=False),
                    "org.bluez.AgentManager1")
                agent_mgr.UnregisterAgent(dbus.ObjectPath(ctx["agent_path"]))
            except Exception:
                pass
            self._agent = None

        # The main BleakScanner loop owns adapter discovery — just drop
        # the pause reference via ``on_done`` and let it restart scanning
        # on its next iteration.
        self._busy = False
        if ctx.get("on_done"):
            ctx["on_done"](success)

    def _step_check_paired(self, ctx):
        """Step 1: Check if device is paired."""
        try:
            device_obj = self._bus.get_object(
                "org.bluez", ctx["device_path"], introspect=False)
            props = dbus.Interface(device_obj,
                                   "org.freedesktop.DBus.Properties")

            def on_paired(value):
                if bool(value):
                    logger.debug("Device %s already paired", ctx["mac"])
                    self._step_connect(ctx)
                else:
                    self._step_pair(ctx)

            def on_error(e):
                logger.warning("Paired check failed for %s: %s",
                               ctx["mac"], e)
                # Device might not exist in BlueZ yet
                self._step_scan(ctx)

            props.Get("org.bluez.Device1", "Paired",
                      reply_handler=on_paired,
                      error_handler=on_error)

        except Exception as e:
            logger.info("Device %s not in BlueZ, scanning: %s",
                         ctx["mac"], e)
            self._step_scan(ctx)

    def _step_scan(self, ctx):
        """Step 1b: Scan for device."""
        try:
            adapter = dbus.Interface(
                self._bus.get_object("org.bluez", ctx["adapter_path"],
                                      introspect=False),
                "org.bluez.Adapter1")

            def on_start():
                logger.debug("Scan started, waiting 5s...")
                GLib.timeout_add(5000, lambda: self._step_stop_scan(ctx))

            def on_start_err(e):
                logger.warning("StartDiscovery failed: %s", e)
                self._done(ctx, False)

            adapter.StartDiscovery(reply_handler=on_start,
                                   error_handler=on_start_err)
        except Exception as e:
            logger.error("Scan init failed: %s", e)
            self._done(ctx, False)

    def _step_stop_scan(self, ctx):
        """Step 1c: Stop scan and check device."""
        try:
            adapter = dbus.Interface(
                self._bus.get_object("org.bluez", ctx["adapter_path"],
                                      introspect=False),
                "org.bluez.Adapter1")
            adapter.StopDiscovery(
                reply_handler=lambda: None,
                error_handler=lambda e: None)
        except Exception:
            pass

        try:
            self._bus.get_object("org.bluez", ctx["device_path"],
                                  introspect=False)
            self._step_check_paired(ctx)
        except Exception:
            logger.error("Device %s not found after scan", ctx["mac"])
            self._done(ctx, False)
        return False

    def _step_pair(self, ctx):
        """Step 2: Pair with device."""
        logger.info("Pairing with %s (passkey %06d)...",
                     ctx["mac"], ctx["passkey"])

        # Register pairing agent
        try:
            self._agent = _PairingAgent(
                self._bus, ctx["agent_path"], ctx["passkey"])
        except KeyError:
            pass

        try:
            agent_mgr = dbus.Interface(
                self._bus.get_object("org.bluez", "/org/bluez",
                                      introspect=False),
                "org.bluez.AgentManager1")
            agent_path = dbus.ObjectPath(ctx["agent_path"])
            try:
                agent_mgr.UnregisterAgent(agent_path)
            except Exception:
                pass
            agent_mgr.RegisterAgent(agent_path, "KeyboardDisplay")
            agent_mgr.RequestDefaultAgent(agent_path)
        except Exception as e:
            logger.warning("Agent registration: %s", e)

        device_obj = self._bus.get_object(
            "org.bluez", ctx["device_path"], introspect=False)

        # Trust the device
        props = dbus.Interface(device_obj,
                               "org.freedesktop.DBus.Properties")
        try:
            props.Set("org.bluez.Device1", "Trusted", dbus.Boolean(True))
        except Exception:
            pass

        # Connect first, then pair
        device = dbus.Interface(device_obj, "org.bluez.Device1")

        def on_connect():
            GLib.timeout_add(2000, lambda: self._do_pair(ctx))

        def on_connect_err(e):
            logger.warning("Pre-pair connect: %s", e)
            GLib.timeout_add(1000, lambda: self._do_pair(ctx))

        device.Connect(reply_handler=on_connect,
                       error_handler=on_connect_err)

    def _do_pair(self, ctx):
        device_obj = self._bus.get_object(
            "org.bluez", ctx["device_path"], introspect=False)
        device = dbus.Interface(device_obj, "org.bluez.Device1")

        def on_pair():
            logger.info("Paired with %s", ctx["mac"])
            GLib.timeout_add(1000, lambda: self._step_connect(ctx))

        def on_pair_err(e):
            logger.error("Pair failed: %s", e)
            # Check if it actually paired despite the error
            try:
                props = dbus.Interface(device_obj,
                                       "org.freedesktop.DBus.Properties")
                if bool(props.Get("org.bluez.Device1", "Paired")):
                    logger.info("Paired despite error for %s", ctx["mac"])
                    self._step_connect(ctx)
                    return
            except Exception:
                pass
            self._done(ctx, False)

        device.Pair(reply_handler=on_pair,
                    error_handler=on_pair_err)
        return False

    def _step_connect(self, ctx):
        """Step 3: Connect to device."""
        device_obj = self._bus.get_object(
            "org.bluez", ctx["device_path"], introspect=False)
        props = dbus.Interface(device_obj,
                               "org.freedesktop.DBus.Properties")

        def check_connected(connected):
            if bool(connected):
                logger.debug("Already connected to %s", ctx["mac"])
                self._step_wait_services(ctx, 0)
            else:
                logger.info("Connecting to %s...", ctx["mac"])
                device = dbus.Interface(device_obj, "org.bluez.Device1")

                def on_connect():
                    logger.info("Connected to %s", ctx["mac"])
                    GLib.timeout_add(1000,
                                     lambda: self._step_wait_services(ctx, 0))

                def on_connect_err(e):
                    logger.error("Connect to %s failed: %s", ctx["mac"], e)
                    self._done(ctx, False)

                device.Connect(reply_handler=on_connect,
                               error_handler=on_connect_err)

        def check_err(e):
            logger.error("Connected check failed: %s", e)
            self._done(ctx, False)

        props.Get("org.bluez.Device1", "Connected",
                  reply_handler=check_connected,
                  error_handler=check_err)

    def _step_wait_services(self, ctx, attempt):
        """Step 4: Wait for ServicesResolved."""
        if attempt >= 15:
            logger.error("Services never resolved for %s", ctx["mac"])
            self._done(ctx, False)
            return False

        device_obj = self._bus.get_object(
            "org.bluez", ctx["device_path"], introspect=False)
        props = dbus.Interface(device_obj,
                               "org.freedesktop.DBus.Properties")

        def on_resolved(value):
            if bool(value):
                self._step_discover_chars(ctx)
            else:
                GLib.timeout_add(
                    1000,
                    lambda: self._step_wait_services(ctx, attempt + 1))

        def on_err(e):
            GLib.timeout_add(
                1000,
                lambda: self._step_wait_services(ctx, attempt + 1))

        props.Get("org.bluez.Device1", "ServicesResolved",
                  reply_handler=on_resolved,
                  error_handler=on_err)
        return False

    def _step_discover_chars(self, ctx):
        """Step 5: Discover 306b characteristics."""
        om = dbus.Interface(
            self._bus.get_object("org.bluez", "/", introspect=False),
            "org.freedesktop.DBus.ObjectManager")

        def on_objects(objects):
            char_paths: Dict[str, str] = {}
            for path, interfaces in objects.items():
                if "org.bluez.GattCharacteristic1" not in interfaces:
                    continue
                if not path.startswith(ctx["device_path"]):
                    continue
                cp = interfaces["org.bluez.GattCharacteristic1"]
                char_uuid = str(cp.get("UUID", ""))
                svc_path = str(cp.get("Service", ""))
                if svc_path not in objects:
                    continue
                svc_ifs = objects[svc_path]
                if "org.bluez.GattService1" not in svc_ifs:
                    continue
                svc_uuid = str(
                    svc_ifs["org.bluez.GattService1"].get("UUID", ""))
                if svc_uuid == SERVICE_UUID:
                    char_paths[char_uuid] = path

            if CHAR_DATA_LAST not in char_paths \
                    or CHAR_CONTROL not in char_paths:
                logger.error("306b chars not found for %s (got %d)",
                             ctx["mac"], len(char_paths))
                self._try_disconnect(ctx)
                self._done(ctx, False)
                return

            ctx["char_paths"] = char_paths
            logger.info("Found %d 306b chars for %s",
                         len(char_paths), ctx["mac"])
            self._step_flow_control(ctx)

        def on_err(e):
            logger.error("GetManagedObjects failed: %s", e)
            self._done(ctx, False)

        om.GetManagedObjects(reply_handler=on_objects,
                             error_handler=on_err)

    def _step_flow_control(self, ctx):
        """Step 6: Send flow control and write register."""
        char_paths = ctx["char_paths"]

        try:
            # Flow control
            _write_char(self._bus, char_paths, CHAR_CONTROL,
                         bytes([OPCODE_CHUNK_SIZE, 0x14]))
        except Exception as e:
            logger.warning("Flow control chunk_size failed: %s", e)

        def do_write():
            try:
                _write_char(self._bus, char_paths, CHAR_CONTROL,
                             bytes([OPCODE_READY_TO_RECV, 0x08]))
            except Exception as e:
                logger.warning("Flow control ready failed: %s", e)

            # Write the register
            cmd = (_cbor_uint(6) + _cbor_uint(0)
                   + _cbor_array([_cbor_uint(ctx["register_id"]),
                                  _cbor_bstr(ctx["value_bytes"])]))
            try:
                _write_char(self._bus, char_paths, CHAR_DATA_LAST, cmd)
                logger.info("Wrote reg 0x%04X = %s on %s",
                            ctx["register_id"],
                            ctx["value_bytes"].hex(),
                            ctx["mac"])
            except Exception as e:
                logger.error("Register write failed: %s", e)
                self._try_disconnect(ctx)
                self._done(ctx, False)
                return False

            # Disconnect after brief delay
            GLib.timeout_add(1000, lambda: self._step_disconnect(ctx))
            return False

        GLib.timeout_add(300, do_write)

    def _step_disconnect(self, ctx):
        """Step 7: Disconnect."""
        self._try_disconnect(ctx)
        self._done(ctx, True)
        return False

    def _try_disconnect(self, ctx):
        try:
            device_obj = self._bus.get_object(
                "org.bluez", ctx["device_path"], introspect=False)
            device = dbus.Interface(device_obj, "org.bluez.Device1")
            device.Disconnect(
                reply_handler=lambda: None,
                error_handler=lambda e: None)
        except Exception:
            pass

def _write_char(bus, char_paths: dict, char_uuid: str, data: bytes):
    path = char_paths[char_uuid]
    # Introspect so dbus-python can auto-marshal `aya{sv}` from a
    # plain list + dict — matches the pattern that worked on-device.
    char_iface = dbus.Interface(
        bus.get_object("org.bluez", path),
        "org.bluez.GattCharacteristic1")
    char_iface.WriteValue(list(data), {"type": "command"}, timeout=5)
