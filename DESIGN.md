# Wifit3 Design Document

## Background & Motivation
Wifit3 is "yet another rewrite of Wifite." 

Wifite1 was a monolithic script. Wifite2 introduced a modular class design but remained architecturally bound to the `aircrack-ng` suite, operating essentially as a complex wrapper that shelled out to subprocesses and scraped `stdout` and CSV files. While functional, this "weak script" feel led to fragility, especially as external tool outputs changed or when integrating modern UI components (like a TUI) that suffered from polling lag.

Wifit3 is a clean-slate reimagining. It aims to shed the legacy dependency hell by handling wireless protocols natively in Python, while providing a beautiful, responsive, and highly customizable Terminal User Interface (TUI).

## Core Tenets
1. **Userland (no sudo):** Users are only required to use Admin priviledges *while setting up their wireless driver to allow interfacing with Pyusb*. **Everything else is in Userland**: raw USB packets sent to the wireles card to enable monitor mode, inject/sniff packets, etc.
  - Note: This is true on Windows. On Linux, however, `sudo` is required to communicate with hardware via `pyusb`.
2. **Cross-Platform:** The bytes sent to the wireless card are platform-agnositic. Windows users have Zadig (WinUSB), Linux users have "rmmod" and "detach_kernel_driver()", OSX users can have "Codeless Kext" to override system driver capture (allegedly).
3. **Native First:** Packet parsing, injection, and handshake verification are handled entirely in native Python via PyUSB.
  - *No Scapy!* Scapy is bloated, causes "User Access Control" popups in Windows on every import.
4. **Textual TUI:** UI/UX uses the `Textual` framework. Expect themes, resizable layouts, and a highly polished, responsive interface unhindered by background subprocess blocking.

## Key Differences from Wifite2
* **Dependency Pruning:** Wifite2 relied heavily on `airodump-ng`, `aireplay-ng`, etc., for core loops. Wifit3 strips this away completely; all wireless card interactions are done by sending & receiving raw bytes sent via Pyusb.
* **Responsive Architecture:** Instead of sequential, blocking attack flows, Wifit3's UI has instant feedback via Wlan interface (+PyUSB).
* **Standalone Migration:** This is not an in-place upgrade to Wifite2. It is a new project designed to extract the domain knowledge from v2 without inheriting its structural baggage.

## Architecture

### Chipset-Specific Logic/Behavior (`wifit3.chips.*`)
* "Mini-drivers" for enabling and interacting with wireless cards via PyUSB.

### Wireless Interface & Device Manager (`wifit3.wlan`)
Device-agnostic abstraction for network cards.
* Handles identification and selection of supported wireless interfaces.
* Puts interfaces into Monitor Mode.
* Handles Channel Hopping.

### Core Engine (`wifit3.engine`)
* Attacks
* Capturing/Saving

### Textual TUI (`wifit3.ui`)
* A full-screen application managing state centrally.
* Features: Theming engine, ASCII art headers, dynamic data tables for Targets, and dedicated "Attack Views" that update via async messages from the Core Engine.

## Modern Attack Landscape
*To be expanded during implementation:*
* **WPA3 / SAE:** Requires research into SAE handshake capture/downgrade detection.
* **hcxdumptool:** Wifite2 used `hcxdumptool` for extracting PMKID/handshakes; Wifit3 needs a native approach.
* **Evil Twin:** Usually requires `hostapd` and `dnsmasq`. Wifit3 would require supporting chipsets of both cards.
* **WPS Pixie Dust:** Determine if the market share of vulnerable routers in 2026 warrants prioritizing this, or if we focus purely on WPA2/WPA3 PMKID/Handshakes first.
