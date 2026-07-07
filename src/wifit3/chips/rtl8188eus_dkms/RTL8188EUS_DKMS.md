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

Default still ships `mainline`, but the RX A/B that gated the flip has now run (2026-07-07, see Debug
log): the DKMS port is at **kernel parity** on the reference AP (6.5 vs 6.2 bcn/s), so flipping the
default to `dkms` is a user decision, not a blocked-on-evidence one. Untested/un-walked: uncaptured
TX-desc variants, 40 MHz, power-save, sreset recovery, and the runtime IQK/LCK/power-track triggers —
no wire ground-truth exists for any of these.

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

**efuse fields are read live, but board-option bytes are decoded-and-ignored — and only ONE of them
touches the wire.** MAC / TX-power / crystal / thermal all come from the *real* 512 B logical map,
not hardcoded. Of the ignored board-option bytes, most are inert **in this driver build**: the
antenna option (`0xC9 = 0x03`) never reaches the wire (`CONFIG_ANTENNA_DIVERSITY` off, [SRC]
autoconf.h:94 — `_InitAntenna_Selection` is a no-op), the regulatory bits of the board option
(`0xC1[2:0]`) are dead code (`CONFIG_TXPWR_LIMIT_EN` off, Makefile), and the channel plan
(`0xB8 = 0xA2`) only picks the SW channel list (no register write). The *one* byte that changes what
the chip is programmed with on another unit is the PA/LNA select `0xCA[3:2]`: this card is blank
`0xFF` → `iPA+iLNA`, so `PHY_SetRFEReg_8188E` early-returns and our static RF/AGC replay matches — but
a card with an external PA or LNA needs `PHY_SetRFEReg_8188E` [SRC] rtl8188e_phycfg.c:1993 (writes
0x40 / 0xEE8 / 0x87C) **and** the external-LNA AGC table, neither ported. `efuse.
assert_board_options_ported` now fails loud (`NotImplementedError` naming `0xCA`) on any
external-PA/LNA unit rather than running silently mis-tuned. (Verified comment-blind 2026-07-07: all
four captures are the same physical dev card — identical MAC and identical board bytes.)

**Two more deferred items, non-default but real:** the receiver-blocking NBI notch arms only with
`rtw_adaptivity_en=1` (e.g. ETSI), and powertrack IQK/LCK only fires on ≥8 °C thermal drift.

**The kernel never runs IQK in monitor mode.** Init-time IQK only flags `neediqk_24g` (no
calibration); the deferred IQK fires from link / AP-start / join / sreset — never a monitor-mode
channel hop. The one-shot trigger value (`0xf9000000`/`0xf8000000`) appears as a write zero times in
the whole capture. So `chan.set_channel` skipping IQK is correct, and the `0xe30–0xe8c` writes that
look IQK-adjacent are the BB-config table verbatim.

**Two async 2 s producers interleave the single EP0 stream** (load-bearing for replay):
- The `R REG_SYS_CFG(0xF0)/4` reads are all identified and reproduced now — chip-version at probe,
  the TX-power-track foundry read at the RF-config tail, and the per-tick `phydm_receiver_blocking`
  read — **none is waived** (the earlier "global SYS_CFG waiver" is gone; verify_pcap.py has no
  SYS_CFG filter).
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

### 2026-07-07 — RX gap is at kernel parity; waiver audit; efuse guard landed

Autonomous RX-gap + verify-audit pass (4 captures incl. the new `usb_dumps_new2/captures_8188eu`).

- **RX gap effectively closed — it was the environment, not the port.** 60 s cold soak on the
  reference AP (−56 dBm, strong), fixed ch1: our DKMS **port** live = **6.5 bcn/s
  (67% of the 9.77/s ceiling)**; the DKMS **kernel** driver's own bulk-IN on the same AP from the 7/6
  USB capture (`beacon_watch_usbcap.py`) = **6.2 bcn/s (63%)**. Two independent implementations hit
  the same ~63–67% on a *strong* AP → the loss is on-air congestion (39 APs on ch1), not a port
  defect. The doc's old "5.3 vs 7.0 (76%)" is stale (different environment/measurement). A −74 dBm AP
  out-scored the −56 dBm reference AP in the same run → not sensitivity. **Recommendation: the RXFLTMAP-fix
  "flip default to dkms" gate can be considered satisfied — the port matches the kernel.**
- **RXFLTMAP before/after (monkeypatched, not committed):** re-adding the pre-`92cdf326`
  `RXFLTMAP0/1/2 = 0xffff` ACK-flood gave the reference AP 6.2/s vs the fixed 6.5/s and 4003 vs 4294 all-AP
  beacons — a real but **marginal +5–7%**, not the theorized big lever. Keep the fix (faithful +
  slightly better); it is not the whole story.
- **verify_pcap waiver audit (all 4 PASS):** the only waived ops are aireplay-ng's injected TX —
  all bulk-OUT sits in the capture tail (82–91% in), zero bulk-OUT between init-end and the first
  injected frame (the vendor driver TX's nothing in NOLINK monitor), and the waived `REG_TX_RPT_TIME`
  writes are the firmware RA's timestamp-derived values that aireplay's TX triggers (the init
  `0xCDF0`/`0x3DF0` writes are *matched*, not waived). Nothing vendor-bring-up is wrongly waived. A
  true zero-waiver gate would need a *passive* cold-boot capture (no aireplay) — none of the 4 are.
  Open idea: byte-check the vendor 32 B TX-descriptor prefix of each injected frame (waive only the
  aireplay 802.11 payload) so the injected txdesc stops being 100% unverified offline.
- **efuse:** `assert_board_options_ported` added (fail-loud on external PA/LNA `0xCA[3:2]≠3`). See
  the Gotchas rewrite: `0xC9`/`0xC1`/`0xB8` are inert in this build; `0xCA` is the only wire-affecting
  board byte and this dev card is internal (`0xFF`). All 4 captures still replay byte-for-byte.

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
