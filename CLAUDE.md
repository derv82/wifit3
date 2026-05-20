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

| Chip | VID:PID | Architecture |
|------|---------|--------------|
| Atheros AR9271 | 0cf3:9271 | Soft-MAC — WMI over HTC, Big Endian, strict Sequence IDs |
| Realtek RTL8187L | 0bda:8187 | Hard-MAC — USB control transfers |
| Ralink RT5572 | 148f:5572 | rt2800usb family |
| Ralink RT3572 | 148f:3572 | rt2800usb family |
| Ralink RT5372 | 148f:5372 | rt2800usb family |
| **Realtek RTL8821AU** | **0bda:0811** | **rtw88 family — WiFi 5 (2.4 + 5 GHz), legacy MCUFWDL FW path** |
| **Realtek RTL8822BU** | **2357:0138** (+24 more) | **rtw88 family — WiFi 5 (2.4 + 5 GHz), 2T2R, modern iDDMA FW path** |
| Mediatek MT7921AU | 0e8d:7961 | WiFi 6 — MCU Unified Commands, Little Endian |

RT2800USB variants share `RT2800USBDriver`; the `chip_id` string (carried in `DeviceID.extras`) selects the right RXWI/TXWI sizes and init assets.

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

### AR9271 Protocol Notes (Soft-MAC)

Channel changes require replaying a ~500-byte WMI_REG_WRITE (0x15) sequence with dynamically injected Sequence IDs, then patching register `0x9874` (AR_PHY_SYNTH_CONTROL) with the Fractional-N Synthesizer Word for the target channel. Static PCAP replay fails because the firmware rejects out-of-order Sequence IDs. See `SOFT-MAC.md` and `QUIRKS.md` for byte offsets and the WMI header format.

### RTL8821AU Protocol Notes (rtw88 family — legacy MCUFWDL)

WiFi 5, 2.4 + 5 GHz, 1T1R. Cleanroom-RE'd from `data_dumps/rtw88-source-v6.18/`; FW blob byte-verified vs `linux-firmware/rtw88/rtw8821a_fw.bin`. Full bring-up runs in userland on Windows (WinUSB via Zadig) and Linux:

- **FW upload**: legacy `MCUFWDL` path (8051 wlan CPU). All control transfers — `bRequest=0x05`, `wValue=FW_START_ADDR_LEGACY(0x1000)+offset`. ACK is `BIT_FWDL_CHK_RPT` of `REG_MCUFW_CTRL(0x0080)`; running confirmation is `FW_READY_LEGACY=0xC6`.
- **Init tables**: `mac_tbl` / `agc_tbl` / `bb_tbl` / `rf_a_tbl` ported 1:1 from `rtw8821a_table.c`. Runtime walker `chips/rtl8821au/phy_cond.py` mirrors `rtw_parse_tbl_phy_cond` + `check_positive` line-for-line. Branches gate on (intf, rfe); no `cut` dependency for 8821A.
- **EFUSE optional for RX**: rfe=0 ELSE-branch defaults are sufficient for monitor-mode capture on AWUS036ACS. EFUSE read becomes necessary for accurate TX power + BT coex.
- **Channel tune**: `chan.py:set_channel_2g_20mhz` / `set_channel_5g_20mhz`. Unified `_switch_channel` does the centre-frequency-area write + RF18 SIPI writes (RFREG_MASK=0xfffff, encoded as `((addr<<20)|(data & 0xfffff)) & 0x0fffffff`).
- **Warm reattach**: `mac.is_chip_warm()` checks FW_READY_LEGACY + MACTXEN|MACRXEN. Driver skips the entire bring-up and just resumes USB polling. 1.5 s RX smoke test detects wedged bulk-IN pipes and tells the user to replug.

When porting other rtw88 chips (8812au, 8822bu, 8814au), most of this stack should slot in: the phy_cond walker, the SIPI primitives, the `pwr_seq` runtime, the tx_desc/rx_desc layouts are family-shared. The big delta is the FW upload path — 8822bu/8814au use modern `iddma` rather than legacy `MCUFWDL`.

