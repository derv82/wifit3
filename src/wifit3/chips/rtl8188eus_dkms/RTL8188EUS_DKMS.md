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

Default still ships `mainline`. The RX A/B is nuanced (2026-07-07, see Debug log): the DKMS port is at
**kernel parity fixed-channel on a strong AP** (6.5 vs 6.2 bcn/s) but shows a **real ~18 % gap in the
hopping sweep** (7/6 same-session Kali A/B: 5.3 vs 7.0 on the reference AP, concentrated on weaker
APs) — and mainline had a *smaller* sweep gap than dkms there, so the flip is **not** justified on RX
rate yet. Untested/un-walked: uncaptured TX-desc variants, 40 MHz, power-save, sreset recovery, and
the runtime IQK/LCK/power-track triggers — no wire ground-truth exists for any of these.

## Gotchas

**`verify_pcap` green + `beacon_watch` healthy do NOT mean this matches the kernel line-by-line.**
`verify_pcap` is green by construction — the hardcoded constants (phy_cond driver words, board /
PA-LNA / antenna / channel-plan assumptions) were tuned to reproduce the recorded wire, so you
cannot validate a constant against the wire you derived it from; it only catches a wrong value that
changes a *captured register write*. `beacon_watch` only catches catastrophic RX loss on the one
channel/scenario tested. We were blind to a whole gap class until a question about the `misc` names
accidentally surfaced it (see Debug log). A severe audit cleared the RX/waiver/
EFUSE axes, but that is not a line-by-line proof of the whole driver.

**The comments are the porter's assumptions written as fact, and some are wrong.** e.g.
`0xCA[3:2]=iPA+iLNA on all 3 boots` reads like a measurement but is a wrong inference — the byte is
blank `0xFF`. Any audit must be *comment-blind*: derive expected behaviour from kernel source + real
chip state (extract the real efuse / chip-version from the pcap, never trust the byte a comment
claims), then diff our emitted bytes against it. Treat every `always / never runs / no-op / we skip`
comment as a hypothesis to falsify.

**efuse fields are read live, and the ONE wire-affecting board option is now runtime-gated.** MAC /
TX-power / crystal / thermal all come from the *real* 512 B logical map, not hardcoded. Of the
board-option bytes, most are inert **in this driver build**: the antenna option (`0xC9 = 0x03`) never
reaches the wire (`CONFIG_ANTENNA_DIVERSITY` off, [SRC] autoconf.h:94 — `_InitAntenna_Selection` is a
no-op), the regulatory bits of the board option (`0xC1[2:0]`) are dead code (`CONFIG_TXPWR_LIMIT_EN`
off, Makefile), the channel plan (`0xB8 = 0xA2`) only picks the SW channel list (no register write),
and `rfe_type` (`0xCA[1:0]`) never gates a table row nor changes `PHY_SetRFEReg`'s single case-0 arm.
The *one* byte that changes what the chip is programmed with on another unit is the PA/LNA select
`0xCA[3:2]` (+ its gain-select `0xCA[6:4]`): this card is blank `0xFF` → `iPA+iLNA`, so the reference
walk + a no-op `PHY_SetRFEReg_8188E`. As of 2026-07-11 an **external-PA/LNA burn is ported and
runtime-gated** (`efuse.read_board_options` → `phy_cond.build_driver_words` drives the board-gated
MAC/PHY_REG/AGC/RADIO_A table rows, and `bb.phy_set_rfe_reg` emits the `0x40 / 0xEE8 / 0x87C` writes).
The fail-loud `assert_board_options_ported` is gone. (Verified comment-blind 2026-07-07: all four
captures are the same physical dev card — internal PA+LNA, identical MAC and board bytes; the external
path is source-ported but **hardware-untested** — see the Debug log.)

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

### 2026-07-11 — generalize the EFUSE board options (external PA/LNA), reference byte-identical

