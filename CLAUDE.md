# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Session Cheatsheet

- **Platform**: Dev machine is Windows + PowerShell. Bash tool is available, but respect the PowerShell-isms (no `&&` chaining, no Unix `grep`/`tail`, never pass `-c` to `tshark` since it limits *input* not output).
- **Git**: Commit directly to `master`; branch, switch branches, or make worktrees only when asked. The working tree is shared across concurrent sessions — `git status` may show files, and tests may fail, from work that isn't yours; stage only your task's files (`git add <paths>`, never `-A`/`.`).
- **Comments / code style**: a small closed allowlist — docstrings, citations/magic-value notes, phase landmarks, surprise-why; everything else is noise. When unsure, omit; prefer naming over commenting. Full rules in `docs/porting/CODE-STYLE.md`.
- **Cross-platform by design**: Wifit3 uses PyUSB + `libusb_package` so drivers run on Windows (with Zadig binding the device to WinUSB) AND Linux (after `rmmod <kernel_driver>`). No Kali boot is needed for normal dev — that's the whole point of going userland.
- **Hardware testing — the agent runs it.** Offline pcap verification (`scripts/verify_pcap.py <chip>`) tests the port against the recorded wire without hardware, so chipset bring-up is now agent-driven: once the user has plugged in + WinUSB-bound (Zadig) the card, the agent runs `python scripts/<chipset>/test_hw.py` (optionally `--debug`) **itself**, pcap-verifies + HW-smoke-tests each milestone, and commits each — no per-iteration handoff. **Hard boundary: the agent NEVER fires live 802.11 TX (frame injection / deauth) — that is the user's explicit action** [[passive_by_default]]. Everything up to and including *wiring* TX (channel tune, 2.4/5 GHz RX, TX-descriptor build) is the agent's to complete autonomously. The `beacon_watch.py` (live) vs `beacon_watch_usbcap.py` (the `usb_dumps_new/` capture's beacon count) A/B is the RX-health check.
- **Device gets borked? User replugs.** That resets cold-boot state. You can suggest "please unplug, wait a few seconds, replug, then rerun" if a previous attempt left it stuck.
- **Porting / bringing up a chip?** The playbook lives in `docs/porting/METHODOLOGY.md` (or run `/port <chip>`): port from the C source, verify each milestone against the cold-boot pcap, commit each. Run the loop to a stopping point yourself — surface only for a decision you can't make from source+pcap, the live-TX gate, a committed milestone, or a real block. Don't narrate progress.
- **Register READs can mutate device state — never assume two reads commute, never reorder them vs the capture.** Read-to-clear status regs, latch-on-read pairs, FIFO pops, indirect-access auto-advance: `READ X; READ Y` ≠ `READ Y; READ X` on silicon, and out-of-order reads strand the card in a state the capture never visited. So the verify tool's strict-positional cursor (reads included) is a *correctness* gate, not pedantry — a reordered-read divergence is a real driver bug to fix, never a tolerance to add.
- **Per-chipset port-reference docs**: each chip dir has a `<CHIP>.md` — a short README for the chip (status, gotchas, orientation, scripts, dated debug log). Keep it to what isn't already in the code. Template + rules in `docs/porting/CHIP-DOC.md`.
- **Human-facing docs are the face of the project.** `README.md` + `VERIFICATION.md`: edit only when the user asks (then just do it — terse, observational, no port-accuracy braggadocio; that belongs in `<CHIP>.md` + commits). Prefer prose direction over multiple-choice for these.
- **Within `chips/`, driver *implementation* is deliberately anti-DRY: don't assume a shared family base.** Some families share infra (`chips/rtw88_base/`, `chips/rtl88xxau_base/`), but many are separate per-chip implementations with their *own* transports (`rt2800usb` uses `read32/write32`; `rt3070`/`rt5372` use `register_read/register_write`), and a chip's mainline vs `_dkms` variant are independent. *Why:* a shared core meant a fix for one card forced re-testing every card and risked regressing the others. *So:* porting a cross-cutting change (a new capability, a core fix) is a **separate port into each driver's own structure** — the mechanism (registers) transfers, the code does not. Check the actual imports before assuming a sibling inherits anything. **Scope (read before citing this):** it governs per-chip *implementation/behavior inside `chips/`* and the hardware-retest cost of sharing it. It does NOT govern class or interface design, and it does NOT apply to the driver *contract*. The driver interface is the opposite: a single base `Driver` (ABC) that every `chips/*/driver.py` inherits is **required**. A shared *shape* carries no behavior, so it has zero hardware-retest cost and never falls under this rule.
- **Lead's rule**: discuss class design (`Driver` vs `WlanInterface` responsibilities, etc.) BEFORE execution. Treat the user as Senior Lead.
- **Never write to auto-memory without asking.** Before saving or updating any file under the auto-memory dir (`MEMORY.md` + its entries), show the user the proposed entry and wait for explicit approval. This overrides the default proactive-save behavior — the user owns what goes into always-loaded context.
- **Planning docs** (NOT auto-loaded — open as needed): `planning/RELEASE-PLAN.md` (road to release + logistics + code-quality/de-vibe), `planning/FEATURES.md` (capabilities to build), `planning/BUGS.md` (defects + QoL to fix). Current per-card state: `VERIFICATION.md` (grading process: `docs/verification-methodology.md`). Porting playbook: `docs/porting/` (or `/port <chip>`).

## Commands

This repo uses **`uv`** for env management. The system `python` on PATH does NOT have project deps — always run Python via `uv run` (or `.venv\Scripts\python.exe`). Quick import probes like `python -c "import textual"` from the agent will fail with `ModuleNotFoundError` — use `uv run python -c "..."` instead.

```bash
# Install (editable, with dev deps)
uv sync --group dev               # preferred; or: pip install -e ".[dev]"

# Run
uv run wifit3                     # or: uv run python -m wifit3

# Tests
uv run pytest                          # all tests
uv run pytest tests/chips/ar9271_v2/   # single module
uv run pytest tests/wlan/test_parser.py::test_wlan_frame_parser_extracts_ssid

# Lint (lint only — NEVER format)
uv run ruff check src/

# Textual live dev (hot-reload)
uv run textual run --dev src/wifit3/ui/app.py
```

Tests require no hardware — all USB interactions are mocked via `pytest-mock`. `asyncio_mode = "auto"` is set globally, so async tests require no decorator.

**Never run `ruff format`.** This tree is hand-formatted (~99-col, multi-per-line collections) and is NOT `ruff format`-clean — running the formatter reflows the entire codebase at the default 88-col + magic-trailing-comma, burying your actual diff in thousands of unrelated lines. The formatter is disabled repo-wide in `pyproject.toml` (`[tool.ruff.format] exclude` + `force-exclude`), so `ruff format` is a deliberate no-op; lint with `ruff check` only and match the surrounding style by hand.

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
  └─ attacks/            Attack implementations (SAE probe, etc.)

chips/driver.py            The `Driver` ABC every chips/*/driver.py subclasses; + DeviceID / FakeMacSupport
chips/rtw88_base/          Shared rtw88-family infrastructure (used by 8821au, 8822bu, ...)
  ├─ transport.py        Generic vendor-control xfer (bRequest=0x05) for all rtw88 USB chips
  ├─ phy_cond.py         rtw_parse_tbl_phy_cond walker (handles both 8812a/8821a bitfield-rfe and 8822b/c scalar-rfe)
  ├─ power_seq.py        Generic run_pwr_seq runtime (CMD codes + flag constants); per-chip TABLES live in the chip dir
  ├─ rf_sipi.py          SIPI read_rf / write_rf_masked primitives, path-A or path-B parameterised
  ├─ registers.py        Family-shared MAC/PHY reg.h symbols (REG_SYS_CFG1, REG_MCUFW_CTRL, REG_CR, iDDMA, ...)
  ├─ tx_common.py        TX-desc XOR checksum + dma_mapping → bulk-OUT-index lookup
  └─ rx_common.py        24-byte rx_pkt_desc decoder + bulk-IN endpoint probe + frame iterator

chips/<chipset>/
  ├─ driver.py           Subclasses the `Driver` ABC (chips/driver.py); declares SUPPORTED_IDS + SUPPORTED_CHANNELS
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

Not every chip uses every module — `mac.py`/`phy.py`/`chan.py`/`tx.py` etc. are the rtw88-family split. AR9271 (ath9k_htc) splits across `driver.py` + `hw`/`phy`/`tx`/`rx`/`wmi`/`htc`. MT7921AU has its own `firmware.py` + `sequences/`. Add modules as the chip's protocol needs them.

### Supported Hardware

See `README.md` for the user-facing supported-cards table, and `VERIFICATION.md` for the full per-attack verification matrix.

### Adding a New Chipset

The manager is a generic VID:PID discovery loop — each driver declares its own hardware. To register a new chip:

1. Create `src/wifit3/chips/<name>/` with at minimum `driver.py`, `transport.py`, `constants.py` (+ `firmware.py` if the chip needs a FW upload).
2. `driver.py` must subclass the `Driver` ABC (`wifit3.chips.driver`); Python enforces the surface at instantiation. Concretely:
   - Class attr `SUPPORTED_IDS: list[DeviceID]` — every VID:PID this driver claims, with a human-readable description and any chip-id discriminator in `extras={}`.
   - Class attr `SUPPORTED_CHANNELS: list[int]` — every channel the driver can tune to (consumed by `WlanInterface.start_hopping`).
   - Classmethod `from_usb_device(cls, dev, id_entry) -> Driver` — driver-side construction (transport wrapping, chip_id reads from `extras`, etc.). Keeps the manager free of chip-specific switches.
   - Runtime methods: `connect()`, `set_channel()`, `inject_frame()`, `close()`, plus the `register_rx_callback()` hook.
3. Add the driver class to `_all_drivers()` in `wifit3/wlan/manager.py` (lazy registry — order is the priority for VID:PID disambiguation).
4. Drop a `<CHIP>.md` port-reference doc next to the driver (skeleton + rules in `docs/porting/CHIP-DOC.md`).

The cold-vs-warm distinction is a per-driver concern: if a previous session left the chip running, `connect()` should detect that and skip the bring-up. See `chips/rtl8821au/mac.py:is_chip_warm()` + `driver.py:_warm_reattach` for the pattern (light reattach + smoke-test the bulk-IN pipe; surface a clear "please replug" message if the USB pipe is wedged — that path can't always be recovered in userland on Windows+WinUSB).

### Per-chipset Protocol Notes

See `chips/<chipset>/<CHIP>.md` for per-chipset protocol notes — FW upload path, PHY/MAC init, channel-tune semantics, warm-reattach behaviour, and per-chip bit-position gotchas.

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
- **ScannerView** — Live AP table; triggers channel hopping; leads to FocusViewV2
- **FocusViewV2** — Single-target attack panel (deauth, handshake capture)