### RTL8822BU Protocol Notes (rtw88 family — modern iDDMA)

WiFi 5, 2.4 + 5 GHz, **2T2R**, no 8051 (modern wlan CPU). Confirmed on the TP-Link Archer T3U Plus v1 (`2357:0138`, CUT_D). 25 known VID:PIDs from rtw8822bu.c's id_table. Full-bring-up runs in userland on Windows (WinUSB) and Linux:

- **FW upload (M2)**: modern `iDDMA` path — TX descriptor + chunk on bulk-OUT EP 0x05 (BEACON qsel → HIGH-priority lane), then iDDMA register triggers (REG_DDMA_CH0SA/DA/CTRL with OWN | CHKSUM_EN). FW file = 64-byte rtw_fw_hdr + 11216-byte DMEM + 149960-byte IMEM; our pcap-extracted blob is 161176 B, byte-for-byte verified vs `linux-firmware/rtw88/rtw8822b_fw.bin[64:]`. ~106 ms end-to-end.
- **FW_READY (M3)**: `(REG_MCUFW_CTRL & 0xCFFF) == 0xC078` (FW_INIT_RDY | FW_DW_RDY | IMEM_DW_OK | DMEM_DW_OK | IMEM_CHKSUM_OK | DMEM_CHKSUM_OK).
- **PHY init (M4)**: 5 tables (mac + agc + bb + rf_a + rf_b, ~5000 register writes) loaded via shared `rtw88_base/phy_cond.py` walker with `chip_id=OTHER` (scalar-rfe semantics; 8822b uses `cond.rfe != drv_cond.rfe`, NOT bitfield like 8821a). `EfuseDefaults` uses `rfe_option=3` (= IFEM with ext, matches most retail dongles in id_table). Skips `config_trx_mode`, `phy_rfe_init`, `pwrtrack_init`, `phy_bf_init`, `phy_init` DIG — not needed for monitor RX. ~950 ms.
- **MAC init for RX (M5)**: REG_CR = MAC_TRX_ENABLE, RX filters open (`REG_RXFLTMAP0=0x0FFFFFFF`, `REG_RCR=0xE400220E`), drv_info + APP_PHYSTS, USB burst size 512.
- **Priority queue init (M7, needed for MGMT TX)**: `mac.init_priority_queue_8822b` ports `__priority_queue_cfg` — FIFO page tables + auto LLT init. Without this, MGMT bulk-OUT stalls.
- **Channel tune (M6)**: `chan.set_channel_2g_20mhz` / `_5g_20mhz`. Full port of `rtw8822b_set_channel`: BB + MAC + RF18 + rxdfir + toggle_igi + CCA + RFE/TRSW switch. 2T2R writes RF18 on both paths A and B. 5G uses rfbe lookup from `LOW_BAND[]` / `MIDDLE_BAND[]` / `HIGH_BAND[]` tables.
- **TX inject (M7)**: 48-byte tx_pkt_desc (vs 8821a's 40), MGMT qsel → bulk-OUT EP 0x05. `old_datarate_fb_limit = False` for 8822b (vs True for 8821a) — DO NOT set W4 FB_LIMIT.
- **Warm reattach**: `mac.is_chip_warm()` checks `FW_INIT_RDY | FW_DW_RDY` in REG_MCUFW_CTRL + `MACTXEN | MACRXEN` in REG_CR. Same "please replug" pattern as 8821a if bulk-IN pipe is wedged.

**Critical bit-position bug to avoid**: `BIT_HCI_TXDMA_EN = BIT(0)` and `BIT_TXDMA_EN = BIT(2)` (NOT BIT(2) and BIT(3) as one might guess). Wrong bits silently break bulk-OUT — chip won't accept any TX, manifests as USBTimeoutError. See `chips/rtl8822bu/RTL8822BU.md` for the full bit-vs-bug audit table.

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
