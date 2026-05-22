# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Session Cheatsheet

- **Platform**: Dev machine is Windows + PowerShell. Bash tool is available, but `scripts/AGENTS.md` has PowerShell-isms worth respecting (no `&&` chaining, no Unix `grep`/`tail`, never pass `-c` to `tshark` since it limits *input* not output).
- **Cross-platform by design**: Wifit3 uses PyUSB + `libusb_package` so drivers run on Windows (with Zadig binding the device to WinUSB) AND Linux (after `rmmod <kernel_driver>`). No Kali boot is needed for normal dev — that's the whole point of going userland.
- **Hardware testing is the USER's job**, not the agent's. The loop is:
  1. Agent proposes code changes.
  2. User runs `python scripts/<chipset>/test_hw_<chipset>.py` (optionally `--debug`) and pastes output.
  3. Agent reads output, iterates.
  Do not try to flash/test hardware yourself.
- **Device gets borked? User replugs.** That resets cold-boot state. You can suggest "please unplug, wait a few seconds, replug, then rerun" if a previous attempt left it stuck.
- **Reverse-engineering workflow**: when porting a kernel driver, the kernel C is the spec but `usb_dumps/captures_*/capture-N.pcap` is the ground truth. Use the deterministic helpers in `scripts/` instead of ad-hoc tshark queries:
  - `python scripts/pcap_slicer.py <main.log> <pcap>` — maps `capture.py` log timestamps to pcap frame ranges (e.g. "firmware upload happens in frames 14182–14400").
  - For register/macro lookups in kernel sources, use `Grep` / `Read` directly against `data_dumps/<chip-source>/` (e.g. `Grep #define\s+REG_FOO data_dumps/rtw88-source-v6.18/ --glob "*.h"`).
  - See `scripts/AGENTS.md` for the full tooling brief.
- **Captures are made by `src/wifit3/scripts/capture.py`** on the Kali persistent USB. Each capture comes with a `*_logs/main.log` (absolute-epoch timeline) that `pcap_slicer.py` consumes.
- **Per-chipset ground-truth docs**: each chip dir has a `<CHIP>.md` (e.g. `chips/mt7921au/MT7921AU.md`) that accumulates *verified* facts decoded from its pcap. Treat anything not in that doc as a hypothesis. Update the doc as facts are confirmed so future sessions don't re-derive them.
- **Lead's rule** (from `NEXT-STEPS.md`): discuss class design (`GenericDriver` vs `WlanInterface` responsibilities, etc.) BEFORE execution. Treat the user as Senior Lead.
- **Other top-level docs** (NOT auto-loaded — open as needed): `DESIGN.md`, `NEXT-STEPS.md`, `QUIRKS.md`, `SOFT-MAC.md`, `WPA3-Frames.md`, `WPA3-SAE-Group-Detection.md`, `planning/OSX-SUPPORT.md`.

## Commands

This repo uses **`uv`** for env management. The system `python` on PATH does NOT have project deps — always run Python via `uv run` (or `.venv\Scripts\python.exe`). Quick import probes like `python -c "import textual"` from the agent will fail with `ModuleNotFoundError` — use `uv run python -c "..."` instead.

