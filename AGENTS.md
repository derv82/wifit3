# CLAUDE.md

This file provides guidance to coding agents when working with code in this repository.

## Session Cheatsheet

- **Code style**: Outside of `chips/`, comments/docstrings only on public APIs, 2 line maximum (a ceiling not a quota). Don't paper over bad code with a comment: Precise and accurate naming avoids the need for comments (modules, classes, methods and variables).
- **Naming modules/classes/methods/variables**: "Intention-revealing, exact names" applies everywhere. Examples: `array` of `devices` instead of a `pool` of `cards`, `device` instead of `radio`, `sink` or `aggregator` instead of `picture` or `airspace`.
- **Human-facing docs are written by humans.** Any non-chipset `.md` files.
- **Planning docs** (NOT auto-loaded, open as needed): `docs/planning/FEATURES.md`, `docs/planning/BUGS.md`, `docs/SUPPORTED-HARDWARE.md` (+ the grading process: `docs/GRADING.md`).

## Porting Session Cheatsheet

- **Porting code style**: When porting or working within `src/wifit3/chips/**/*`: `docs/porting/CODE-STYLE.md`.
- **Porting / bringing up a chip?** Playbook: `docs/porting/METHODOLOGY.md` (or run `/port <chip>` in `.claude/skills/port`).
- **Per-chipset port-reference docs**: each chip dir has a `<CHIP>.md`. Template + rules in `docs/porting/CHIP-DOC.md`.
- **Within `chips/`, don't re-use code from another driver.** *Why:* a shared core meant a fix for one device forced re-testing every device and risked regressing the others.
- **Register READs can mutate device state: never assume two reads commute, never reorder them vs the capture.**
- **Device gets borked? User replugs.** That resets cold-boot state.

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
uv run textual run --dev src/wifit3/ui/app.py
```

Tests require no hardware: all USB interactions are mocked via `pytest-mock`. `asyncio_mode = "auto"` is set globally, so async tests require no decorator.

**Never run `ruff format`.**

## Architecture Overview

Wifit3 is a userland 802.11 auditing tool. It communicates directly with USB wireless devices via **PyUSB**.
The TUI is built on **Textual**.

### Where things live

Not a clean top-to-bottom stack (the real flow is more tangled), but the points of interest:

```
ui/      Textual screens (Splash → Scanner → Focus); WifiteApp holds the DeviceManager + active interface
device/  DeviceManager + the VID:PID map (USB scan, VID:PID → driver, read WITHOUT importing the
         driver), DeviceWatch (plug-in / un-plug callbacks)
wlan/    WlanInterface (802.11 abstraction: channel hopping, AP/Client registry),
         WlanFrameParser (the Python 802.11 frame parser)
models/  project-wide dataclasses: AccessPoint, Client, DeviceID
campaigns/ AutoDeauth, PMKID harvest, WPS PIN, WEP/Replay, EvilTwin
chips/   driver.py is the Driver ABC; one dir per chip subclasses it

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

### Adding a New Chipset

Discovery is a `pkgutil` walk over `chips/*` that reads each package's light `__init__` (its VID:PID list) WITHOUT importing the driver; the matched driver is imported only on a hit. There is no manual registry to edit. To add a chip:

1. Create `src/wifit3/chips/<name>/` with at minimum `__init__.py`, `driver.py`, `transport.py`, `constants.py` (+ `firmware.py` if the chip needs a FW upload).
2. `chips/<name>/__init__.py` declares the hardware, and must NOT import `driver.py` at module top:
   - `SUPPORTED_IDS: list[DeviceID]` (`from wifit3.models import DeviceID`): every VID:PID this driver claims, with a human-readable description and any chip-id discriminator in `extras={}`.
   - `def import_driver()`: the one heavy import, lazy (`from .driver import <Class>; return <Class>`).
3. `driver.py` must subclass the `Driver` ABC (`wifit3.chips.driver`); Python enforces the surface at instantiation:
   - Class attr `SUPPORTED_CHANNELS: list[int]`: every channel the driver can tune to (consumed by `WlanInterface.start_hopping`). `SUPPORTED_IDS` lives in `__init__.py`, not on the class.
   - Classmethod `from_usb_device(cls, dev, id_entry) -> Driver`: driver-side construction (transport wrapping, chip_id reads from `extras`).
   - Runtime methods: `connect()`, `set_channel()`, `inject_frame()`, `close()`, plus the `register_rx_callback()` hook.
4. Only if the chip's setup key must differ from its dir name, or two packages claim the same VID:PID (the Realtek mainline/DKMS pairs): add a row to `_FAMILIES` in `device/manager.py`. Otherwise there is nothing else to register.
5. Drop a `<CHIP>.md` port-reference doc next to the driver (skeleton + rules in `docs/porting/CHIP-DOC.md`).

The cold-vs-warm distinction is a per-driver concern: if a previous session left the chip running, `connect()` should detect that and skip the bring-up. See `chips/rtl8821au/mac.py:is_chip_warm()` + `driver.py:_warm_reattach` for the pattern.

### TUI Screens

- **SplashView** — USB device discovery, driver progress, interface selection
- **ScannerView** — Live AP table; triggers channel hopping; leads to FocusViewV2
- **FocusViewV2** — Single-AP focus view
