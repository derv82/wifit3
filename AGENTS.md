# CLAUDE.md

This file provides guidance to coding agents when working with code in this repository.

## Session Cheatsheet

- **Code style**: Outside of `chips/`, comments/docstrings only on public APIs, 2 line maximum (a ceiling not a quota). Don't paper over bad code with a comment: Precise and accurate naming avoids the need for comments (modules, classes, methods and variables).
- **Naming modules/classes/methods/variables**: "Intention-revealing, exact names" applies everywhere. Examples: `array` of `devices` instead of a `pool` of `cards`, `device` instead of `radio`, `sink` or `aggregator` instead of `picture` or `airspace`.
- **Human-facing docs** (any non-chipset `.md`): agents may edit them; say so in the hand-off so a human reviews the wording.
- **Planning docs** (NOT auto-loaded, open as needed): `docs/planning/FEATURES.md`, `docs/planning/BUGS.md`, `docs/SUPPORTED-HARDWARE.md` (+ the grading process: `docs/GRADING.md`).
- **Porting a driver, or editing `src/wifit3/chips/`**: read `docs/porting/METHODOLOGY.md` completely before starting; `chips/` code follows its "Code style in `chips/`", not the code style above.

## Commands

This repo uses **`uv`** for env management. Always run Python via `uv run` (or `.venv\Scripts\python.exe`).

```bash
# Install (editable, with dev deps)
uv sync --group dev               # preferred; or: pip install -e ".[dev]"

# Run
uv run wifit3                     # or: uv run python -m wifit3

# Tests
uv run pytest                          # all tests
uv run pytest tests/chips/ar9271_v2/   # single module
uv run pytest tests/wlan/test_parser.py::test_wlan_frame_parser_extracts_ssid

# Lint (lint only: NEVER format)
uv run ruff check src/

# Textual live dev (hot-reload)
uv run textual run --dev wifit3.ui.app:WifiteApp
```

Tests require no hardware: all USB interactions are mocked via `pytest-mock`. `asyncio_mode = "auto"` is set globally, so async tests require no decorator.

**Never run `ruff format`.**

## Architecture Overview

Wifit3 is a userland 802.11 auditing tool. It communicates directly with USB wireless devices via **PyUSB**.
The TUI is built on **Textual**.

### Where things live

Not a clean top-to-bottom stack (the real flow is more tangled), but the points of interest.

**Core engine**: generic and wifit3-agnostic, meant to be split out into a separate `wifit3-drivers`
package. Not off-limits, but changes here stay generic: no imports from the app layer (`ui/`, `id/`,
`vault/`, `persist/`, `campaigns/`), no wifit3 UI or product concepts.

```
dot11/   pure 802.11 / WSC spec: frame parsing and building, no device or asyncio
device/  DeviceManager + the VID:PID map (USB scan, VID:PID → driver, read WITHOUT importing the
         driver), DeviceWatch (plug-in / un-plug callbacks)
wlan/    WlanInterface (802.11 abstraction: channel hopping, AP/Client registry)
chips/   driver.py is the Driver ABC; one dir per chip subclasses it
```

**App layer**: wifit3 itself, built on the core engine.

```
ui/      Textual screens (Splash → Scanner → Focus); WifiteApp holds the DeviceManager + active interface
models/  project-wide dataclasses: AccessPoint, Client, DeviceID (DeviceID is also used by the core engine)
campaigns/ AutoDeauth, PMKID harvest, WPS PIN, WEP/Replay, EvilTwin
vault/   cracking-tool jobs: VaultManager + tools/ (hashcat)
persist/ Config (TOML), capture history, pcap / vault storage
id/      OUI → vendor lookup, device and client fingerprinting
crack/   offline crypto: WPA PSK, WEP, handshake checks, hc22000 format
setup/   per-OS privileged device setup (udev/modprobe, WinUSB) behind the splash's Install button
```

```
A chip dir (chips/<chipset>/) is typically shaped:
  __init__.py       SUPPORTED_IDS + import_driver (the VID:PID list, read without importing driver.py)
  driver.py         subclasses the Driver ABC; declares SUPPORTED_CHANNELS
  transport.py      raw USB read/write (control transfers + bulk I/O)
  firmware.py       firmware upload
  constants.py      register addresses, command IDs, magic bytes
  mac.py / phy.py   MAC / BB / RF / EFUSE port from kernel C
  chan.py / fifo.py channel tune, set_channel, FIFO partitioning
  rx.py / tx.py     RX descriptor decode + frame iter / TX descriptor build + bulk-OUT
```

Not every chip uses every module; add modules as the chip's protocol needs them.

### TUI Screens

- **SplashView** — USB device discovery, driver progress, interface selection
- **ScannerView** — Live AP table; triggers channel hopping; leads to FocusViewV2
- **FocusViewV2** — Single-AP focus view

`app.py`'s global `Button { min-width: 12; margin-right: 1 }` outranks every widget's `DEFAULT_CSS`.
Override it from the screen's `CSS` attribute (it competes on specificity), not by adding rules to `app.py`.
