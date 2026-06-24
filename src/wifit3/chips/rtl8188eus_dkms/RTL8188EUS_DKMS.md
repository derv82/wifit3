# RTL8188EUS — DKMS (vendor) port

Sibling of `chips/rtl8188eus/` (mainline), ported from the Realtek vendor/DKMS driver
(`realtek-rtl8188eus` 5.3.9, module `8188eu`, in the capture bundle). RTL8188EUS silicon: 1T1R,
2.4 GHz only, phydm/ODM RX stack, firmware-based. The goal of the re-port is hotter, **stable**
2.4 GHz monitor RX than mainline drives the 8188e at. VID:PID `2357:010c`; registered behind
`WIFIT3_RTL8188` (mainline default, `=dkms` opts in).

## Status

Cold init + firmware boot, channel tune, monitor RX, and TX all work on hardware (live TL-WN722N
v2): a 28 s hop saw 78 APs / 940 beacons / 1527 frames, and `deauth_hw.py` landed (target
reconnected, 20/20 captured EAPOL to/from it). RX is promiscuous both directions — client→AP ToDS
data is captured, so the crackable WPA M2 is reachable (no ToDS-filter gap). `verify_pcap` is clean
end-to-end on all three captures.

Default stays `mainline` until a hardware A/B (`beacon_watch.py`, before/after the RXFLTMAP fix
below) settles the re-port. Untested/un-walked: uncaptured TX-desc variants, 40 MHz, power-save,
sreset recovery, and the runtime IQK/LCK/power-track triggers — no wire ground-truth exists for any
of these.

## Gotchas

**`verify_pcap` green + `beacon_watch` healthy do NOT mean this matches the kernel line-by-line.**
`verify_pcap` is green by construction — the hardcoded constants (phy_cond driver words, board /
PA-LNA / antenna / channel-plan assumptions) were tuned to reproduce the recorded wire, so you
cannot validate a constant against the wire you derived it from; it only catches a wrong value that
changes a *captured register write*. `beacon_watch` only catches catastrophic RX loss on the one
channel/scenario tested. We were blind to a whole gap class until a question about the `misc` names
accidentally surfaced it (see Debug log). A severe audit (`SEVERE-AUDIT.md`) cleared the RX/waiver/
EFUSE axes, but that is not a line-by-line proof of the whole driver.

**The comments are the porter's assumptions written as fact, and some are wrong.** e.g.
`0xCA[3:2]=iPA+iLNA on all 3 boots` reads like a measurement but is a wrong inference — the byte is
blank `0xFF`. Any audit must be *comment-blind*: derive expected behaviour from kernel source + real
chip state (extract the real efuse / chip-version from the pcap, never trust the byte a comment
claims), then diff our emitted bytes against it. Treat every `always / never runs / no-op / we skip`
comment as a hypothesis to falsify.

**efuse fields are hardcoded from this one dev card; two are non-default and silently ignored.** We
decode the full 512 B logical map but use only MAC, TX power, crystal, thermal. This card's antenna
option (`0xC9 = 0x03`, programmed) and channel plan (`0xB8 = 0xA2`, programmed) are non-default and
ignored — RX-inert here but wrong for another 8188eus. The PA/LNA option (`0xCA`) is blank `0xFF`, so
"matches internal iPA+iLNA" only via the blank→internal default; a card with `0xCA` programmed
external needs `PHY_SetRFEReg_8188E` ported. The fix is to wire from the map we already hold and
fail-loud (`NotImplementedError` naming the byte) on non-default fields whose handling isn't ported.

**Two more deferred items, non-default but real:** the receiver-blocking NBI notch arms only with
`rtw_adaptivity_en=1` (e.g. ETSI), and powertrack IQK/LCK only fires on ≥8 °C thermal drift.

**The kernel never runs IQK in monitor mode.** Init-time IQK only flags `neediqk_24g` (no
calibration); the deferred IQK fires from link / AP-start / join / sreset — never a monitor-mode
channel hop. The one-shot trigger value (`0xf9000000`/`0xf8000000`) appears as a write zero times in
the whole capture. So `chan.set_channel` skipping IQK is correct, and the `0xe30–0xe8c` writes that
look IQK-adjacent are the BB-config table verbatim.

**Two async 2 s producers interleave the single EP0 stream** (load-bearing for replay):
- An `R REG_SYS_CFG(0xF0)/4` poll fires at arbitrary, non-lock-serialized points — filtered out
  globally as a named, counted waiver (not the silent-reset timer).
- The `rtw_dynamic_chk_wk_hdl` tick is one IO-locked burst per ~2 s (never splits a channel tune):
  silent-reset status poll (`sreset.status_check`) then the no-link phydm watchdog
  (`dig.watchdog_tick`) — both reproduced, not waived; DM state carries across all 22 ticks.