```bash
# Install (editable, with dev deps)
uv sync --group dev               # preferred; or: pip install -e ".[dev]"

# Run
uv run python -m wifit3

# Tests
uv run pytest                          # all tests
uv run pytest tests/chips/ar9271/      # single module
uv run pytest tests/wlan/test_parser.py::TestWlanFrameParser::test_beacon

# Lint / format
uv run ruff check src/
uv run ruff format src/

# Textual live dev (hot-reload)
uv run textual run --dev src/wifit3/ui/app.py
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

chips/rtw88_base/          Shared rtw88-family infrastructure (used by 8821au, 8822bu, ...)
  ├─ transport.py        Generic vendor-control xfer (bRequest=0x05) for all rtw88 USB chips
  ├─ phy_cond.py         rtw_parse_tbl_phy_cond walker (handles both 8812a/8821a bitfield-rfe and 8822b/c scalar-rfe)
  ├─ power_seq.py        Generic run_pwr_seq runtime (CMD codes + flag constants); per-chip TABLES live in the chip dir
  ├─ rf_sipi.py          SIPI read_rf / write_rf_masked primitives, path-A or path-B parameterised
  ├─ registers.py        Family-shared MAC/PHY reg.h symbols (REG_SYS_CFG1, REG_MCUFW_CTRL, REG_CR, iDDMA, ...)
  ├─ tx_common.py        TX-desc XOR checksum + dma_mapping → bulk-OUT-index lookup
  └─ rx_common.py        24-byte rx_pkt_desc decoder + bulk-IN endpoint probe + frame iterator

chips/<chipset>/
  ├─ driver.py           Implements WlanDriver Protocol; declares SUPPORTED_IDS + SUPPORTED_CHANNELS
  ├─ transport.py        Raw USB read/write (control transfers + bulk I/O)
  ├─ firmware.py         Firmware upload logic
  ├─ constants.py        Register addresses, command IDs, magic bytes
  ├─ mac.py / phy.py     (post-bring-up family) MAC / BB / RF / EFUSE port from kernel C
  ├─ chan.py / fifo.py   Channel tune, set_channel, FIFO partitioning
  ├─ rx.py / tx.py       RX descriptor decode + frame iter / TX descriptor build + bulk-OUT
  ├─ power_seq.py        rtw_pwr_seq_cmd translations + run_pwr_seq runtime
  ├─ phy_cond.py         rtw_parse_tbl_phy_cond walker (for rtw88-family init tables)
  └─ assets/ or sequences/
       init.py / tuning.py   Captured USB register-write sequences (replayed at runtime)
       *_tbl.py              For rtw88 family: flat-u32 init tables ported 1:1 from kernel C
       <chip>_fw.bin         Firmware blob (pcap-extracted, byte-verified vs linux-firmware)
```

Not every chip uses every module — `mac.py`/`phy.py`/`chan.py`/`tx.py` etc. are the rtw88-family split. AR9271 lives mostly in `driver.py` + `protocol/`. MT7921AU has its own `firmware.py` + `sequences/`. Add modules as the chip's protocol needs them.

### Supported Hardware

See `NEXT-STEPS.md` for the current supported-hardware table (chip → VID:PID → status → notes).

### Adding a New Chipset

The manager is a generic VID:PID discovery loop — each driver declares its own hardware. To register a new chip:

1. Create `src/wifit3/chips/<name>/` with at minimum `driver.py`, `transport.py`, `constants.py` (+ `firmware.py` if the chip needs a FW upload).
2. `driver.py` must satisfy the `WlanDriver` Protocol (`wifit3.engine.protocols`). Concretely:
   - Class attr `SUPPORTED_IDS: list[DeviceID]` — every VID:PID this driver claims, with a human-readable description and any chip-id discriminator in `extras={}`.
   - Class attr `SUPPORTED_CHANNELS: list[int]` — every channel the driver can tune to (consumed by `WlanInterface.start_hopping`).
   - Classmethod `from_usb_device(cls, dev, id_entry) -> Driver` — driver-side construction (transport wrapping, chip_id reads from `extras`, etc.). Keeps the manager free of chip-specific switches.
   - Runtime methods: `connect()`, `set_channel()`, `inject_frame()`, `close()`, plus the `register_rx_callback()` hook.
3. Add the driver class to `_all_drivers()` in `wifit3/wlan/manager.py` (lazy registry — order is the priority for VID:PID disambiguation).
4. Drop a `<CHIP>.md` ground-truth doc next to the driver with `[SRC]`/`[WIRE]` citations.

The cold-vs-warm distinction is a per-driver concern: if a previous session left the chip running, `connect()` should detect that and skip the bring-up. See `chips/rtl8821au/mac.py:is_chip_warm()` + `driver.py:_warm_reattach` for the pattern (light reattach + smoke-test the bulk-IN pipe; surface a clear "please replug" message if the USB pipe is wedged — that path can't always be recovered in userland on Windows+WinUSB).

### Per-chipset Protocol Notes

See `chips/<chipset>/<CHIP>.md` for per-chipset protocol notes — FW upload path, PHY/MAC init, channel-tune semantics, warm-reattach behaviour, and per-chip bit-position gotchas. Each chip's ground-truth doc accumulates `[SRC]/[WIRE]/[HW]` citations against the kernel source and the cold-boot pcap.

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
