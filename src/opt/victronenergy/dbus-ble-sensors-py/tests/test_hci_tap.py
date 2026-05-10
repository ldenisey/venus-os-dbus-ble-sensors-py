"""Tests for HCI monitor channel advertisement parsing.

These tests verify the packet parsing logic in hci_advertisement_tap.py
using hand-crafted byte sequences that match the Bluetooth Core Specification
and Linux kernel UAPI monitor channel frame format.
"""
import sys
import os
import socket
import struct
import unittest
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from hci_advertisement_tap import (
    TappedAdvertisement,
    _walk_ad_structures,
    _parse_legacy_reports,
    _parse_extended_reports,
    parse_monitor_frame,
    run_tap_loop,
    _FRAME_HDR,
    _OP_HCI_EVENT_RX,
    _EVT_LE_META,
    _SUB_ADV_REPORT,
    _SUB_EXT_ADV_REPORT,
    _AD_TYPE_MANUFACTURER,
)


def _build_monitor_frame(opcode: int, adapter_idx: int, payload: bytes) -> bytes:
    """Build a complete monitor channel frame with the 6-byte header."""
    return _FRAME_HDR.pack(opcode, adapter_idx, len(payload)) + payload


def _build_legacy_hci_event(reports: list[tuple[int, int, bytes, bytes, int]]) -> bytes:
    """Build a complete HCI LE Meta Event with legacy advertising reports.

    Each report is (event_type, addr_type, addr_bytes, ad_data, rssi).
    """
    body = bytes([len(reports)])
    for evt_type, addr_type, addr_bytes, ad_data, rssi in reports:
        body += bytes([evt_type, addr_type])
        body += addr_bytes
        body += bytes([len(ad_data)])
        body += ad_data
        body += bytes([rssi & 0xFF])
    event = bytes([_EVT_LE_META, len(body) + 1, _SUB_ADV_REPORT]) + body
    return event


def _build_mfg_ad(company_id: int, payload: bytes) -> bytes:
    """Build a manufacturer-specific AD structure."""
    company_le = struct.pack("<H", company_id)
    ad_payload = company_le + payload
    return bytes([len(ad_payload) + 1, _AD_TYPE_MANUFACTURER]) + ad_payload


def _mac_bytes(mac_str: str) -> bytes:
    """Convert 'AA:BB:CC:DD:EE:FF' to 6 little-endian bytes."""
    parts = mac_str.split(":")
    return bytes(int(p, 16) for p in reversed(parts))


class TestWalkAdStructures(unittest.TestCase):
    def test_single_manufacturer_data(self):
        ad = _build_mfg_ad(0x0059, b'\x01\x02\x03')
        result = _walk_ad_structures(ad)
        self.assertIn(0x0059, result)
        self.assertEqual(result[0x0059], b'\x01\x02\x03')

    def test_multiple_ad_types(self):
        flags = bytes([2, 0x01, 0x06])
        mfg = _build_mfg_ad(0x004C, b'\xAA\xBB')
        result = _walk_ad_structures(flags + mfg)
        self.assertNotIn(0x01, result)
        self.assertIn(0x004C, result)
        self.assertEqual(result[0x004C], b'\xAA\xBB')

    def test_empty_data(self):
        self.assertEqual(_walk_ad_structures(b''), {})

    def test_truncated_ad_structure(self):
        result = _walk_ad_structures(bytes([5, _AD_TYPE_MANUFACTURER]))
        self.assertEqual(result, {})

    def test_zero_length_ad(self):
        result = _walk_ad_structures(bytes([0]))
        self.assertEqual(result, {})

    def test_manufacturer_data_too_short_for_company_id(self):
        data = bytes([2, _AD_TYPE_MANUFACTURER, 0x59])
        result = _walk_ad_structures(data)
        self.assertEqual(result, {})

    def test_manufacturer_data_with_only_company_id(self):
        data = bytes([3, _AD_TYPE_MANUFACTURER, 0x59, 0x00])
        result = _walk_ad_structures(data)
        self.assertIn(0x0059, result)
        self.assertEqual(result[0x0059], b'')

    def test_multiple_manufacturer_entries(self):
        mfg1 = _build_mfg_ad(0x0059, b'\x01')
        mfg2 = _build_mfg_ad(0x004C, b'\x02')
        result = _walk_ad_structures(mfg1 + mfg2)
        self.assertIn(0x0059, result)
        self.assertIn(0x004C, result)


