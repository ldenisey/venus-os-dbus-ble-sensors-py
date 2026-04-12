# HCI Monitor Tap — Advertisement Processing Pipeline

## Overview

BLE advertisements are received via an HCI monitor channel tap — a passive,
read-only socket (`HCI_CHANNEL_MONITOR`) that sees ALL HCI traffic between
the host and every Bluetooth controller.  This is the same mechanism that
`btmon` uses.  It bypasses BlueZ's `AdvertisementMonitor1` filtering
entirely, eliminating the need for Bleak, dbus-fast, or any BlueZ scanning
API.

The tap does not issue any commands and does not interfere with BlueZ or
tie up any adapter.

## Processing Pipeline

The diagram below shows every filter stage a raw HCI packet passes through
before reaching a device class.  Red nodes are drop points; the green node
is the final delivery.

```mermaid
flowchart TD
    subgraph TAP_THREAD["Tap Thread (hci_advertisement_tap.py)"]
        A["sock.recv(4096)<br/>raw HCI monitor frame"] --> B{"raw[6] == 0x3E?<br/>(LE Meta Event)"}
        B -->|No| DROP1["Drop<br/>(~80% of traffic: MGMT events)"]
        B -->|Yes| C{"unpack header<br/>opcode == HCI_EVENT_RX?"}
        C -->|No| DROP2[Drop]
        C -->|Yes| D{"subevent?"}
        D -->|0x02| E["_parse_legacy_reports"]
        D -->|0x0D| F["_parse_extended_reports"]
        D -->|other| DROP3[Drop]

        E & F --> G{"MAC in ignored_macs?"}
        G -->|Yes| DROP4["Drop<br/>(known-irrelevant device)"]
        G -->|No| H["_walk_ad_structures<br/>with mfg_filter"]
        H --> I{"company ID in<br/>mfg_filter?"}
        I -->|No| DROP5["Drop<br/>(unknown manufacturer)"]
        I -->|Yes| J["Build TappedAdvertisement"]
    end

    subgraph CALLBACK["Callback (_on_advertisement, tap thread)"]
        J --> K{"has manufacturer_data?"}
        K -->|No| DROP6[Drop]
        K -->|Yes| L["Update _last_tap_rx<br/>Record in tap_seen"]
        L --> M{"same data as last<br/>& within 15 min?"}
        M -->|Yes| DROP7["Drop<br/>(duplicate)"]
        M -->|No| N["GLib.idle_add()<br/>→ main thread"]
    end

    subgraph MAIN_THREAD["GLib Main Thread (dbus_ble_sensors.py)"]
        N --> O["_glib_process_tap"]
        O --> P["_process_advertisement"]
        P --> Q{"MAC in _ignored_mac?"}
        Q -->|"Yes & not in _known_mac"| DROP8["Drop + stays in<br/>_tap_ignored_macs"]
        Q -->|No or recovered| R{"MAC in _known_mac?"}

        R -->|"No (first time)"| S["DEVICE_CLASSES.get(man_id)"]
        S -->|None| DROP9["Add to _ignored_mac<br/>+ _tap_ignored_macs"]
        S -->|Found| T["device_class(mac)<br/>Instantiate"]
        T --> U{"check_manufacturer_data?"}
        U -->|Fail| DROP10["Add to _ignored_mac<br/>+ _tap_ignored_macs"]
        U -->|Pass| V["configure(man_data)<br/>init()"]
        V --> W["Add to _known_mac"]

        R -->|"Yes (known device)"| X["Get dev_instance"]
        W --> X
        X --> Y{"check_manufacturer_data?"}
        Y -->|Fail| DROP11["Log & skip"]
        Y -->|Pass| Z["dev_instance.handle_manufacturer_data(man_data)"]
    end

    style DROP1 fill:#fee,stroke:#c00
    style DROP2 fill:#fee,stroke:#c00
    style DROP3 fill:#fee,stroke:#c00
    style DROP4 fill:#fee,stroke:#c00
    style DROP5 fill:#fee,stroke:#c00
    style DROP6 fill:#fee,stroke:#c00
    style DROP7 fill:#fee,stroke:#c00
    style DROP8 fill:#fee,stroke:#c00
    style DROP9 fill:#fee,stroke:#c00
    style DROP10 fill:#fee,stroke:#c00
    style DROP11 fill:#fee,stroke:#c00
    style Z fill:#efe,stroke:#0a0
```

## Filter Stages

| # | Stage | Location | Thread | What it drops |
|---|-------|----------|--------|---------------|
| 1 | Event code fast-path | `parse_monitor_frame` | Tap | ~80% of raw traffic (MGMT events, non-LE) |
| 2 | Opcode check | `parse_monitor_frame` | Tap | Non-`HCI_EVENT_RX` frames |
| 3 | Subevent check | `parse_monitor_frame` | Tap | Non-advertising LE Meta subevents |
| 4 | MAC-level filter | `_parse_legacy/extended_reports` | Tap | Previously-rejected MACs (before AD parsing) |
| 5 | Manufacturer ID filter | `_walk_ad_structures` | Tap | Unknown company IDs |
| 6 | Deduplication | `_on_advertisement` | Tap | Identical data from same MAC within 15 min |
| 7 | Thread boundary | `GLib.idle_add()` | Tap → Main | *(not a filter — bridges to main thread)* |
| 8 | `_ignored_mac` check | `_process_advertisement` | Main | MACs rejected in a prior cycle |
| 9 | Device class lookup | `_process_advertisement` | Main | Manufacturer IDs with no registered `BleDevice` subclass |
| 10 | `check_manufacturer_data` | `_process_advertisement` | Main | Device-specific validation (e.g., Mopeka NIC check) |
| 11 | Delivery | `handle_manufacturer_data` | Main | *(final delivery to device class)* |

## Threading Model

Two threads cooperate:

- **Tap thread** (`hci-monitor-tap`): runs `run_tap_loop`, calls
  `_on_advertisement` on the tap thread.  All filtering through stage 6
  happens here.  The `_tap_ignored_macs` set is shared with the main
  thread (CPython GIL guarantees safety for `in` and `add()`).

- **GLib main thread**: runs the GLib main loop, handles D-Bus.  Stages
  8–11 execute here.  The `_prune_tick` timer (every 30s) syncs the
  ignored MAC set: entries that expired from `_ignored_mac` or were
  promoted to `_known_mac` are removed from `_tap_ignored_macs` so
  those devices can be re-evaluated.

## Key Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `DEDUP_KEEPALIVE_SECONDS` | 900 (15 min) | Re-forward identical data as a keepalive |
| `IGNORED_DEVICES_TIMEOUT` | 600 (10 min) | TTL for ignored MAC entries |
| `DEVICE_SERVICES_TIMEOUT` | 3600 (60 min) | TTL for known device entries |
| `SILENCE_WARNING_SECONDS` | 300 (5 min) | Warn if no matching advertisements |
| `ADV_LOG_QUIET_PERIOD` | 1800 (30 min) | Per-device log throttle period |
