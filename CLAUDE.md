# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Session Cheatsheet

- **Git**: Commit directly to `master`; branch, switch branches, or make worktrees only when asked. The working tree is shared across concurrent sessions: `git status` may show files, and tests may fail, from work that isn't yours; stage only your task's files (`git add <paths>`, never `-A`/`.`).
- **Comments / code style**: a small closed allowlist: docstrings, citations/magic-value notes, phase landmarks; everything else is noise. When unsure, omit; prefer naming over commenting. Full rules in `docs/porting/CODE-STYLE.md`.
- **Cross-platform by design**: Wifit3 uses PyUSB + `libusb_package` so drivers run on Windows (with Zadig binding the device to WinUSB) AND Linux (after `rmmod <kernel_driver>`). No Kali boot is needed for normal dev. That's the whole point of going userland.
- **Hardware testing: the agent runs it.** Offline pcap verification (`scripts/verify_pcap.py <chip>`) tests the port against the recorded wire without hardware, so chipset bring-up is now agent-driven: once the user has plugged in + WinUSB-bound (Zadig) the card, the agent runs `python scripts/<chipset>/test_hw.py` (optionally `--debug`) **itself**, pcap-verifies + HW-smoke-tests each milestone, and commits each: no per-iteration handoff. Everything including wiring *and* firing TX (channel tune, 2.4/5 GHz RX, TX-descriptor build) is the agent's to complete autonomously. The `beacon_watch.py` (live) vs `beacon_watch_usbcap.py` (the `usb_dumps_new/` capture's beacon count) A/B is the RX-health check.
- **Device gets borked? User replugs.** That resets cold-boot state. You can suggest "please unplug, wait a few seconds, replug, then rerun" if a previous attempt left it stuck.
- **Porting / bringing up a chip?** The playbook lives in `docs/porting/METHODOLOGY.md` (or run `/port <chip>`): port from the C source, verify each milestone against the cold-boot pcap, commit each. Run the loop to a stopping point yourself: surface only for a decision you can't make from source+pcap, the live-TX gate, a committed milestone, or a real block. Don't narrate progress.
- **Register READs can mutate device state: never assume two reads commute, never reorder them vs the capture.** Read-to-clear status regs, latch-on-read pairs, FIFO pops, indirect-access auto-advance: `READ X; READ Y` ≠ `READ Y; READ X` on silicon, and out-of-order reads strand the card in a state the capture never visited. So the verify tool's strict-positional cursor (reads included) is a *correctness* gate, not pedantry: a reordered-read divergence is a real driver bug to fix, never a tolerance to add.
- **Per-chipset port-reference docs**: each chip dir has a `<CHIP>.md`: a short README for the chip (status, gotchas, orientation, scripts, dated debug log). Keep it to what isn't already in the code. Template + rules in `docs/porting/CHIP-DOC.md`.
- **Human-facing docs are the face of the project.** `README.md` + `VERIFICATION.md`: edit only when the user asks, and show the proposed edit for approval before writing (terse, observational, no port-accuracy braggadocio; that belongs in `<CHIP>.md` + commits). Prefer prose direction over multiple-choice for these.
- **Within `chips/`, don't re-use code from another driver.** *Why:* a shared core meant a fix for one card forced re-testing every card and risked regressing the others.
- **Lead's rule**: discuss class design (`Driver` vs `WlanInterface` responsibilities, etc.) BEFORE execution. Treat the user as Senior Lead.
- **Never write to auto-memory without asking.** Before saving or updating any file under the auto-memory dir (`MEMORY.md` + its entries), show the user the proposed entry and wait for explicit approval. This overrides the default proactive-save behavior. The user owns what goes into always-loaded context.
- **Planning docs** (NOT auto-loaded, open as needed): `planning/RELEASE-PLAN.md` (road to release + logistics + code-quality/de-vibe), `planning/FEATURES.md` (capabilities to build), `planning/BUGS.md` (defects + QoL to fix). Current per-card state: `VERIFICATION.md` (grading process: `docs/verification-methodology.md`). Porting playbook: `docs/porting/` (or `/port <chip>`).

## Commands

This repo uses **`uv`** for env management. The system `python` on PATH does NOT have project deps: always run Python via `uv run` (or `.venv\Scripts\python.exe`). Quick import probes like `python -c "import textual"` from the agent will fail with `ModuleNotFoundError`. Use `uv run python -c "..."` instead.

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