Replaced the fail-loud `assert_board_options_ported` with a real runtime decode so the driver runs on
any `2357:010c` card regardless of the `0xCA` burn, not just this internal-PA/LNA dev unit. rf_type is
fixed 1T1R, so the only fuse discriminator that reaches the wire is `0xCA[3:2]` PA/LNA select (+ the
`0xCA[6:4]` GLNA gain-select). Ported 1:1 from the vendor C:

- `efuse.read_board_options` — `Hal_ReadPAType_8188E` (0xCA[3:2] → ExternalPA/LNA_2G) +
  `Hal_ReadAmplifierType_8188E` (0xCA[6:4] → TypeGLNA), AUTO/registry-default path. Returned in
  `ChipParams.board`.
- `phy_cond.build_driver_words` — feeds ExternalLNA/PA → phydm `board_type` (GLNA/GPA) and TypeGLNA →
  `driver2` into `check_positive`, so the **board-gated rows already present** in the MAC/PHY_REG/AGC/
  RADIO_A tables fire on an external card. Empirically confirmed the tables *do* carry ext-LNA/PA
  branches (walk diffs for GLNA/GPA); the internal card's words equal the module defaults → identical
  walk. `walk_table` / `phy_mac_config` / `phy_bb_config` / `phy_rf_config` gained an optional
  `driver_words` arg (default = reference), so positional callers (verify scripts/tests) are unchanged.
- `bb.phy_set_rfe_reg` — `PHY_SetRFEReg_8188E` (MISC11 tail, [SRC] usb_halinit.c:1568), the
  `0x40[3:2]=3 / 0xEE8[28]=1 / 0x87C[0]=0` writes, gated on ExternalPA||ExternalLNA. rfe_type has a
  single case-0 arm so the three writes are rfe_type-independent. No-op on the internal dev card.

`connect()` logs the detected board once (internal = reference; external tagged `[untested variant]`).
Gate: `verify_pcap.py` PASS byte-for-byte on all 4 captures (5769/5829/5752/5661), `phy_set_rfe_reg`
consuming 0 wire ops. `verify_channels.py` is RED both before and after this change (pre-existing: its
width-4-only SYS_CFG filter vs `rf.phy_rf_config`'s foundry `read32` — proven on pristine HEAD, not my
change). Unit tests + ruff (scoped) green.

