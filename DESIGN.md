# Wifit3 Design Document

## Background & Motivation
Wifit3 is "yet another rewrite of Wifite." 

Wifite1 was a monolithic script. Wifite2 introduced a modular class design but remained architecturally bound to the `aircrack-ng` suite, operating essentially as a complex wrapper that shelled out to subprocesses and scraped `stdout` and CSV files. While functional, this "weak script" feel led to fragility, especially as external tool outputs changed or when integrating modern UI components (like a TUI) that suffered from polling lag.

Wifit3 is a clean-slate reimagining. It aims to shed the legacy dependency hell by handling wireless protocols natively in Python, while providing a beautiful, responsive, and highly customizable Terminal User Interface (TUI).

## Core Tenets
1. **Userland (no sudo):** Users are only required to use Admin priviledges *while setting up their wireless driver to allow interfacing with Pyusb*. **Everything else is in Userland**: raw USB packets sent to the wireles card to enable monitor mode, inject/sniff packets, etc.
2. **Cross-Platform:** The bytes sent to the wireless card are platform-agnositic. Windows users have Zadig (WinUSB), Linux users have "rmmod" and "detach_kernel_driver()", OSX users can have "Codeless Kext" to override system driver capture (allegedly).
3. **Native First:** Packet parsing, injection, and handshake verification are handled entirely in native Python via Pyusb (Scapy is bloated, causes UAC warnings every time).
4. **Textual TUI:** A primary focus on UI/UX using the `Textual` framework. Expect themes, resizable layouts, ASCII art, and a highly polished, responsive interface unhindered by background subprocess blocking.

## Key Differences from Wifite2
* **Dependency Pruning:** Wifite2 relied heavily on `airodump-ng`, `aireplay-ng`, etc., for core loops. Wifit3 strips this away completely; all wireless card interactions are done by sending & receiving raw bytes sent via Pyusb.
* **Event-Driven Architecture:** Instead of sequential, blocking attack flows, Wifit3 uses an asynchronous, event-driven model. A background `Scanner` emits events (e.g., `BeaconReceived`, `HandshakeCaptured`), and the Textual TUI reacts instantly.
* **Standalone Migration:** This is not an in-place upgrade to Wifite2. It is a new project designed to extract the domain knowledge from v2 without inheriting its structural baggage.

## Proposed Architecture

*Note: Potentially outdated; subject to change at anytime.*

### 1. Interface Manager (`wifit3.interface`)
OS-agnostic abstraction for network cards.
* Handles identifying wireless interfaces.
* Puts interfaces into Monitor Mode
* Handles Channel Hopping (aside: Scapy can't hop channels directly on Windows).

### 2. Core Engine (`wifit3.engine`)
* **Scanner:** An `asyncio`-compatible loop using Scapy to sniff `Dot11Beacon` and `Dot11ProbeReq/Resp` frames. Maintains an internal state of Targets and Clients.
* **Capture Manager:** Listens for EAPOL frames, PMKIDs, and manages the writing of clean `.pcap` files directly in native Python.
* **Injector:** Handles crafting and sending `Dot11Deauth` frames natively.

### 3. Textual TUI (`wifit3.ui`)
* A full-screen application managing state centrally.
* Features: Theming engine, ASCII art headers, dynamic data tables for Targets, and dedicated "Attack Views" that update via async messages from the Core Engine.

## Modern Attack Landscape (2026 Context)
*To be expanded during implementation:*
* **WPA3 / SAE:** Requires research into native Scapy implementation for SAE handshake capture/downgrade detection. Wifite2 used `hcxdumptool`; Wifit3 needs a native approach.
* **Evil Twin:** Usually requires `hostapd` and `dnsmasq`. We need to determine if we continue shelling out for these specific daemons or if Scapy can act as a lightweight soft-AP.
* **WPS Pixie Dust:** Determine if the market share of vulnerable routers in 2026 warrants prioritizing this, or if we focus purely on WPA2/WPA3 PMKID/Handshakes first.

## (Legacy) Implementation Phases

*Note: Potentially outdated & subject to change at any time.*

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
