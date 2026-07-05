# Wifit3 — Wireless Auditor

A cross-platform, userland Wi-Fi auditing tool with a terminal UI. Wifit3 talks to USB
Wi-Fi adapters **directly over USB** (PyUSB) — no `aircrack-ng`/`airmon-ng`
subprocesses — so it runs the same on **Linux and Windows**.

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
  proper handshake validation, compact PCAP saves.
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
| Auscoumer 600 Mbps | Realtek RTL8821CU | 2.4 / 5 GHz |
| LOTEKOO 150 Mbps | Ralink RT5370 | 2.4 GHz |

[VERIFICATION.md](VERIFICATION.md) has detailed information about each card's capability and performance.

## Thanks

Wifit3 only exists because of the people who reverse-engineered and maintained the Linux
drivers we ported from.

**Biggest thanks: Christian "kimo" B. ([@kimocoder](https://github.com/kimocoder))** — who
took over **wifite2** when its original maintainer stepped away and has kept it alive and
evolving for years since (and maintains `aircrack-ng`'s RTL8188EUS driver, which we port here).

**Special thanks: Sandman** — close friend, and the master to my Linux & wireless-hacking apprenticeship.

A few more of the driver authors we ported from:

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
taken are PyUSB/LibUSB (USB interfacing) and Textual/Rich (TUI libraries). Everything
else is written in pure Python.

**Cross-platform.** The bytes sent to the card are OS-agnostic, so one codebase
runs on **Linux and Windows**. Point the adapter at the USB stack Wifit3 talks
through and go. No VM, no Kali boot.

**Userland.** Root/Admin is only required during the one-click setup stage.
After that, Wifit3 can be run without privilege (no sudo, no UAC popups).

**Responsive.** No blocking subprocesses. A Textual TUI that updates
live via async messages — scan, pick a target, attack, save — instead of polling
another process's output.

## Installation

Wifit3 uses [`uv`](https://docs.astral.sh/uv/) (requires internet access to pull dependencies
for the first run):

**Windows** — Wifit3 installs the **WinUSB** driver for your adapter itself: pick the
card on the splash screen and confirm. The bundled installer self-elevates for that one
step (a single UAC prompt), after which no Administrator privileges are needed to run Wifit3.

**Linux** — pick the card on the splash and confirm. Wifit3 takes complete control of that
chipset: it blacklists the card's kernel driver (so the kernel stops grabbing it) and grants your
user raw USB access (one privileged prompt), then asks you to replug the card once. While Wifit3
controls it, the card won't work as a normal Wi-Fi adapter; press **✕** on the splash to hand it
back to the kernel. Afterward Wifit3 runs without sudo.

```bash
uv sync
uv run wifit3
```

**Don't want to hand the chipset over?** Run as root against a manually freed card:

```bash
sudo rmmod <kernel_driver>   # e.g. ath9k_htc, rtl8xxxu, mt76x2u, rt2800usb
sudo .venv/bin/python3 -m wifit3
```

## License

Wifit3 is licensed under the **GNU General Public License v2.0** (GPL-2.0-only) — see
[LICENSE](LICENSE). The userland drivers are ports of GPLv2 Linux kernel and vendor DKMS
drivers, so GPLv2 is the natural fit; the upstream authors are credited in [CREDITS.md](CREDITS.md).

**Source for binary releases.** The prebuilt executables on the Releases page are built from
this repository. The complete corresponding source for any released binary is this repository
at its matching version tag — GPLv2 §3 is satisfied by offering source from the same place the
binary is offered.

**Firmware is not GPL.** The vendor firmware blobs Wifit3 loads onto the cards are
redistributed verbatim under their own manufacturers' licenses (Realtek / MediaTek / Ralink),
*not* the GPL. Each ships with its license text alongside it; provenance and byte-verification
are documented in [FIRMWARE.md](FIRMWARE.md).

## Disclaimer

For use only on networks you own or are explicitly authorized to test.

⚠️ **Hardware-damage risk.** Wifit3 talks to USB Wi-Fi hardware at the register level, with no
kernel driver between it and the silicon. A bad register write, firmware page, or power sequence
can damage or permanently disable ("brick") a device. **Use at your own risk — there is no
liability for hardware damage.**