# Wifit3 — Wireless Auditor

A cross-platform, userland Wi-Fi auditing tool with a terminal UI. Wifit3 talks to USB
Wi-Fi adapters **directly over USB** (PyUSB) — no `aircrack-ng`/`airmon-ng`
subprocesses, no Scapy — so it runs the same on **Linux and Windows**.

<p align="center">
  <img src="screenshots/wifit3-1-splash.png" alt="Wifit3 splash / adapter picker" width="700">
</p>

<p align="center">
  <img src="screenshots/wifit3-demo.gif" alt="Wifit3 in action — WPS PushButton PSK capture" width="700">
</p>

## Features

- **Live scan** — APs and clients with channel hopping, signal, encryption,
  WPS state, and WPA3/SAE detection.
- **WPA/WPA2 handshakes** — passive 4-way capture and deauth-triggered capture,
  proper handshake validation, minimalist PCAP saves.
- **PMKID** — passive capture and active harvest, saves as HashCat .hc22000 files.
- **WPS vectors**: Passive/active PushButton invasion, resumable brute-force sessions.
- **WEP suite** — ARP replay, ChopChop, fake auth, PTW key recovery.
- **Live packet dashboard** — real-time per-class sparklines (beacons, data, injects,
  deauths) for the focused target.
- **Cross-platform** — one codebase on Linux and Windows.

### Limitations

- **USB Only** - Embedded wireless devices and PCI are not supported *at all*. No plans to support those.
- **Limited Hardware Support** - Only the devices listed in *Supported hardware* will work with Wifit3.
  If your USB device is not listed there, Wifit3 will not work with it.

## Screenshots

| Scanner | Focus (single target) |
|---|---|
| ![Scanner](screenshots/wifit3-2-scanner.png) | ![Focus](screenshots/wifit3-3-focus-handshake.png) |

## Supported hardware

| Card | Chipset | Bands |
|---|---|---|
| ALFA AWUS036**NHA** | Atheros AR9271 | 2.4 GHz |
| ALFA AWUS036**ACS** | Realtek RTL8821AU | 2.4 / 5 GHz |
| ALFA AWUS036**ACH** | Realtek RTL8812AU | 2.4 / 5 GHz |
| ALFA AWUS036**ACM** | MediaTek MT7612U | 2.4 / 5 GHz |
| ALFA AWUS036**ACHM** | MediaTek MT7610U | 2.4 / 5 GHz |
| ALFA AWUS036**AXML** / Panda PAU0F | MediaTek MT7921AU | 2.4 / 5 GHz |
| ALFA AWUS036**H** | Realtek RTL8187L | 2.4 GHz |
| ALFA AWUS036**NH** | Ralink RT3070 | 2.4 GHz |
| ALFA AWUS1900 | Realtek RTL8814AU | 2.4 / 5 GHz |
| TP-Link T3U Plus | Realtek RTL8822BU | 2.4 / 5 GHz |
| TP-Link TL-WN722N v2/v3 | Realtek RTL8188EUS | 2.4 GHz |
| Panda PAU05 / PAU06 | Ralink RT5372 | 2.4 GHz |
| Panda PAU09 N600 | Ralink RT5572 | 2.4 / 5 GHz |
| Buffalo Nintendo Wi-Fi | Ralink RT2570 | 2.4 GHz |

[VERIFICATION.md](VERIFICATION.md) has detailed information about each card's capability and performance.

## Thanks

Wifit3 only exists because of the people who reverse-engineered and maintained the Linux
drivers we ported from.

**Biggest thanks: Christian "kimo" B. ([@kimocoder](https://github.com/kimocoder))** — who
took over **wifite2** when its original maintainer stepped away and has kept it alive and
evolving for years since (and maintains `aircrack-ng`'s RTL8188EUS driver, which we port here).

**Special thanks: Sandman** — close friend, and the master to my Linux & wireless-hacking apprenticeship.

A few more of the giants whose shoulders we stand on:

- **Nick Morrow** ([@morrownr](https://github.com/morrownr)) — the out-of-tree Realtek USB
  DKMS drivers (RTL8812AU / RTL8814AU / RTL8821AU / RTL8822BU) that keep these cards alive.
- **Stanislaw Gruszka**, **Ivo van Doorn**, and the **rt2x00** team — the Ralink drivers.
- **Lorenzo Bianconi** and **Felix Fietkau** — MediaTek `mt76`.
- **Sujith Manoharan** and the **ath9k** team; **Bitterblue Smith** and the Realtek **rtw88** team.

The full list — every substantive contributor to the drivers we ported, and the cards they
enabled — is in **[CREDITS.md](CREDITS.md)**.

## Philosophy

**(Almost) Zero dependencies.** Wifit3 implements the whole stack itself — the USB device
drivers (ported from the Linux kernel), 802.11 frame parsing, the crypto, and
the WEP attacks (ported from aircrack-ng) — instead of shelling out to
`airmon-ng`, `aireplay-ng`, `tshark`, or `hcxdumptool`. The only runtime dependencies we have
taken are PyUSB/LibUSB (USB interfacing) and Textual/Rich (modern TUI libraries). Everything
else is written in native Python.

**Cross-platform.** The bytes sent to the card are OS-agnostic, so one codebase
runs on **Linux and Windows** — point the adapter at the USB stack Wifit3 talks
through and go. No VM, no Kali boot.

**Userland.** Root/Admin is only required during the one-click setup stage.
After that, Wifit3 can be run without privilege (no sudo, no UAC popups).

**Native and responsive.** No Scapy (it's heavy and triggers a UAC prompt on
every import on Windows); no blocking subprocesses. A Textual TUI that updates
live via async messages — scan, pick a target, attack, save — instead of polling
another process's output.

## Installation

Wifit3 uses [`uv`](https://docs.astral.sh/uv/) (requires internet access to pull dependencies
for the first run):

**Windows** — Wifit3 installs the **WinUSB** driver for your adapter itself: pick the
card on the splash screen and confirm. The bundled installer self-elevates for that one
step (a single UAC prompt), after which no Administrator privileges are needed to run Wifit3.

**Linux** — Wifit3 installs (with your permission) a `udev` rules file to enable userland access
to the supported wireless cards. This is a one-time privileged (root) action. Afterward,
Wifit3 can be run without sudo.

```bash
uv sync
uv run wifit3
```

**Linux Users**: If you don't want to install the `udev` rules file, you can still run Wifit3 as root:

```bash
sudo rmmod <kernel_driver>   # e.g. ath9k_htc, rtl8xxxu, mt76x2u, rt2800usb
sudo .venv/bin/python3 -m wifit3
```

## Disclaimer

For use only on networks you own or are explicitly authorized to test.
