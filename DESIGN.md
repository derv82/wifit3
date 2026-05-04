# Wifit3 Design Document

## Background & Motivation
Wifit3 is "yet another rewrite of Wifite." 

Wifite1 was a monolithic script. Wifite2 introduced a modular class design but remained architecturally bound to the `aircrack-ng` suite, operating essentially as a complex wrapper that shelled out to subprocesses and scraped `stdout` and CSV files. While functional, this "weak script" feel led to fragility, especially as external tool outputs changed or when integrating modern UI components (like a TUI) that suffered from polling lag.

Wifit3 is a clean-slate reimagining. It aims to shed the legacy dependency hell by handling wireless protocols natively in Python, while providing a beautiful, responsive, and highly customizable Terminal User Interface (TUI).

## Core Tenets
1. **Native First:** Packet parsing, injection, and handshake verification are handled entirely in native Python via Scapy. 
2. **Textual TUI:** A primary focus on UI/UX using the `Textual` framework. Expect themes, resizable layouts, ASCII art, and a highly polished, responsive interface unhindered by background subprocess blocking.
3. **Cross-Platform (Second):** While Linux remains the primary target, the architecture will abstract interface controls to allow Windows (via NPcap) and macOS support where hardware/driver combinations permit. If cross-platform proves too hostile regarding Monitor Mode drivers, a native Linux-only Wifit3 is still a massive net win.

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
1. **Phase 1: MVP Scanner & TUI:** 
   * Basic scaffolding of the `Textual` TUI.
   * Implement a native Scapy sniffer listening on a single channel to populate the TUI with Access Points in real-time (no polling delay).
   * Verify Npcap compatibility/Monitor Mode capabilities on Windows without getting blocked by channel hopping implementations.
2. **Phase 2: Interface & Channel Hopping:** OS-agnostic Interface Manager (getting a card into Monitor Mode automatically and implementing channel hopping for Linux and Windows).
3. **Phase 3: Attack Workflows:** Implementing Native Deauths and Handshake/PMKID capturing.
4. **Phase 4: Polish & Export:** Theming, PCAP exporting, cracking handoffs (passing clean captures to Hashcat), and expanding attack logic (Evil Twin/WPA3).
