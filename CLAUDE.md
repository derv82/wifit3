# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Session Cheatsheet

- **Platform**: Dev machine is Windows + PowerShell. Bash tool is available, but `scratch/AGENTS.md` has PowerShell-isms worth respecting (no `&&` chaining, no Unix `grep`/`tail`, never pass `-c` to `tshark` since it limits *input* not output).
- **Cross-platform by design**: Wifit3 uses PyUSB + `libusb_package` so drivers run on Windows (with Zadig binding the device to WinUSB) AND Linux (after `rmmod <kernel_driver>`). No Kali boot is needed for normal dev — that's the whole point of going userland.
- **Hardware testing is the USER's job**, not the agent's. The loop is:
  1. Agent proposes code changes.
  2. User runs `python scratch/test_hw_mt7921au.py` (optionally `--debug`) and pastes output.
  3. Agent reads output, iterates.
  Do not try to flash/test hardware yourself.
- **Device gets borked? User replugs.** That resets cold-boot state. You can suggest "please unplug, wait a few seconds, replug, then rerun" if a previous attempt left it stuck.
- **Reverse-engineering workflow**: when porting a kernel driver, the kernel C is the spec but `usb_dumps/captures_*/capture-N.pcap` is the ground truth. Use the deterministic helpers in `scratch/` instead of ad-hoc tshark queries:
  - `python scratch/pcap_slicer.py <main.log> <pcap>` — maps `capture.py` log timestamps to pcap frame ranges (e.g. "firmware upload happens in frames 14182–14400").
  - `python scratch/source_intel.py data_dumps/<chip-source>/ <hex_or_token>` — locates register/macro definitions in the kernel source, with parent-register context.
  - See `scratch/AGENTS.md` for the full tooling brief.
- **Captures are made by `src/wifit3/scripts/capture.py`** on the Kali persistent USB. Each capture comes with a `*_logs/main.log` (absolute-epoch timeline) that `pcap_slicer.py` consumes.
- **Per-chipset ground-truth docs**: each chip dir has a `<CHIP>.md` (e.g. `chips/mt7921au/MT7921AU.md`) that accumulates *verified* facts decoded from its pcap. Treat anything not in that doc as a hypothesis. Update the doc as facts are confirmed so future sessions don't re-derive them.
- **Lead's rule** (from `NEXT-STEPS.md`): discuss class design (`GenericDriver` vs `WlanInterface` responsibilities, etc.) BEFORE execution. Treat the user as Senior Lead.
- **Other top-level docs** (NOT auto-loaded — open as needed): `DESIGN.md`, `NEXT-STEPS.md`, `QUIRKS.md`, `SOFT-MAC.md`, `WPA3-Frames.md`, `WPA3-SAE-Group-Detection.md`, `planning/OSX-SUPPORT.md`.

## Commands

```bash
# Install (editable, with dev deps)
pip install -e ".[dev]"   # or: uv sync --group dev

# Run
python -m wifit3

# Tests
pytest                          # all tests
pytest tests/chips/ar9271/      # single module
pytest tests/wlan/test_parser.py::TestWlanFrameParser::test_beacon

# Lint / format
ruff check src/
ruff format src/

# Textual live dev (hot-reload)
textual run --dev src/wifit3/ui/app.py
```

Tests require no hardware — all USB interactions are mocked via `pytest-mock`. `asyncio_mode = "auto"` is set globally, so async tests require no decorator.

## Architecture Overview

Wifit3 is a userland 802.11 auditing tool. It communicates directly with USB wireless cards via **PyUSB** — no `aircrack-ng` subprocess wrappers, no Scapy. The TUI is built on **Textual**.

### Layer Stack (top to bottom)

```
ui/                      Textual screens (Splash → Scanner → Focus)
  └─ WifiteApp           Central app; holds WlanDeviceManager + active interface

wlan/
  ├─ WlanDeviceManager   Scans USB bus, maps VID:PID → driver class, returns WlanInterface
  ├─ WlanInterface       Device-agnostic 802.11 abstraction consumed by UI
  │    - channel hopping (asyncio task)
  │    - AP/Client registry (built from parsed frames)
  │    - Handshake tracking (EAPOL M1/M2 pair via replay counter)
  └─ WlanFrameParser     Pure-Python 802.11 frame parser (no Scapy)

engine/
  ├─ models.py           Pydantic: AccessPoint, Client, Handshake
  ├─ protocols.py        WlanDriver structural Protocol (typing contract for drivers)
  └─ attacks/            Attack implementations (SAE probe, etc.)

chips/<chipset>/
  ├─ driver.py           Implements WlanDriver Protocol
  ├─ transport.py        Raw USB read/write + async RX loop
  ├─ firmware.py         Firmware upload logic
  ├─ constants.py        Register addresses, command IDs, magic bytes
  └─ assets/ or sequences/
       init.py / tuning.py   Captured USB register-write sequences (replayed at runtime)
```

### Supported Hardware

| Chip | VID:PID | Architecture |
|------|---------|--------------|
| Atheros AR9271 | 0cf3:9271 | Soft-MAC — WMI over HTC, Big Endian, strict Sequence IDs |
| Realtek RTL8187L | 0bda:8187 | Hard-MAC — USB control transfers |
| Ralink RT5572 | 148f:5572 | rt2800usb family |
| Ralink RT3572 | 148f:3572 | rt2800usb family |
| Ralink RT5372 | 148f:5372 | rt2800usb family |
| Mediatek MT7921AU | 0e8d:7961 | WiFi 6 — MCU Unified Commands, Little Endian |

RT2800USB variants share `RT2800USBDriver`; the `chip_id` string selects the correct RXWI/TXWI sizes and init assets.

### Adding a New Chipset

1. Create `src/wifit3/chips/<name>/` with `driver.py`, `transport.py`, `firmware.py`, `constants.py`.
2. `driver.py` must satisfy `WlanDriver` in `wifit3.engine.protocols`.
3. Register VID:PID → driver class in `WlanDeviceManager.SUPPORTED_DEVICES`.

### AR9271 Protocol Notes (Soft-MAC)

Channel changes require replaying a ~500-byte WMI_REG_WRITE (0x15) sequence with dynamically injected Sequence IDs, then patching register `0x9874` (AR_PHY_SYNTH_CONTROL) with the Fractional-N Synthesizer Word for the target channel. Static PCAP replay fails because the firmware rejects out-of-order Sequence IDs. See `SOFT-MAC.md` and `QUIRKS.md` for byte offsets and the WMI header format.

### MT7921AU Protocol Notes

WiFi 6, little-endian, `mt76` family. The scaffolding under `chips/mt7921au/` is unverified — see `src/wifit3/chips/mt7921au/MT7921AU.md` for facts confirmed against `usb_dumps/captures_mt7921u/capture-3.pcap`. Kernel source under `data_dumps/mt76-source-v6.18/`.

### Frame Flow (RX)

```
transport._rx_loop()
  → driver._on_raw_rx()       strips hardware descriptor (RXD/RXWI), extracts RSSI
  → WlanFrameParser.parse_80211_frame()   returns dict
  → WlanInterface._on_frame_parsed()      updates AP/Client registry
  → UI ScannerView polls interface.get_access_points() via Textual timer
```

### TUI Screens

- **SplashView** — USB device discovery, driver progress, interface selection
- **ScannerView** — Live AP table; triggers channel hopping; leads to FocusView
- **FocusView** — Single-target attack panel (deauth, handshake capture)