class TestParseLegacyReports(unittest.TestCase):
    def test_single_report_with_manufacturer_data(self):
        mac = "AA:BB:CC:DD:EE:FF"
        ad_data = _build_mfg_ad(0x0059, b'\x42\x43')
        body = bytes([1])  # num_reports
        body += bytes([0x00, 0x01])  # event_type, addr_type
        body += _mac_bytes(mac)
        body += bytes([len(ad_data)])
        body += ad_data
        body += bytes([0xD0])  # rssi = -48

        results = _parse_legacy_reports(body, 0, adapter_idx=0)
        self.assertEqual(len(results), 1)
        adv = results[0]
        self.assertEqual(adv.mac, "aabbccddeeff")
        self.assertEqual(adv.address_type, 0x01)
        self.assertEqual(adv.rssi, -48)
        self.assertIn(0x0059, adv.manufacturer_data)
        self.assertEqual(adv.manufacturer_data[0x0059], b'\x42\x43')

    def test_report_without_manufacturer_data_is_filtered(self):
        flags = bytes([2, 0x01, 0x06])
        body = bytes([1])
        body += bytes([0x00, 0x00])
        body += _mac_bytes("11:22:33:44:55:66")
        body += bytes([len(flags)])
        body += flags
        body += bytes([0xC0])

        results = _parse_legacy_reports(body, 0, adapter_idx=0)
        self.assertEqual(len(results), 0)

    def test_multiple_reports(self):
        reports = []
        for i in range(3):
            ad = _build_mfg_ad(0x0059, bytes([i]))
            mac = f"AA:BB:CC:DD:EE:{i:02X}"
            reports.append((0x00, 0x01, _mac_bytes(mac), ad, 0xD0))

        body = bytes([len(reports)])
        for evt_type, addr_type, addr_bytes, ad_data, rssi in reports:
            body += bytes([evt_type, addr_type])
            body += addr_bytes
            body += bytes([len(ad_data)])
            body += ad_data
            body += bytes([rssi])

        results = _parse_legacy_reports(body, 0, adapter_idx=1)
        self.assertEqual(len(results), 3)
        for i, adv in enumerate(results):
            self.assertEqual(adv.adapter_index, 1)
            self.assertIn(0x0059, adv.manufacturer_data)

    def test_empty_payload(self):
        results = _parse_legacy_reports(b'', 0, adapter_idx=0)
        self.assertEqual(results, [])

    def test_truncated_report(self):
        body = bytes([1, 0x00, 0x01])  # num=1 but only 2 bytes of report
        results = _parse_legacy_reports(body, 0, adapter_idx=0)
        self.assertEqual(results, [])


