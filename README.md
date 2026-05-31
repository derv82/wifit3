# Wifit3 — Wireless Auditor

A cross-platform Wi-Fi auditing tool with a terminal UI. Wifit3 talks to USB
Wi-Fi adapters **directly over USB** (PyUSB) — no `aircrack-ng`/`airmon-ng`
subprocesses, no Scapy — so it runs the same on **Linux and Windows**.

<p align="center">
  <img src="screenshots/wifit3-1-splash.png" alt="Wifit3 splash / adapter picker" width="700">
</p>

## Features

- **Live scan** — APs and clients with channel hopping, signal, encryption,
  WPS state, and WPA3/SAE detection.
- **WPA/WPA2 handshakes** — passive 4-way capture and deauth-triggered capture,
  proper handshake validation, minimalist PCAP saves.
- **PMKID** — passive capture and active harvest, saves as HashCat .hc22000 files.
- **WPS vectors**: Passive/active PushButton invasion, resumable brute-force sessions.
- **WEP suite** — ARP replay, ChopChop, fragmentation, fake auth, PTW key recovery.
- **Cross-platform** — one codebase on Linux and Windows.

## Screenshots

| Scanner | Focus (single target) |
|---|---|
| ![Scanner](screenshots/wifit3-2-scanner.png) | ![Focus](screenshots/wifit3-3-focus.png) |

## Supported hardware

| Card | Chipset | Bands |
|---|---|---|
| ALFA AWUS036NHA | Atheros AR9271 | 2.4 GHz |
| ALFA AWUS036ACS | Realtek RTL8821AU | 2.4 / 5 GHz |
| ALFA AWUS036ACH | Realtek RTL8812AU * | 2.4 / 5 GHz |
| ALFA AWUS1900 | Realtek RTL8814AU | 2.4 / 5 GHz |
| TP-Link T3U Plus | Realtek RTL8822BU * | 2.4 / 5 GHz |
| TP-Link TL-WN722N v2/v3 | Realtek RTL8188EUS | 2.4 GHz |
| ALFA AWUS036ACM | MediaTek MT7612U | 2.4 / 5 GHz |
| ALFA AWUS036ACHM | MediaTek MT7610U | 2.4 / 5 GHz |
| (various) | Realtek RTL8187 | 2.4 GHz |
| Panda PAU05 / PAU09 N600 | Ralink RT2800USB (RT5372 / RT5572 / RT3572) * | 2.4 / 5 GHz |
| Buffalo Nintendo Wi-Fi USB Connector | Ralink RT2500USB / RT2570 * | 2.4 GHz |

\* Known limitation — see [VERIFICATION.md](VERIFICATION.md). The absence of an
asterisk means *no known issue*, not that every attack has been verified on that
card — see the matrix for the full per-attack status.

## Philosophy

**Zero dependencies.** Wifit3 implements the whole stack itself — the USB device
drivers (ported from the Linux kernel), 802.11 frame parsing, the crypto, and
the WEP attacks (ported from aircrack-ng) — instead of shelling out to
`airmon-ng`, `aireplay-ng`, `tshark`, or `hcxdumptool`. wifite2 ran a binary
dependency check on every startup; Wifit3 has nothing to check. That's what
keeps it portable and robust: no PATH probing, no scraping another tool's
stdout, no breakage when an external tool changes its output.

**Cross-platform.** The bytes sent to the card are OS-agnostic, so one codebase
runs on **Linux and Windows** — point the adapter at the USB stack Wifit3 talks
through (WinUSB via Zadig on Windows; unbind the kernel driver on Linux) and go.
No VM, no Kali boot.

**Native and responsive.** No Scapy (it's heavy and triggers a UAC prompt on
every import on Windows); no blocking subprocesses. A Textual TUI that updates
live via async messages — scan, pick a target, attack, save — instead of polling
another process's output.

**A fresh start, not a fork.** Wifit3 is a clean-slate reimagining, not an
in-place upgrade. It extracts the domain knowledge from wifite/wifite2 without
inheriting the baggage of a tool architected as an aircrack-ng wrapper.

## Installation

Wifit3 uses [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
uv run wifit3
```

**Linux** — unload the kernel driver so Wifit3 can claim the adapter:

```bash
sudo rmmod <kernel_driver>   # e.g. ath9k_htc, rtl8xxxu, mt76x2u, rt2800usb
```

**Windows** — install the **WinUSB** driver for your adapter with
[Zadig](https://zadig.akeo.ie/) first; Wifit3 won't see it otherwise. Once
WinUSB is set up you don't need to run Wifit3 as Administrator.

## Disclaimer

For use only on networks you own or are explicitly authorized to test.
