# wifit3: USB Wireless Auditor

> A wireless auditor that runs on Linux and Windows, comes with its own built-in drivers.

> *At least* one of the [supported USB adapters](#supported-hardware) is **required**.

<p align="center">
  <img src="assets/wifit3-1-splash.png" alt="Wifit3 splash / adapter picker" width="700">
</p>

<p align="center">
  <img src="assets/wifit3-demo.gif" alt="Wifit3 in action: WPS PushButton PSK capture" width="700">
</p>

wifit3 is fundamentally different from its predecessor, [wifite2](https://github.com/derv82/wifite2):

* Only supports certain popular USB cards (see [Supported Hardware](#supported-hardware)).
* Bundles its own driver stack (see [Mini-Drivers](#mini-drivers)), avoiding headaches with native wireless drivers (Windows NDIS, Linux driver conflicts).
* Talks to wireless cards directly from userland *after* setup.
   * `sudo` is required to set up permissions on Linux (udev/modprobe).
   * Admin is required to install [WinUSB drivers](https://learn.microsoft.com/en-us/windows-hardware/drivers/usbcon/introduction-to-winusb-for-developers) on Windows (automated).
   * After setup/install, wifit3 runs without privilege escalation.
* *Far* fewer dependencies: PyUSB/libusb (USB) and Textual/Rich (TUI).
  * No aircrack, airmon, reaver, bully, hcxdumptool, etc.

## Status: Beta *("Works On My Machine")*

[Thoroughly tested](docs/SUPPORTED-HARDWARE.md) only on my own machine, with the cards I physically own.

Other wireless cards with a supported chipset may not behave as expected.

Bug reports are genuinely welcome: [open an issue](https://github.com/derv82/wifit3/issues).

## Features

- **Multi-card**: listen on every plugged-in and supported device, improves capturing; TX device selection.
- **Live scan**: lists Access Points (APs) with channel hopping, signal, encryption, WPS state, and WPA3/SAE detection.
- **VAP Decloaking**: identifies and tags hidden Virtual APs (VAPs) with its physical AP.
- **Live packet dashboard**: real-time traffic sparklines (beacons, data, injects, deauths) for the focused target.
- **PMKID**: passive capture and active harvest, saves as HashCat `.hc22000` filetype.
- **WPA/WPA2 handshakes**: passive 4-way capture and deauth-triggered capture, proper handshake validation, compact PCAP and `.hc22000` saves.
- **WPS PushButton Extraction**: detects when an AP's WPS button is pressed, automatically extracts PSK.
- **WPS PIN Brute-force**: resumable WPS PIN brute-force sessions.
- **WEP suite**: ARP replay, ChopChop, fake auth, PTW key recovery. For anyone trapped in 2006.
- **WiFFy**: helpful assistant that provides useful messages during the WinUSB installation process.

## Screenshots

| Scanner | Focus (single target) |
|---|---|
| ![Scanner](assets/wifit3-2-scanner.png) | ![Focus](assets/wifit3-3-focus-handshake.png) |

## Supported hardware

*If your USB device is not listed there, wifit3 will not work with it.*

A matching chipset does not guarantee that your wireless card will work.

| Chipset | Bands | Cards (Make + Model) |
|---|---|---|
| Atheros AR9271 | 2.4 GHz | ALFA AWUS036**NHA**, TP-Link TL-WN722N V1 |
| MediaTek MT7610U | 2.4 / 5 GHz | ALFA AWUS036**ACHM**, Panda PAU0B |
| MediaTek MT7612U | 2.4 / 5 GHz | ALFA AWUS036**ACM** |
| MediaTek MT7921AU | 2.4 / 5 GHz | ALFA AWUS036**AXML**, Panda PAU0F |
| MediaTek MT7925U | 2.4 / 5 GHz | Netgear A9000 |
| Realtek RTL8812AU | 2.4 / 5 GHz | ALFA AWUS036**ACH** |
| Realtek RTL8814AU | 2.4 / 5 GHz | ALFA AWUS1900 |
| Realtek RTL8821AU | 2.4 / 5 GHz | ALFA AWUS036**ACS**, TP-Link Archer T2U Plus/Nano |
| Realtek RTL8821CU | 2.4 / 5 GHz | Auscoumer 600 Mbps |
| Realtek RTL8922AU | 2.4 / 5 GHz | ASUS USB-BE93 |
| Realtek RTL8822BU | 2.4 / 5 GHz | TP-Link T3U Plus |
| Realtek RTL8187L | 2.4 GHz | ALFA AWUS036**H** |
| Realtek RTL8188EUS | 2.4 GHz | TP-Link TL-WN722N v2/v3 |
| Ralink RT2570 | 2.4 GHz | Buffalo Nintendo Wi-Fi USB Controller |
| Ralink RT3070 | 2.4 GHz | ALFA AWUS036**NH** |
| Ralink RT5370 | 2.4 GHz | LOTEKOO 150 Mbps |
| Ralink RT5372 | 2.4 GHz | Panda PAU05/PAU06 |
| Ralink RT5572 | 2.4 / 5 GHz | Panda PAU09 N600 |

See [Supported Hardware](docs/SUPPORTED-HARDWARE.md) for detailed information about each card's capabilities and performance.

## Install

### Download (recommended)

Grab a prebuilt binary from the [**Releases**](https://github.com/derv82/wifit3/releases/latest)

- **Windows** — download `wifit3-windows-x64.exe` and run it.
- **Linux** — download `wifit3-linux-x64`, then `chmod +x wifit3-linux-x64 && ./wifit3-linux-x64`.
- **macOS (Apple Silicon + Intel):**
  1. Download `wifit3-macos-universal2`
  2. Bypass quarantine: `xattr -d com.apple.quarantine wifit3-macos-universal2 && chmod +x wifit3-macos-universal2`
  3. Run it: `./wifit3-macos-universal2`

### Run from source

Wifit3 uses [`uv`](https://docs.astral.sh/uv/) (requires internet access to pull dependencies for the first run):

```
uv sync
uv run wifit3
```

### Build

Build using `uv run pyinstaller wifit3.spec --noconfirm --clean` (Windows: `dist/wifit3.exe`, Linux/OSX: `dist/wifit3`).

### First-run setup

**Windows**: Wifit3 offers to install the **WinUSB** driver for your device. The bundled installer
self-elevates for that one step (a single UAC prompt), after which no Administrator privileges are needed to run Wifit3.

**Linux**: Wifit3 offers to create udev and modprobe rules which enable userland access. These rules blocklist 
the card's kernel driver (so the kernel stops grabbing it). Afterward Wifit3 runs without `sudo`.

**macOS**: No driver install is needed. macOS asks to allow the USB device on first plug-in: choose
*Allow*, afterwards wifit3 can see & interact with the device.

### Uninstall

Click the red `Uninstall` button on Wifit3's Splash screen to uninstall
* **Windows:** Uninstalls WinUSB driver, relinquishing control to Windows' installed driver.
* **Linux:** Deletes udev & modprobe rules, kernel assumes control of the driver after a replug.

## Thanks

Wifit3 only exists because of the people who reverse-engineered and maintained the Linux
drivers we ported from.

**Biggest thanks: Christian "kimo" B. ([@kimocoder](https://github.com/kimocoder))**, who
took over **wifite2** when its original maintainer (me) stepped away and has kept it alive and
evolving for years since (and maintains `aircrack-ng`'s RTL8188EUS DKMS driver, which we port here).

**Special thanks: Sandman**, close friend and the master to my Linux & wireless-hacking apprenticeship.

A few more of the driver authors we ported from:

- **Nick Morrow** ([@morrownr](https://github.com/morrownr)) — the out-of-tree Realtek USB
  DKMS drivers (RTL8812AU / RTL8814AU / RTL8821AU / RTL8822BU) that keep these cards alive.
- **Stanislaw Gruszka**, **Ivo van Doorn**, and the **rt2x00** team — the Ralink drivers.
- **Lorenzo Bianconi** and **Felix Fietkau** — MediaTek `mt76`.
- **Sujith Manoharan** and the **ath9k** team; **Bitterblue Smith** and the Realtek **rtw88** team.

The full list (every substantive contributor to the drivers we ported, and the cards they
enabled) is in **[CREDITS.md](docs/CREDITS.md)**.

## Mini-Drivers

wifit3 talks to the wireless cards directly over USB through
[its built-in "Mini-Drivers"](https://github.com/derv82/wifit3/blob/master/src/wifit3/chips/):
miniature userland ports of the Linux kernel drivers. These ports only include the bare minimum
needed for RX and TX in Monitor Mode (no AP/STA modes).

This sidesteps the operating system's wireless stack completely, including Windows' NDIS
(Network Driver Interface Specification), which would otherwise block Monitor Mode and
injection. The bytes sent to the card are the same on either OS, so a single codebase runs
on both Linux and Windows.

Mini-Drivers also enables wifit3's multi-card feature: Plug in multiple (supported)
wireless devices and wifit3 will "cross the streams", improving the chances of capturing
packets and overall RX.

### Ported from C to Python by a coding agent

The Mini-Drivers were ported from their Linux C drivers by a coding agent. During development,
the agent is guided by an offline test harness: it replays real USB traffic (recorded from the
Linux wireless driver) against the Python port and halts at the first instruction where the port
diverges from the recording. The agent ports that next sequence, replays, and repeats until the
driver port reproduces the entire recording. Only then is it reasonably safe to try live hardware.

The loop in brief:

1. **Capture once on Linux.** With the Linux kernel driver loaded, record the card's USB traffic
   while `airmon-ng`, `airodump-ng`, and `aireplay-ng` run.
   [capture.py](https://github.com/derv82/wifit3/blob/master/src/wifit3/scripts/capture.py)
   automates the capture (`usbmon` via `tshark`) and pulls the driver's C source.
2. **Start the port:** `/port <chip>` (e.g. `/port rt5370`), a Claude-specific command. The agent
   wires the capture into
   [verify_pcap.py](https://github.com/derv82/wifit3/blob/master/scripts/porting/verify_pcap.py) so the
   capture can be replayed & verified against the new driver without touching the hardware at all.
3. **Port to the recording.** `verify_pcap.py` reports the next USB instruction where the port's
   output diverges from the capture. The agent uses the C source to fix it, replays, and repeats
   until the capture runs clean.
4. **Go live.** With the port proven against the recording, the agent tests on real hardware and
   iterates.

[docs/porting/](docs/porting/) documents the full process.

## License

Wifit3 is licensed under the **GNU General Public License v2.0** (GPL-2.0-only): see
[LICENSE](LICENSE). The userland drivers are ports of GPLv2 Linux kernel and vendor DKMS
drivers, so GPLv2 is the natural fit; the upstream authors are credited in [CREDITS.md](docs/CREDITS.md).

**Source for binary releases.** The prebuilt executables on the Releases page are built from
this repository. The complete corresponding source for any released binary is this repository
at its matching version tag. GPLv2 §3 is satisfied by offering source from the same place the
binary is offered.

**Firmware is not GPL.** The vendor firmware blobs that Wifit3 loads onto the cards are
redistributed verbatim under their own manufacturers' licenses (Realtek / MediaTek / Ralink),
*not* the GPL. Each ships with its license text alongside it; provenance and byte-verification
are documented in [FIRMWARE.md](docs/FIRMWARE.md).

## Disclaimer

For use only on networks you own or are explicitly authorized to test.

⚠️ **Hardware-damage risk.** Wifit3 talks to USB Wi-Fi hardware at the register level, with no
kernel driver between it and the silicon. A bad register write, firmware page, or power sequence
can damage or permanently disable ("brick") a device. **Use at your own risk: there is no
liability for hardware damage.**