**Never run `ruff format`.** This tree is hand-formatted (~99-col, multi-per-line collections) and is NOT `ruff format`-clean: running the formatter reflows the entire codebase at the default 88-col + magic-trailing-comma, burying your actual diff in thousands of unrelated lines. The formatter is disabled repo-wide in `pyproject.toml` (`[tool.ruff.format] exclude` + `force-exclude`), so `ruff format` is a deliberate no-op; lint with `ruff check` only and match the surrounding style by hand.

## Architecture Overview

Wifit3 is a userland 802.11 auditing tool. It communicates directly with USB wireless cards via **PyUSB**: no `aircrack-ng` subprocess wrappers, no Scapy. The TUI is built on **Textual**.

### Where things live

Not a clean top-to-bottom stack (the real flow is more tangled), but the points of interest:

```
ui/      Textual screens (Splash → Scanner → Focus); WifiteApp holds the device manager + active interface
wlan/    WlanDeviceManager (USB scan, VID:PID → driver), WlanInterface (802.11 abstraction:
         channel hopping, AP/Client registry, handshake tracking), WlanFrameParser (pure-Python parser)
engine/  models.py (Pydantic: AccessPoint, Client, Handshake); attacks/ (SAE probe, etc.)
chips/   driver.py is the Driver ABC; one dir per chip subclasses it

A chip dir (chips/<chipset>/) is typically shaped:
  driver.py         subclasses the Driver ABC; declares SUPPORTED_IDS + SUPPORTED_CHANNELS
  transport.py      raw USB read/write (control transfers + bulk I/O)
  firmware.py       firmware upload
  constants.py      register addresses, command IDs, magic bytes
  mac.py / phy.py   MAC / BB / RF / EFUSE port from kernel C
  chan.py / fifo.py channel tune, set_channel, FIFO partitioning
  rx.py / tx.py     RX descriptor decode + frame iter / TX descriptor build + bulk-OUT
```

Not every chip uses every module; add modules as the chip's protocol needs them.

### Supported Hardware

See `README.md` for the user-facing supported-cards table, and `VERIFICATION.md` for the full per-attack verification matrix.

### Adding a New Chipset

The manager is a generic VID:PID discovery loop: each driver declares its own hardware. To register a new chip:

1. Create `src/wifit3/chips/<name>/` with at minimum `driver.py`, `transport.py`, `constants.py` (+ `firmware.py` if the chip needs a FW upload).
2. `driver.py` must subclass the `Driver` ABC (`wifit3.chips.driver`); Python enforces the surface at instantiation. Concretely:
   - Class attr `SUPPORTED_IDS: list[DeviceID]` — every VID:PID this driver claims, with a human-readable description and any chip-id discriminator in `extras={}`.
   - Class attr `SUPPORTED_CHANNELS: list[int]` — every channel the driver can tune to (consumed by `WlanInterface.start_hopping`).
   - Classmethod `from_usb_device(cls, dev, id_entry) -> Driver` — driver-side construction (transport wrapping, chip_id reads from `extras`, etc.). Keeps the manager free of chip-specific switches.
   - Runtime methods: `connect()`, `set_channel()`, `inject_frame()`, `close()`, plus the `register_rx_callback()` hook.
3. Add the driver class to `_all_drivers()` in `wifit3/wlan/manager.py` (lazy registry: order is the priority for VID:PID disambiguation).
4. Drop a `<CHIP>.md` port-reference doc next to the driver (skeleton + rules in `docs/porting/CHIP-DOC.md`).

The cold-vs-warm distinction is a per-driver concern: if a previous session left the chip running, `connect()` should detect that and skip the bring-up. See `chips/rtl8821au/mac.py:is_chip_warm()` + `driver.py:_warm_reattach` for the pattern (light reattach + smoke-test the bulk-IN pipe; surface a clear "please replug" message if the USB pipe is wedged: that path can't always be recovered in userland on Windows+WinUSB).

### Per-chipset Protocol Notes

See `chips/<chipset>/<CHIP>.md` for per-chipset protocol notes: FW upload path, PHY/MAC init, channel-tune semantics, warm-reattach behaviour, and per-chip bit-position gotchas.

### Frame Flow (RX)

```
transport._rx_loop()
  → driver._on_raw_rx()       strips hardware descriptor (RXD/RXWI), extracts RSSI
  → WlanFrameParser.parse_80211_frame()   returns Packet
  → WlanInterface._on_frame_parsed()      updates AP/Client registry
  → UI ScannerView polls interface.get_access_points() via Textual timer
```

### TUI Screens

- **SplashView** — USB device discovery, driver progress, interface selection
- **ScannerView** — Live AP table; triggers channel hopping; leads to FocusViewV2
- **FocusViewV2** — Single-target attack panel (deauth, handshake capture)