class TestParseExtendedReports(unittest.TestCase):
    def _build_ext_report(self, mac: str, ad_data: bytes, rssi: int = -60,
                          event_type: int = 0x0013) -> bytes:
        """Build one extended advertising report record."""
        report = struct.pack("<H", event_type)
        report += bytes([0x01])  # addr_type
        report += _mac_bytes(mac)
        report += bytes([0x01, 0x00, 0xFF, 0x7F])  # phy, sid, tx_power
        report += bytes([rssi & 0xFF])
        report += struct.pack("<H", 0)  # periodic_adv_interval
        report += bytes([0x00])  # direct_addr_type
        report += bytes(6)  # direct_addr
        report += bytes([len(ad_data)])  # data_length is 1 byte per BT Core Spec
        report += ad_data
        return report

    def test_single_extended_report(self):
        mac = "AA:BB:CC:DD:EE:FF"
        ad = _build_mfg_ad(0x0059, b'\x01\x02')
        body = bytes([1]) + self._build_ext_report(mac, ad, rssi=-55)

        results = _parse_extended_reports(body, 0, adapter_idx=2)
        self.assertEqual(len(results), 1)
        adv = results[0]
        self.assertEqual(adv.mac, "aabbccddeeff")
        self.assertEqual(adv.adapter_index, 2)
        self.assertEqual(adv.rssi, -55)
        self.assertIn(0x0059, adv.manufacturer_data)

    def test_incomplete_data_status_is_filtered(self):
        """Reports with data_status != 0 (incomplete) should be skipped."""
        mac = "AA:BB:CC:DD:EE:FF"
        ad = _build_mfg_ad(0x0059, b'\x01\x02')
        # data_status=0b01 (incomplete) is in bits 5-6: 0x0013 | (0b01 << 5) = 0x0033
        body = bytes([1]) + self._build_ext_report(mac, ad, event_type=0x0033)

        results = _parse_extended_reports(body, 0, adapter_idx=0)
        self.assertEqual(len(results), 0, "Incomplete reports should be filtered")

    def test_truncated_data_status_is_filtered(self):
        """Reports with data_status=0b10 (truncated) should be skipped."""
        mac = "AA:BB:CC:DD:EE:FF"
        ad = _build_mfg_ad(0x0059, b'\x01\x02')
        # data_status=0b10 (truncated): 0x0013 | (0b10 << 5) = 0x0053
        body = bytes([1]) + self._build_ext_report(mac, ad, event_type=0x0053)

        results = _parse_extended_reports(body, 0, adapter_idx=0)
        self.assertEqual(len(results), 0, "Truncated reports should be filtered")

    def test_complete_data_status_is_accepted(self):
        """Reports with data_status=0b00 (complete) should be accepted."""
        mac = "AA:BB:CC:DD:EE:FF"
        ad = _build_mfg_ad(0x0059, b'\x01\x02')
        # data_status=0b00 (complete): standard 0x0013
        body = bytes([1]) + self._build_ext_report(mac, ad, event_type=0x0013)

        results = _parse_extended_reports(body, 0, adapter_idx=0)
        self.assertEqual(len(results), 1, "Complete reports should be accepted")

    def test_empty_payload(self):
        results = _parse_extended_reports(b'', 0, adapter_idx=0)
        self.assertEqual(results, [])


class TestParseMonitorFrame(unittest.TestCase):
    def test_legacy_adv_report(self):
        mac = "AA:BB:CC:DD:EE:FF"
        ad_data = _build_mfg_ad(0x0059, b'\x42\x43')
        hci_event = _build_legacy_hci_event([
            (0x00, 0x01, _mac_bytes(mac), ad_data, 0xD0),
        ])
        frame = _build_monitor_frame(_OP_HCI_EVENT_RX, 0, hci_event)

        results = parse_monitor_frame(frame)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].mac, "aabbccddeeff")
        self.assertEqual(results[0].manufacturer_data[0x0059], b'\x42\x43')

    def test_wrong_opcode_is_ignored(self):
        frame = _build_monitor_frame(99, 0, bytes(10))
        self.assertEqual(parse_monitor_frame(frame), [])

    def test_non_le_meta_event_is_ignored(self):
        payload = bytes([0x0E, 4, 0, 0, 0, 0])  # HCI Command Complete
        frame = _build_monitor_frame(_OP_HCI_EVENT_RX, 0, payload)
        self.assertEqual(parse_monitor_frame(frame), [])

    def test_frame_too_short(self):
        self.assertEqual(parse_monitor_frame(b'\x00\x01'), [])

    def test_mopeka_company_id(self):
        """Mopeka uses company ID 0x0059 (Nordic Semiconductor)."""
        ad = _build_mfg_ad(0x0059, b'\x03\xC3\x05\x00\x60\x40')
        hci_event = _build_legacy_hci_event([
            (0x03, 0x01, _mac_bytes("11:22:33:44:55:66"), ad, 0xBC),
        ])
        frame = _build_monitor_frame(_OP_HCI_EVENT_RX, 0, hci_event)

        results = parse_monitor_frame(frame)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].manufacturer_data[0x0059], b'\x03\xC3\x05\x00\x60\x40')
        self.assertEqual(results[0].rssi, -68)


