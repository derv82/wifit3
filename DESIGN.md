# Wifit3 Design Document

## Background & Motivation
Wifit3 is "yet another rewrite of Wifite." 

Wifite1 was a monolithic script. Wifite2 introduced a modular class design but remained architecturally bound to the `aircrack-ng` suite, operating essentially as a complex wrapper that shelled out to subprocesses and scraped `stdout` and CSV files. While functional, this "weak script" feel led to fragility, especially as external tool outputs changed or when integrating modern UI components (like a TUI) that suffered from polling lag.

Wifit3 is a clean-slate reimagining. It aims to shed the legacy dependency hell by handling wireless protocols natively in Python, while providing a beautiful, responsive, and highly customizable Terminal User Interface (TUI).

## Core Tenets
1. **Native First:** Packet parsing, injection, and handshake verification are handled entirely in native Python via Scapy and Pyusb. 
2. **Cross-Platform (Second):** While Linux remains the primary target, the architecture will abstract interface controls to allow Windows (via NPcap) and macOS support where hardware/driver combinations permit. If cross-platform proves too hostile regarding Monitor Mode drivers, a native Linux-only Wifit3 is still a massive net win.
3. **Textual TUI:** A primary focus on UI/UX using the `Textual` framework. Expect themes, resizable layouts, ASCII art, and a highly polished, responsive interface unhindered by background subprocess blocking.

## Key Differences from Wifite2
* **Dependency Pruning:** Wifite2 relied heavily on `airodump-ng`, `aireplay-ng`, etc., for core loops. Wifit3 strips this away. The *only* carried-over dependency concept is tools for enabling Monitor Mode (e.g., `airmon-ng` on Linux, potentially custom driver setups on Windows/OSX). Everything else—scanning, deauths, capturing—is pure Python.
* **Event-Driven Architecture:** Instead of sequential, blocking attack flows, Wifit3 uses an asynchronous, event-driven model. A background `Scanner` emits events (e.g., `BeaconReceived`, `HandshakeCaptured`), and the Textual TUI reacts instantly.
* **Standalone Migration:** This is not an in-place upgrade to Wifite2. It is a new project designed to extract the domain knowledge from v2 without inheriting its structural baggage.

## Proposed Architecture
### 1. Interface Manager (`wifit3.interface`)
OS-agnostic abstraction for network cards.
* Handles identifying wireless interfaces.
* Puts interfaces into Monitor Mode (via `airmon-ng` shell out on Linux, or specific APIs on Windows).
* Handles Channel Hopping (crucial for Windows where Scapy can't hop channels directly).

### 2. Core Engine (`wifit3.engine`)
* **Scanner:** An `asyncio`-compatible loop using Scapy to sniff `Dot11Beacon` and `Dot11ProbeReq/Resp` frames. Maintains an internal state of Targets and Clients.
* **Capture Manager:** Listens for EAPOL frames, PMKIDs, and manages the writing of clean `.pcap` files directly via Scapy.
* **Injector:** Handles crafting and sending `Dot11Deauth` frames natively.

### 3. Textual TUI (`wifit3.ui`)
* A full-screen application managing state centrally.
* Features: Theming engine, ASCII art headers, dynamic data tables for Targets, and dedicated "Attack Views" that update via async messages from the Core Engine.

## Modern Attack Landscape (2026 Context)
*To be expanded during implementation:*
* **WPA3 / SAE:** Requires research into native Scapy implementation for SAE handshake capture/downgrade detection. Wifite2 used `hcxdumptool`; Wifit3 needs a native approach.
* **Evil Twin:** Usually requires `hostapd` and `dnsmasq`. We need to determine if we continue shelling out for these specific daemons or if Scapy can act as a lightweight soft-AP.
* **WPS Pixie Dust:** Determine if the market share of vulnerable routers in 2026 warrants prioritizing this, or if we focus purely on WPA2/WPA3 PMKID/Handshakes first.

## Implementation Phases
- [x] **Phase 1: MVP Scanner & TUI:** 
   - Scaffolding of the `Textual` TUI.
   - Implemented a native Scapy sniffer to populate the TUI with Access Points.
   - Structured project into a professional `src-layout` (`wifit3.engine`, `wifit3.ui`, `wifit3.interface`).
- [x] **Phase 2: Interface & Channel Hopping:** 
   - OS-agnostic Interface Manager (`manager.py`) with Windows `WlanAPI` bindings (`manager_win.py`).
   - Automatically detects monitor mode support via Npcap's `WlanHelper.exe`.
   - Background thread orchestrating automated channel hopping for live capture.
- [ ] **Phase 3: Attack Workflows:** Implementing Native Deauths and Handshake/PMKID capturing.
   - **Next Session Goal:** Refine TUI to be more "hackerish" (reduce ASCII art header size).
   - Implement Target Selection (navigating the DataTable to lock onto a specific BSSID).
- [ ] **Phase 4: Polish & Export:** Theming, PCAP exporting, cracking handoffs (passing clean captures to Hashcat), and expanding attack logic (Evil Twin/WPA3).

## Developer Notes (Current Status)
* **Windows Monitor Mode:** Requires Npcap installed with ✅ **Support raw 802.11 Traffic (and monitor mode)** checked, and ❌ **WinPcap API-compatible mode** UNCHECKED. 
* **Hardware Limitations:** Built-in Intel Wi-Fi cards (like Wi-Fi 7 BE200) do NOT support raw 802.11 via Npcap on Windows. A compatible USB adapter (like ALFA) is required.
* **WlanHelper Quirks:** When calling `WlanHelper.exe <guid> mode monitor`, the GUID must be passed **without** curly braces (e.g., `53812C72-AB51...`). `shell=True` is also often required for Python subprocesses to inherit necessary permissions.
* **Scapy on Windows:** When sniffing with Npcap in monitor mode, `sniff(..., monitor=True)` is mandatory to receive raw 802.11 headers instead of Ethernet frames.