**Residuals (source-ported, hardware-untested — only the internal card is pcap-gated):** the whole
external-PA/LNA path (RFEReg + board-gated table rows + TypeGLNA gain sub-table) has no wire
ground-truth. Minor faithful omissions, all inert on real cards: the autoload-*fail* TypeGLNA=0x1
quirk (we decode the autoload-OK AUTO path; inert because autoload-fail forces ExternalLNA=0, making
type_glna a don't-care); BT-coex (`EEPROMBluetoothCoexist` → ODM_BOARD_BT) is not parsed but no 88EU
init table gates on the BT bit; 5G ext-PA/LNA (ALNA/APA) is `#if 0` in the vendor. External-PA does
*not* touch the normal TX-power path (only MP-mode `hal_mp.c` reads it), so no txpower gap.

### 2026-07-07 — RX gap: parity fixed-channel, but REAL in the hopping sweep; waiver audit; efuse guard

Autonomous RX-gap + verify-audit pass (4 captures incl. the new `usb_dumps_new2/captures_8188eu`).

- **RX gap: parity on a fixed strong AP, but a REAL ~18% gap in the hopping sweep.** Fixed-channel
  60 s cold soak on the reference AP (−56 dBm, strong), ch1: our DKMS **port** = **6.5 bcn/s (67% of
  the 9.77/s ceiling)** vs the DKMS **kernel** driver's own bulk-IN on the same AP from the 7/6 USB
  capture = **6.2 bcn/s (63%)** — parity. BUT the 7/6 **same-session Kali sweep A/B** (retrieved from
  the VM: `linux-`/`wifit3-rtl8188eusdkms.json`, hops 1–13 @ 15 s, our driver vs the kernel `8188eu`
  on the same box 4 min apart) shows **port 5.3 vs kernel 7.0 bcn/s** on the reference AP — a genuine
  gap (~18 % after the 16 s-vs-14 s span artifact), concentrated on **weaker / adjacent-channel APs**:
  ch1/2 are parity (+0.0/+0.3), ch3–11 are deficits (ch7 −2.4, ch3 −1.7, ch11 −1.5). So the earlier
  "gap closed" read held only fixed-channel-on-a-strong-AP; the hopping / weak-signal case is real.
- **Cause — ruled out so far:** per-hop RX ramp (the reference AP's per-second shape starts strong at
  sec 0 — no leading-low ramp, so it is not a settle/ramp loss); the DIG/AGC watchdog (a Windows
  dig-on vs dig-off sweep is pure noise — breadth 90 vs 92, per-channel deltas balanced both ways);
  the RXFLTMAP flood (the fix `92cdf326` predates the 7/6 run by a month, and the fixed-channel
  before/after is only ±5–7 %, below). Remaining suspects: a runtime RX-pipeline drop on marginal
  beacons, and/or a platform component (our PyUSB driver on Linux vs Windows — today's Windows sweep
  looks healthier: breadth 90, 5–8/s, comparable to/better than the 7/6 linux run). Note mainline had
  a *smaller* sweep gap than dkms on 7/6 (6.1 vs 6.9 = −0.8, vs dkms's −1.7), so a default flip is NOT
  justified on rate. **Definitive next step: a same-session Kali A/B with current code (our driver vs
  kernel on the same box), which needs the card attached to the VM.**
- **Cause — narrowed by live RX-counter instrumentation (no single smoking gun).** Wired the DIG
  watchdog to log, per tick, IGI + FA + the chip's own crc_ok/err counters (already read at
  0xF84/88/90/94 in `_read_fa_counters` and discarded) vs frames we actually deliver. Result: on ch1
  we deliver **97 %** of chip-demodulated frames (ch11 **92 %**), and IGI parks at **0x20–0x22**
  (sensitive; our fixed clamp `[0x1c, 0x2a]` matches the vendor no-link `[dm_dig_min, dig_max_of_min]`
  — verified in `phydm_dig` lines 452/775-798, FA thresholds `{2000,4000,5000}` also match). So it is
  **neither** a USB/software drop **nor** gross DIG deafness. The one real port simplification found:
  our `_new_igi_by_fa` raises IGI on FA count **unconditionally**, but the vendor gates every IGI
  *increase* on `phydm_dig_go_up_check` — an **NHM-noise-histogram** test that blocks the raise (and
  lowers `rx_gain_range_max`) when the noise is broadband/filterable, keeping the chip sensitive. We
  also read the NHM 12-bin histogram each tick in `_nhm` and **discard it** (no NHM→DIG feedback).
  Measured effect: on busy ch11 our IGI stepped 0x20→0x22 where the gated vendor may hold 0x20 — a
  modest ~2-step (~1–2 dB) over-climb. **Correction (same day, deeper source read): that lead is a
  dead end — `phydm_dig_go_up_check` early-returns `true` in `PHYDM_PERFORMANCE_MODE` [SRC]
  phydm_dig.c:50, and this driver assigns `phydm_op_mode = PHYDM_PERFORMANCE_MODE` exactly once and
  never flips it (only SoftAP would use BALANCE) [SRC] hal_dm.c:202. So the gate + its NHM feedback
  are always-true dead code in monitor mode, our unconditional IGI raise is already faithful, and our
  0x20→0x22 step is what the vendor does too. Porting it would be pure dead code, not correctness —
  not done; instead `dig._new_igi_by_fa` now documents the verified omission.** So the DIG is fully
  faithful in monitor; the remaining port-side lead is the small **busy-channel pipeline loss** (ch11
  92% vs ch1 97% of chip-demodulated frames — a URB/buffer-under-load question), the rest being the
  sweep's span/measurement confounds and environment.
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