**The mainline port collapses; this one doesn't — that floor is the win.** Same physical card,
fixed-ch1 passive, canary AP (`beacon_watch_usbcap.py` on cold-boot captures): DKMS 86–89% with a
tight floor (min 7, no collapse) vs mainline kernel 83% (min 5) vs our mainline port ~77% with
bad-window collapses (min 3). The live ~6.5-vs-~8.9 bcn/s gap is RF/silicon/environment, not a port
defect.

## Orientation

1T1R 2.4 GHz-only Realtek, vendor request `0x05`, config style phydm `odm_read_and_config` (flat-u32
tables walked by `phy_cond.py`'s conditional parser; same shape as `rtl8814au_dkms`). The hal_init
spine is power-on → MISC01 → FW download → MAC → BB → RF → LLT → MISC02 → turn-on → security →
MISC11 (txpower + RFE) → InitHalDm → IQK/PWtrack/LCK; `driver.connect()` orchestrates it then monitor
entry + channel tune + RxReaderThread.

The phydm RX seed lives in `dm.init_hal_dm` (DIG/NHM/adaptivity + the data-dependent
`phydm_search_pwdb_lower_bound` EDCCA search). Monitor entry is `monitor.enter_monitor` +
`monitor.enable_rx_bar`; channel tune is `chan.set_channel` (stateful `RfRegChnlVal`, seeded from the
M4a RF read). RX decode + RSSI is `rx.py`; TX desc is `tx.py`. The efuse probe-read mechanism uses
the IOL engine reading the physical map out of the TX packet buffer (PKTBUF debug regs `0x140/0x143/
0x144/0x148`), *not* REG_EFUSE_CTRL (`0x30`, never touched). Names match the vendor C, so grep
`usb_dumps_new/captures_8188eu/driver-source/` to cross-reference.

FW blob `assets/rtl8188eufw.bin` is byte-identical (SHA256) to linux-firmware `rtl8188eufw.bin`,
extracted by `scripts/rtl8188eus_dkms/extract_fw.py`.

## Scripts

- `extract_fw.py` — pull the FW blob from the pcap, byte-verify vs linux-firmware.
- `verify_pcap.py` — cold-boot byte gate (cap1/2/3).
- `verify_channels.py` — byte-diff the initial ch1 channel set on all three captures.
- `beacon_watch.py` (live) / `beacon_watch_usbcap.py` (capture) — the RX-health A/B.
- `deauth_hw.py` — live TX smoke test (deauth + reconnect + EAPOL capture).

## Debug log

### 2026-06-16 — the efuse gap class (and why a code-reading audit missed it)

The port replayed byte-for-byte and beacon_watch was healthy, yet a stray question about the `misc`
field names exposed that we hardcode most efuse fields from this one dev card. Decoding the real
bytes (capture-1, via our own `read_chip_params`) showed `0xC9` antenna and `0xB8` channel plan are
*programmed non-default* yet ignored, and `0xCA` PA/LNA is blank `0xFF` — so the "iPA+iLNA matches"
comment was a wrong inference, not a measurement. This is why the audit must be comment-blind: an
agent that reads our code anchors on these poisoned comments and rubber-stamps them. RX-inert on
*this* card, a robustness defect for any other variant. Likely fleet-wide — every driver brought up
against one dev card probably shares the pattern; see `docs/porting/METHODOLOGY.md`.

### 2026-06-?? — RXFLTMAP over-add (top RX-gap lead, fix in code, needs HW A/B)

`monitor.py` had been writing an ungrounded `RXFLTMAP0/1 = 0xffff` (accept every control subtype incl.
ACK) — a write neither the kernel nor the wire makes, suspected of flooding bulk-IN and starving
beacons. The wire's real value is `init_hw_mlme_ext` → `HW_VAR_ENABLE_RX_BAR` = `RXFLTMAP1 |= BIT(8)`
(BlockAckReq only), with RXFLTMAP0 left at reset. The port now does exactly that. The before/after RX
A/B is the remaining human gate before flipping the default.

### 2026-06-?? — NHM divergence on the second watchdog tick

`verify_pcap` caught the phydm tick diverging on the second fire: `phydm_nhm_get_result` reads the
12-bin histogram (`0x8d8/0x8dc/0x8d0/0x8d4`) only when the report-ready bit (`0x8b4 BIT17`) is set, but
the original tick skipped the result reads. First tick's report wasn't ready so it passed; the second
was. Now gated on the ready bit.

### 2026-06-07 — RX/TX proven on hardware

Live TL-WN722N v2: 78 APs / 940 beacons / 1527 frames in a 28 s hop (canary clean). `deauth_hw.py`
injected 300 deauth on ch1 with no pipe fault; the target reconnected and 20/20 captured EAPOL were
to/from it. RX confirmed both directions: 9 M2/M4 (ToDS) + 11 M1/M3 (FromDS) + 262 ToDS data. The
bug that initially blocked bring-up was `firmware.download_firmware` returning `None` on success,
which `connect()` misread as failure — fixed to return a bool.