class TestMacLevelFiltering(unittest.TestCase):
    """Tests for the ignored_macs MAC-level pre-filter."""

    def test_legacy_ignored_mac_is_dropped(self):
        mac = "AA:BB:CC:DD:EE:FF"
        ad_data = _build_mfg_ad(0x0059, b'\x42\x43')
        body = bytes([1])
        body += bytes([0x00, 0x01])
        body += _mac_bytes(mac)
        body += bytes([len(ad_data)])
        body += ad_data
        body += bytes([0xD0])

        ignored = {"aabbccddeeff"}
        results = _parse_legacy_reports(body, 0, adapter_idx=0, ignored_macs=ignored)
        self.assertEqual(len(results), 0)

    def test_legacy_non_ignored_mac_passes(self):
        mac = "AA:BB:CC:DD:EE:FF"
        ad_data = _build_mfg_ad(0x0059, b'\x42\x43')
        body = bytes([1])
        body += bytes([0x00, 0x01])
        body += _mac_bytes(mac)
        body += bytes([len(ad_data)])
        body += ad_data
        body += bytes([0xD0])

        ignored = {"112233445566"}
        results = _parse_legacy_reports(body, 0, adapter_idx=0, ignored_macs=ignored)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].mac, "aabbccddeeff")

    def test_legacy_empty_ignored_set_passes_all(self):
        mac = "AA:BB:CC:DD:EE:FF"
        ad_data = _build_mfg_ad(0x0059, b'\x42\x43')
        body = bytes([1])
        body += bytes([0x00, 0x01])
        body += _mac_bytes(mac)
        body += bytes([len(ad_data)])
        body += ad_data
        body += bytes([0xD0])

        results = _parse_legacy_reports(body, 0, adapter_idx=0, ignored_macs=set())
        self.assertEqual(len(results), 1)

    def test_legacy_none_ignored_passes_all(self):
        mac = "AA:BB:CC:DD:EE:FF"
        ad_data = _build_mfg_ad(0x0059, b'\x42\x43')
        body = bytes([1])
        body += bytes([0x00, 0x01])
        body += _mac_bytes(mac)
        body += bytes([len(ad_data)])
        body += ad_data
        body += bytes([0xD0])

        results = _parse_legacy_reports(body, 0, adapter_idx=0, ignored_macs=None)
        self.assertEqual(len(results), 1)

    def test_legacy_multiple_reports_only_ignored_dropped(self):
        """With 3 reports, only the one whose MAC is in ignored_macs is dropped."""
        body = bytes([3])
        macs = ["AA:BB:CC:DD:EE:00", "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"]
        for mac in macs:
            ad_data = _build_mfg_ad(0x0059, b'\x42')
            body += bytes([0x00, 0x01])
            body += _mac_bytes(mac)
            body += bytes([len(ad_data)])
            body += ad_data
            body += bytes([0xD0])

        ignored = {"aabbccddee01"}
        results = _parse_legacy_reports(body, 0, adapter_idx=0, ignored_macs=ignored)
        self.assertEqual(len(results), 2)
        result_macs = {r.mac for r in results}
        self.assertNotIn("aabbccddee01", result_macs)
        self.assertIn("aabbccddee00", result_macs)
        self.assertIn("aabbccddee02", result_macs)

    def test_extended_ignored_mac_is_dropped(self):
        mac = "AA:BB:CC:DD:EE:FF"
        ad = _build_mfg_ad(0x0059, b'\x01\x02')
        report = struct.pack("<H", 0x0013)
        report += bytes([0x01])
        report += _mac_bytes(mac)
        report += bytes([0x01, 0x00, 0xFF, 0x7F])
        report += bytes([0xC4])  # rssi = -60
        report += struct.pack("<H", 0)
        report += bytes([0x00])
        report += bytes(6)
        report += bytes([len(ad)])
        report += ad
        body = bytes([1]) + report

        ignored = {"aabbccddeeff"}
        results = _parse_extended_reports(body, 0, adapter_idx=0, ignored_macs=ignored)
        self.assertEqual(len(results), 0)

    def test_extended_non_ignored_mac_passes(self):
        mac = "AA:BB:CC:DD:EE:FF"
        ad = _build_mfg_ad(0x0059, b'\x01\x02')
        report = struct.pack("<H", 0x0013)
        report += bytes([0x01])
        report += _mac_bytes(mac)
        report += bytes([0x01, 0x00, 0xFF, 0x7F])
        report += bytes([0xC4])
        report += struct.pack("<H", 0)
        report += bytes([0x00])
        report += bytes(6)
        report += bytes([len(ad)])
        report += ad
        body = bytes([1]) + report

        ignored = {"112233445566"}
        results = _parse_extended_reports(body, 0, adapter_idx=0, ignored_macs=ignored)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].mac, "aabbccddeeff")

    def test_parse_monitor_frame_threads_ignored_macs(self):
        """Verify ignored_macs flows through parse_monitor_frame to the sub-parser."""
        mac = "AA:BB:CC:DD:EE:FF"
        ad_data = _build_mfg_ad(0x0059, b'\x42\x43')
        hci_event = _build_legacy_hci_event([
            (0x00, 0x01, _mac_bytes(mac), ad_data, 0xD0),
        ])
        frame = _build_monitor_frame(_OP_HCI_EVENT_RX, 0, hci_event)

        results_without = parse_monitor_frame(frame, ignored_macs=None)
        self.assertEqual(len(results_without), 1)

        results_with = parse_monitor_frame(frame, ignored_macs={"aabbccddeeff"})
        self.assertEqual(len(results_with), 0)

    def test_ignored_mac_skips_ad_parsing(self):
        """An ignored MAC should be dropped before _walk_ad_structures runs.

        We verify indirectly: even with manufacturer data that would match,
        an ignored MAC produces no results.
        """
        mac = "AA:BB:CC:DD:EE:FF"
        ad_data = _build_mfg_ad(0x0059, b'\x42\x43')
        body = bytes([1])
        body += bytes([0x00, 0x01])
        body += _mac_bytes(mac)
        body += bytes([len(ad_data)])
        body += ad_data
        body += bytes([0xD0])

        ignored = {"aabbccddeeff"}
        results = _parse_legacy_reports(body, 0, adapter_idx=0, ignored_macs=ignored)
        self.assertEqual(len(results), 0)


class TestRunTapLoop(unittest.TestCase):
    def test_stop_event_exits_loop(self):
        """run_tap_loop should exit promptly when stop_event is set."""
        import socket
        r, w = socket.socketpair()
        stop = threading.Event()
        received = []

        def cb(adv):
            received.append(adv)

        stop.set()
        run_tap_loop(r, cb, stop)
        w.close()
        # Should not block or raise


class TestTappedAdvertisement(unittest.TestCase):
    def test_dataclass_fields(self):
        adv = TappedAdvertisement(
            adapter_index=0,
            mac="aabbccddeeff",
            address_type=1,
            rssi=-50,
            manufacturer_data={0x0059: b'\x01'},
        )
        self.assertEqual(adv.adapter_index, 0)
        self.assertEqual(adv.mac, "aabbccddeeff")
        self.assertEqual(adv.rssi, -50)

    def test_default_manufacturer_data(self):
        adv = TappedAdvertisement(adapter_index=0, mac="A", address_type=0, rssi=0)
        self.assertEqual(adv.manufacturer_data, {})


if __name__ == '__main__':
    unittest.main()
