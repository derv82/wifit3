# Wifit3: USB Wireless Auditor

A USB-only wireless auditor that runs in userland on Linux and Windows.

<p align="center">
  <img src="screenshots/wifit3-1-splash.png" alt="Wifit3 splash / adapter picker" width="700">
</p>

<p align="center">
  <img src="screenshots/wifit3-demo.gif" alt="Wifit3 in action — WPS PushButton PSK capture" width="700">
</p>

wifit3 is fundamentally different from its predecessor, [wifite2](https://github.com/derv82/wifite2):

* wifit3 embeds its entire driver stack (see [Minnie Drivers](#minnie-drivers)), so it works even when *conflicting* wireless drivers are installed on your operating system. No more driver juggling!
* One codebase runs the same on both Linux *and Windows*.
* Only certain (popular) USB cards are supported; non-USB wireless cards are not supported at all.
* No external wireless tooling: no dependencies on airmon, airodump, aircrack, tshark, etc.
   * As few dependencies as reasonably possible (we're not porting hashcat!). Just two stacks: USB (PyUSB/libusb) and TUI (Textual/Rich).
* wifit3 talks to wireless cards directly from userland (no need to run as sudo/Admin).
   * *Installing* the udev/modprobe permissions (Linux) and WinUSB driver (Windows) does require one-time sudo/Administrator privileges. Afterwards, everything runs from userland.
   * This install step is built into wifit3, which asks for privileges only when needed.

## Features

- **Live scan**: APs and clients with channel hopping, signal, encryption,
  WPS state, and WPA3/SAE detection.
- **WPA/WPA2 handshakes**: passive 4-way capture and deauth-triggered capture,
  proper handshake validation, compact PCAP saves.
- **PMKID**: passive capture and active harvest, saves as HashCat .hc22000 files.
- **WPS vectors**: Passive/active PushButton invasion, resumable brute-force sessions.
- **WEP suite**: ARP replay, ChopChop, fake auth, PTW key recovery.
- **Live packet dashboard**: real-time per-class sparklines (beacons, data, injects,
  deauths) for the focused target.

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

## Installation

Wifit3 uses [`uv`](https://docs.astral.sh/uv/) (requires internet access to pull dependencies
for the first run):

**Windows** — Wifit3 installs the **WinUSB** driver for your adapter itself: pick the
card on the splash screen and confirm. The bundled installer self-elevates for that one
step (a single UAC prompt), after which no Administrator privileges are needed to run Wifit3.

**Linux** — pick the card on the splash and confirm. Wifit3 assumes control of that
chipset: it blocklists the card's kernel driver (so the kernel stops grabbing it) and grants your
user raw USB access (one privileged prompt), then asks you to replug the card once.
Afterward Wifit3 runs without sudo. While Wifit3 controls it, the card won't work as a normal Wi-Fi
adapter; press **✕** ("uninstall") on the splash to revert permissions so the kernel can use it again.

```bash
uv sync
uv run wifit3
```

**Don't want to hand the chipset over?** Run as root against a manually freed card:

```bash
sudo rmmod <kernel_driver>   # e.g. ath9k_htc, rtl8xxxu, mt76x2u, rt2800usb
sudo .venv/bin/python3 -m wifit3

# Replug afterward to revert to give back control to the kernel's driver.
```

## Thanks

Wifit3 only exists because of the people who reverse-engineered and maintained the Linux
drivers we ported from.

**Biggest thanks: Christian "kimo" B. ([@kimocoder](https://github.com/kimocoder))** — who
took over **wifite2** when its original maintainer (me) stepped away and has kept it alive and
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

## Minnie Drivers

Wifit3 talks to the wireless cards directly via USB using its built-in "Minnie Drivers":
miniature userland ports of the Linux drivers. They're *miniature* on purpose: there's no STA (client)
mode and no AP mode, just the bare minimum to get RX and TX working in Monitor Mode.

Talking to the card this way bypasses the operating system's wireless driver stack entirely,
including Windows' NDIS (Network Driver Interface Specification), which would otherwise prevent
functionality such as Monitor Mode and injection. And because the bytes sent to the card are the
same regardless of OS, one codebase runs on both Linux and Windows.

### Agent-Driven Driver Porting

The Minnie Drivers were ported from C to Python by a coding agent (Anthropic's Claude Code), not
by hand. I want to be upfront about this: if you maintain one of the upstream drivers and
something in the port looks wrong, that's exactly the feedback I want so I can fix it.

Every port is proven against a pcap-replay test harness that checks the driver reproduces the
captured USB instructions byte-for-byte, entirely offline, before any hardware is involved. The
harness is what keeps the port correct; the agent just does the work inside it.

The porting process assumes you have Anthropic's Claude Code. The steps:

1. Capture all USB traffic on a Linux machine that has the target wireless driver installed (along
   with airmon-ng and aircrack-ng). [capture.py](https://github.com/derv82/wifit3/blob/master/src/wifit3/scripts/capture.py)
   is an automated script that walks you through it.
2. Ask Claude to port the driver with the `/port` command, e.g. `/port rt5370`.
3. Claude asks for the location of the captured traffic (.pcap and .log files).
4. Claude updates the pcap-replay test harness (see [verify_pcap.py](https://github.com/derv82/wifit3/blob/master/scripts/verify_pcap.py))
   so the new driver can be tested offline for correctness.
   1. The harness means hardware is not required during porting.
5. Once the driver is complete and proven correct offline, Claude asks to test live on real
   hardware (card plugged in).
   1. Claude verifies behavior (beacons are seen, RX does not degrade), troubleshoots any issues.
   2. Claude asks you to test injection/TX (deauths). The agent is instructed to never inject
      packets itself.

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

-----

*wifit3 is the end of a 20-year arc that started with a borrowed Slackware laptop and a neighbor's WEP network. [The full story →](FULL-CIRCLE.md)*