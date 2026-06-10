# RTL8814AU (Alfa AWUS1900) — Protocol Notes

**Status:** Ported, RX HW-validated — graded **C** in `VERIFICATION.md` (full attack
matrix passes; RX path complete, 0/100 cold boots deaf). The DKMS sibling
`rtl8814au_dkms` is the manager default (it fixes the weak mainline 2.4 GHz signal).
Still open: calibrated full-power TX (the M6 plan below) and the known 2.4 GHz RX
weakness.

Card: Alfa **AWUS1900**, AC1900, **4T4R** (4 RF paths), 2.4 + 5 GHz.
Chip: Realtek **RTL8814AU**, kernel module `rtw88_8814au`, `RTW_CHIP_TYPE_8814A`.
Family: **rtw88** (modern), shares `chips/rtw88_base/`.

---

## M6 — TX power (calibrated full-power): execution-ready plan (2026-05-26)

**Why:** TX-at-distance is core (secondary only to RX-at-distance). Today we
inject at the BB/AGC-table baseline power — uncalibrated — which kneecaps us vs
aireplay. **Scope (user-approved): calibrated FULL power** — port EFUSE
power-by-rate base + by-rate offset + bb_swing + the `REG_AGC_TBL` write;
transmit at the chip's full calibrated power. **Deliberately skip** the 8
regulatory power-limit table variants (they only *cap* power — an auditing tool
wants max range), `pwrtrack` (thermal drift), and IQK-before-TX. 8814au-local
first; generalize to `rtw88_base` when the 2nd driver needs it.

**Offline verification (the win):** the cold-boot pcap captured the kernel's
exact per-channel `REG_AGC_TBL` writes. Diff our `set_tx_power_index` output
against it to prove the **write encoding** (per-rate/path `txagc_table_wd`, the
DESC_RATE1M double-write, rate-section order). Power *values* will be ≥ the pcap
(we skip the regulatory cap) — that's intended. Hardware: user confirms deauth
range/strength (needs RF, can't self-verify).

**EFUSE power layout — RESOLVED exact** (logical map; `struct rtw8814a_efuse`):
`txpwr_idx_table[4]` at **0x10**, **42 B/path** = 2G(18) + 5G(24):
  - 2G (`rtw_2g_txpwr_idx`): `cck_base[6]`, `bw40_base[5]`, `ht_1s_diff`(1B:
    ofdm:4|bw20:4), `ht_2s/3s/4s_diff` (2B each: bw20:4|bw40:4|cck:4|ofdm:4).
  - 5G (`rtw_5g_txpwr_idx`): `bw40_base[14]`, `ht_1s_diff`(1B: ofdm:4|bw20:4),
    `ht_2s/3s/4s_diff`(1B each: bw20:4|bw40:4), `ofdm_diff`(2B:
    ofdm_3s:4|ofdm_2s:4|ofdm_4s:4|res:4), `vht_1s/2s/3s/4s_diff`(1B each:
    bw160:4|bw80:4). Diffs are **little-endian, signed 4-bit** (x<8 ? x : x-16).
  Plus `tx_bb_swing_setting_2g`@0xc6, `_5g`@0xc7, `thermal_meter`@0xba.

**Steps (each independently verifiable; commit per step):**
1. **EFUSE power parse** — extend `efuse.py`: parse the 42 B/path table + bb_swing
   bytes into a `TxPowerIdx` dataclass. Verify: dump indices, sanity-range check.
2. **By-rate offset** — `rtw_phy_get_tx_power_index` adds `tx_pwr_by_rate_offset`
   (chip default, region-independent) on top of the EFUSE base. Extract the
   8814a by-rate table from `rtw8814a_table.c` as an asset (reuse
   `extract_init_tables.py` pattern + phy_cond walker), or confirm base-only is
   adequate by pcap-diff.
3. **Computation** — new `tx_power.py`: port `rtw_phy_get_2g/5g_tx_power_index`
   (channel-group map `rtw_get_channel_group` → base + EFUSE per-rate diffs +
   by-rate offset), clamp to `max_power_index=0x3f`. Skip limit/SAR/remnant.
4. **Write** — port `rtw8814a_set_tx_power_index[_by_rate]`: `txagc_table_wd =
   0x00801000 | (pwr<<24) | (path<<8) | rate`, all rate sections per path,
   DESC_RATE1M double-write. Pcap-diff the encoding.
5. **bb_swing** — `get_bb_swing` (`swing2setting[4]={0x200,0x16a,0x101,0x0b6}`
   indexed by 2 bits/path of the EFUSE bb_swing byte) → `REG_TXSCALE_A/B/C/D`
   (0xc1c/0xe1c/0x181c/0x1a1c, mask GENMASK(31,21)); fold into `_switch_band`.
6. **Wire** — call the TX-power level set after `phy_set_param` (connect) and per
   `set_channel`. HW: deauth still works + range test.

---

## Correctness audit — what's ported vs deferred (2026-05-26)

**RX path — COMPLETE & validated** (hardware + pcap byte-diff): FW upload, MAC
init, full BB/AGC/RF×4 init tables, EFUSE, crystal_cap, config_trx_path, channel
tune (matches kernel byte-for-byte), `spur_calibration` (ported), DIG watchdog
(ported), band-switch RF re-lock recovery (fixed). Result: 0/100 cold boots
deaf, no channel-hop death.

**RX caveat found 2026-05-31 (HW):** 2.4 GHz RX is weak/miscalibrated. At one
spot a 5 GHz AP read −54 dBm while a 2.4 GHz AP read −82 dBm, and the 2.4 GHz
beacon rate was 0.5–2/s vs ~10/s on 5 GHz; 5 GHz RX is healthy. So the
"validated" RX above held for 5 GHz / the cold-boot capture, but a **2G-specific
RX gap** slipped through — suspect the 2G RX path / AGC / gain or LNA setup in
`switch_band`, the 2G `crystal_cap`/spur path, or a 2.4-GHz RSSI miscalc (though
the low beacon *rate*, not just RSSI, points at real sensitivity loss, not just a
display bug). ARP replay + ChopChop nonetheless worked on 2.4 GHz the same
session (RX could hear ChopChop's relays), so attack TX/RX functioned despite the
weak 2.4 GHz reception.

**TX path — FUNCTIONAL but NOT power/IQ-calibrated.** Deauth/injection is
hardware-confirmed (kicks a phone off, EAPOL re-capture), but TX runs at the
**BB/AGC-table baseline power** — we do not do the kernel's per-channel TX-power
chain. Deferred, in rough priority if we ever want calibrated/long-range TX:
  - `rtw_phy_set_tx_power_level` / `set_tx_power_index` — per-channel + regulatory
    TX power (the 536-entry `REG_AGC_TBL` reload seen per-hop in the pcap). We
    never set it → TX power is whatever the init tables left (works at close
    range; not regulatory-calibrated; weaker at distance).
  - `set_channel_bb_swing` — per-band TX swing scaling (TXSCALE A/D). TX-only,
    confirmed no RX impact.
  - `pwrtrack` — thermal TX-power compensation (drifts with temperature).
  - IQK before TX (`do_iqk` via `mgd_prepare_tx`) — TX/RX I/Q calibration; the
    kernel runs it once before TX (pcap: 4 LOK before aireplay). We skip it;
    fine at the robust 1M/6M MGMT rates we inject at, worse EVM at high rates.
  Impact for a passive auditing tool (scan + deauth + handshake): acceptable —
  TX works. Matters for max-range deauth, high-rate TX, or regulatory power.

**Other intentional limits (fine for this tool's purpose):**
  - **20 MHz only** — no 40/80 MHz tune. Beacons/handshakes ride the primary
    20 MHz so scanning/capture is unaffected; wide-band data payloads partly missed.
  - **Non-DFS 5 GHz only** (36-48, 149-165) — DFS 52-144 excluded; passive
    monitor *could* listen there, we just don't tune to them.
  - **Deferred MAC regs** in `phy_set_param` (HWSEQ/BAR/NAV/QUEUE/FWHW_TXQ =
    TX/protocol timing; MISC_CTRL `DIS_SECOND_CCA` = minor CCA tweak) — not
    needed for monitor RX + basic MGMT injection.

---

## SEVERE AUDIT — 2026-05-26: RF-deaf ~50% at boot + RX silence after channel hop

Analysis only (no fixes yet), grounded in kernel source (`data_dumps/rtw88-source-v6.18/`)
and the 3 cold-boot pcaps. Driving concern: the `connect()` "re-roll
phy_set_param up to 8×" retry loop is a band-aid — if the kernel/airmon is
reliable and we're 50/50, we are **skipping a step the kernel does**, not hitting
unavoidable hardware flakiness. [[feedback_no_bandaids_root_cause]]

### What the pcaps can and cannot tell us (important scoping result)

All 3 captures (`capture-{1,2,3}.pcap`) are **~5260 frames / ~417 KB spanning only
the first 2–18 s** = the **plug-in cold-boot init** (kernel auto-probes
`rtw88_8814au` on insert: power-on → iDDMA FW upload → EFUSE dump (1024 reads @
0x0030) → early MAC regs). The USB capture **stops at ~18 s, before the first
`iw set channel` (epoch 247)**. Confirmed via `pcap_slicer.py` (every `iw set
channel` maps to N/A frames) + `extract_pcap_writes.py` (tail = low MAC regs, no
large BB/AGC/RF table load, no calibration).

Consequences:
- **`phy_set_param` (BB/AGC/RF tables, RF enable, calibration), monitor setup,
  and channel hops are NOT in any capture** — they happen at interface-up, after
  the capture ended. So the flaky part (M3 PHY/RF) is *unvalidated by pcap*; for
  it the **kernel source is the only authority** until we recapture.
- **0/3 captures show a card reboot / re-init after a failed start.** The kernel's
  init succeeds first try, every time. This supports: *the kernel is deterministic
  and we are missing a stabilizing step* — not "the hardware is 50/50 and the
  kernel recovers."
- **TODO recapture:** a longer USB capture that includes `airmon-ng start` +
  several `iw set channel` + dwell, so we can diff the kernel's per-hop register
  burst (commands + inter-command latency) — the data this audit could not get.

### Q1 — Why does cold boot come up RF-deaf ~50%?  (ranked)

1. **[PRIMARY] No DIG / dynamic-mechanism watchdog.** Kernel runs
   `rtw_phy_dynamic_mechanism` every **2 s** (`RTW_WATCH_DOG_DELAY_TIME = HZ*2`,
   main.h:30) → `rtw_phy_dig` walks the OFDM **initial gain index (IGI)** from the
   false-alarm count and **writes it** (`rtw_phy_dig_write`, phy.c). It also runs
   `rtw_phy_cck_pd`, cfo/dpk/pwr track, adaptivity. **We run NONE of it** — IGI is
   left at the AGC-table default forever. Whether that fixed default lets RX hear
   depends on the boot analog gain state → **50/50**, and re-rolling
   `phy_set_param` "works after a few tries" because each re-roll is a fresh
   lottery on that state. This single gap explains the band-aid's behaviour.
   *Status: **CONFIRMED + FIXED 2026-05-26.** Ported the DIG watchdog
   (`dynamic.py`): seed IGI=DIG_CVRG_MIN (0x1c, max coverage) at each bring-up
   attempt + a 2 s loop that walks IGI from the FA count (no-link/coverage path
   of `rtw_phy_dig`). HW: **8/8 consecutive bring-ups came up on attempt 1,
   RECEIVING** (CRC-OK 119–351), zero deaf, zero re-rolls — vs the prior ~50%.
   Caveat: warm software re-inits (the deaf retry historically fired on those
   too); the truly-cold physical-replug case still wants user confirmation
   before the 8× re-roll band-aid is removed.*
2. **[MED] `rtw_phy_init` skipped** (rtw8814a.c:335). *Status: investigated —
   `rtw_phy_init` is almost entirely software bookkeeping (zeroing FA/IGI history,
   `cck_pd_init` sets dm_info state only) with **no hardware writes** except
   *reading* dig[0] to seed the starting IGI; `adaptivity_init` only writes HW for
   ETSI/JP regd. So skipping it left no HW gap — its only role (the IGI seed) is
   now handled by `dynamic.dig_init`.*
3. **[MED→FIXED] `rtw8814a_config_trx_path` skipped** (rtw8814a.c:311): clears
   `REG_CCK0_FAREPORT` 2RX|MRC, sets CCK RX→path B. *Status: now ported into
   `phy.config_trx_path`, called from `phy_set_param`. (Consistently-wrong path
   config would be consistently bad, not 50/50, so it wasn't the 50/50 cause —
   but it's a real faithfulness gap, now closed. crystal_cap→AFE_CTRL3 was also
   added in the same pass.)*
4. **[LOW] No RF/PLL settle or lock-poll after RF power-on.** Neither kernel nor we
   poll, but the kernel emits ~thousands more writes (full phy_init, init_rfe_reg,
   MAC regs) between RF-enable and RX, giving de-facto settle time we skip.
   *Status: weak; unquantified.*
5. **[DISCONFIRMED] Missing IQK.** Kernel **defers** IQK for monitor/scan
   (`need_rfk=true`; `phy_calibration` only fires from `mgd_prepare_tx` / `start_ap`
   — main.c:907–921, mac80211.c:473). Passive monitor RX runs without per-channel
   IQK, so missing IQK does not kill RX (hurts TX EVM). Not the deaf cause.
6. **[DISCONFIRMED] Missing `adc_clk`.** `rtw8814a_adc_clk` returns early for
   non-A cuts (rtw8814a.c:751). Our sample is **CUT_B** (`SYS_CFG1=0x044411b5`),
   so correctly skipped.

### Q2 — Why does RX go silent 5–10 s after a channel switch?  (ranked)

1. **[PARTIAL] Missing DIG watchdog, post-tune.** `set_channel` now re-seeds IGI
   to max coverage on each hop + the 2 s watchdog re-converges. This helps a
   *manual* channel change (instant max sensitivity on the new channel), but does
   **not** fix aggressive hopping — DIG runs on a 2 s cadence, irrelevant to a
   0.25 s dwell. *Status: addressed for slow/manual changes; not the fast-hop cause.*
2. **[PRIMARY — open] PLL relock blanks the radio; cadence vs dwell.** HW with the
   DIG fix still shows ~1 s `frames/s=0` windows at the TUI's 0.25 s hop interval.
   The 4-path relock + AGC re-settle eats most of a 250 ms dwell, so few/no frames
   land per channel. This is a **dwell-time** problem, not a gain problem — the
   lever is the hop interval (kernel/airmon dwell ≥1 s in the captures and are
   fine), or a post-tune RX-resume verify. `set_channel` still never re-verifies RX.
3. **[CONTRIBUTING] TUI hops every 0.25 s** (`on_screen_resume`, interval=0.25).
   Per the user's plan, the remaining hop flakiness is the "go fetch fresh captures"
   branch (now that `capture.py` uses `usbmon0`, a recapture will actually contain
   the hop sequence — see below).
4. **[LOW] `rtw8814a_spur_calibration` skipped** (rtw8814a.c:1117) — per-spur-channel
   NBI/CSI notch; only affects specific channels, would not cause general silence.

### Cross-cutting note

The headless harness (`scripts/rtw88_8814au/measure_rx_load.py`) **cannot reproduce
the user-visible 5–10 s silence** — it only ever saw ~1 s gaps and bounded loop
slip (≤28 ms, 0 drops, 0.1 ms dispatch). So the RX-dispatch/loop path is *not* the
cause; verification of the real silence stays a HW/TUI task for the user. The
lag the user saw once is shelved (likely Textual latency, not card-specific).

### Outcome + remaining work

- **DONE — DIG watchdog** (`dynamic.py`, wired into `driver.py`): seed IGI to
  max coverage at bring-up + 2 s FA-driven IGI walk. 8/8 warm re-inits came up
  first-try RECEIVING (was ~50% deaf). The 8× re-roll band-aid is left in place
  as a safety net pending **user confirmation on cold physical replugs**; remove
  it once cold boots are confirmed reliable.
- **DONE — capture.py bus fix:** `USBMON` was hardcoded to `usbmon3`; a
  FW-loading adapter re-enumerates onto a different bus after firmware boot, so
  every capture stopped dead at ~5260 frames (init burst only). Switched to
  `usbmon0` (all-buses meta-interface) + `lsusb` topology logging. Untested by
  agent (Linux/Kali only) — ready for the next capture run.
- **RESOLVED 2026-05-26 — channel-hop "death" = intermittent RF deaf on band
  switch.** Root cause: on a 2G↔5G change the RF front-end fails to re-lock
  ~30-50% of the time and comes up deaf (**CCA=0**, energy detection upstream of
  gain — so beacons stop entirely on the new band; "going to 5G revives 2G").
  Proven via `repro_band_death.py` (CH44↔CH1) + PHY-counter layer probe; ruled
  out per-hop extras (worse without), a missing register (`switch_band` matches
  the kernel byte-for-byte; bb_swing/pwrtrack are TX-only), and timing (80 ms
  settle didn't help). Fix (`driver.set_channel`): on a band change, verify
  CCA>0 over a 40 ms window and force a fresh `switch_band` re-tune if deaf (≤4×)
  — the same verify+recover `connect()` uses for the cold-boot deaf, justified
  by it being genuine analog re-lock variance, not a port gap. **HW: repro
  0/15 + 0/12 (was 3-7/10); general band-crossing `--hop` 0 NO-FRAMES/20 s
  (was 3-5); one re-lock recovers every deaf case.**
- **cold-boot RX death ("1 beacon then silent") — two contributing causes:**
  bulk-IN delivers the startup backlog once then goes silent while the BB keeps
  decoding (death3 log: `crc_ok` 50-200/window, `bytes=0`) — an RX-DMA delivery
  halt, not RF.
  1. **RX aggregation ON** (FIXED): kernel runs aggregation OFF in monitor
     (`REG_RXDMA_AGG_PG_TH=0x0100`, size=0/timeout=1); we ran it ON (`0x2005`),
     whose page accumulator could fail to re-arm. Matched the kernel
     (`configure_rx_aggregation`, immediate flush). Cut the rate (10/10) but
     **not eliminated** — attempt #11 reproduced it with agg off.
  2. **Undrained 2s RF probe** (FIXED 2026-05-27): `connect()` ran the
     `rf_receiving_frames` 2 s probe with the reader **not yet started**, so RX
     decoded ~1000 frames into a bulk-IN no one was draining → device RX path
     backs up → RX-DMA halts. Fix: start the reader BEFORE the probe (drain
     throughout, as the kernel always does). **Needs cold-boot HW verify.**
  Lesson: don't retire diagnostics on a 10/10 sample for a ~1/11 bug (the
  opt-in `WIFIT3_RX_STATS` produced/bytes log is back for this reason).

### Known gaps / tech-debt (parked — see correctness audit above)

- **TX power (M6) — NOT calibrated.** Biggest gap: we inject at the BB/AGC-table
  baseline power, not the EFUSE-calibrated per-channel power, so TX is weaker than
  aireplay at distance. Scope decided (calibrated full-power) and EFUSE step 1
  (power-by-rate parse) landed; steps 2-6 (by-rate offset, computation, REG_AGC_TBL
  write, bb_swing, wiring) parked. See "M6 — TX power" plan above.
- **Two recovery-style fixes not fully root-caused** (acknowledged band-aids; the
  cold-boot/agg + DIG fixes ARE faithful state-matches, these two are not):
  1. **8× phy_set_param re-roll in `connect()`** — now likely unnecessary since
     cold boots are 10/10 with DIG + agg-off; flagged for removal/verification.
  2. **Band-switch CCA re-lock in `set_channel`** — detect-deaf-and-re-tune. Fixed
     the band-hop death (0/15) but is detect/recover; `switch_band` matches the
     kernel byte-for-byte, so it's *probably* genuine analog re-lock variance, but
     that wasn't confirmed against a kernel band-transition capture (the 3 caps
     lacked enough 2G↔5G transitions). Revisit with a band-transition capture.
- **5 GHz non-DFS only; 20 MHz only** — by design (see correctness audit).
- **Unit tests: pure-logic covered; register-sequencing not.** `tests/chips/
  rtw88_8814au/` (28 tests) covers the bitfield/parse logic — EFUSE power-by-rate
  parse + logical-map walker, jaguar RSSI decode, DIG IGI walk, TX-desc/deauth.
  Still hardware-only (no mock-transport tests): the register *sequencing*
  (`phy_set_param`, `chan.set_channel`/`spur_calibration`, `mac_init_for_rx`,
  power-seq) — validated via `test_hw_8814au.py` + the pcap diff.

### pcap byte-level findings — 2026-05-26 (full captures, usbmon0)

Fresh captures (`capture-{1,2,3}.pcap`, 25–32 MB each, now complete through
init+airmon+hops+inject). `pcap_slicer.py` (now phase-aware, with a per-phase
frame count) + `extract_pcap_writes.py --min-frame/--max-frame` give exact
per-phase register bytes. Capture-1 frame map: init 1–10948, airmon 10949–20406,
airodump 20407–88479, then 1 s iw hops, aireplay, 0.25 s fast-hops.

1. **Cadence/hardware cleared, decisively.** Kernel `iw` fast-hops at **0.25 s
   captured ~1250–1560 USB frames *per hop*** (airodump 20 s = 68 k frames). The
   kernel is healthy at our exact cadence ⇒ the post-hop death is **purely our
   userland `set_channel`/RX path**, not cadence or relock-vs-dwell. The earlier
   "PLL relock blanks the radio" framing is wrong as a *root* cause.
2. **Per-hop tune sequence MATCHES the kernel.** Kernel `set channel 6` writes
   CLKTRK(0x0860) fc_area, per-path RF_CFGCH (0x0c90/0e90/1890/1a90), CCK TX
   filter (0x0a20/24/28), WMAC_TRXPTCL(0x0668)/DATA_SC(0x0483), bw_rf — all of
   which `chan._switch_channel`/`_set_bw_mode` already emit. **Crucially the
   kernel tunes RF_CFGCH live with NO RX-DMA pause/reset and RX keeps flowing**,
   so "tuning without resetting RX" is NOT the wedge.
3. **The 536× `REG_AGC_TBL`(0x1998) per-hop writes are the TX-power table**
   (`txagc_table_wd`, rtw8814a.c:1288/1294, via `rtw_phy_set_tx_power_level`),
   not RX — irrelevant to the death (we don't TX while hopping).
4. **[CORRECTS PRIOR CLAIM] The kernel runs IQK at monitor bring-up.** Earlier I
   disconfirmed missing-IQK from source ("deferred to mgd_prepare_tx"). The pcap
   overrules that: full IQK fires in the airmon/interface-up window (frames
   ~13939+: trigger writes 0x1b00=0xf8.., 0x1b04, 0x1b1c — the LOK/TX/RX one-shot
   pattern of `rtw8814a_iqk`). It runs **once at bring-up, NOT per hop** (no
   0x1b00 in any per-hop window — matches "calibration during scan takes too
   long"). **We run zero IQK.** Real gap; affects RX/TX I/Q quality, worth porting.
5. **We skip `spur_calibration` per hop** (NBI 0x087c / CSI 0x0874 + 0x0880-089c
   block) — confirmed present in every kernel hop, absent from ours. Per-channel
   notch; quality, not a wedge.
6. **PHY init + IQK happen at interface-up (airmon), not at probe.** The airmon
   window even repeats the iDDMA FW-chunk dance (0x0204/0x0101/0x0550) — airmon
   stop/start re-runs a full bring-up. (Architecture note; our single connect()
   does power-on→FW→efuse→mac→phy in one shot, which is fine.)

### 2026-05-26 follow-up: spur ported; IQK BLOCKED on driver mismatch

- **DONE — `spur_calibration` ported** (`chan.py`, per-hop in `set_bw_mode`,
  rfe-gated): full 1:1 of all branches + 2.4G NBI + reset path. HW: RX still
  first-try, no regression. Did **not** change the hop death (expected — notch
  filter, not RX on/off).
- **IQK — NO monitor-RX gap (earlier "blocked/vendor-mismatch" claim RETRACTED).**
  I first mistook the pcap's `0x1b04/1b10/1b18/1b1c/1b20-1b3c` block for a
  runtime IQK that mainline lacks. It is not: those are entries in the
  **`rtw8814a_bb` BB init table** (rtw8814a_table.c:~4053; the `0x9000000N`
  rows are phy_cond conditionals), which `phy_set_param` loads — and **our
  `bb_tbl.py` already contains them (14/14 vs mainline)**. The extractor
  collapses table writes, which is what fooled me. The *runtime* `do_iqk`
  (LOK/TX/RX one-shot; fingerprint `0x1bd4=0x003f0001`) is **absent from the
  whole monitor portion** (count 0 ≤ frame 20406) and appears only **4× (per
  path) right before the aireplay TX** — i.e. exactly mainline's "IQK deferred
  to `mgd_prepare_tx`". So: monitor RX needs no IQK, we already match mainline,
  and the Kali driver behaves like mainline rtw88 (lwfinger/rtw88's
  `rtw8814a.c` LOK is byte-identical to mainline; morrownr's halrf LOK too).
  Only open IQK item: the kernel runs `do_iqk` *before TX*; we don't before
  `inject_frame` — a TX-quality nicety (deauth already works), not the death.

**Net:** the death is in OUR code, and the pcap (kernel) can't show it directly.
Suspects now (need Windows-HW toggling, not more pcap): (a) our **RX aggregation**
desyncing on rapid retune; (b) our **extra per-hop writes** the kernel never does
(`rx.tune_monitor_cck_sensitivity` + `dynamic.dig_init` re-seed); (c) WinUSB
reader-thread ↔ control-transfer contention. Separate faithfulness win available:
**port `rtw8814a_do_iqk`** (run once after phy_set_param at connect), per finding #4.

---

## Build status

- **M1.a (FW extract + verify)** — ✅ DONE (offline). Pcap-extracted blob is a
  byte-for-byte match to linux-firmware. See §1.3.1.
- **M1 (FW upload + FW_READY)** — ✅ **DONE 2026-05-26, HW-VERIFIED.** First-try
  pass on the AWUS1900. `test_hw_8814au.py` cold boot:
  - `REG_SYS_CFG1 = 0x04441135` → **CUT_B**, cut_mask 0x04 (note for M3: PHY/RF
    tables are cut-gated — this sample is a B-cut).
  - `REG_MCUFW_CTRL` pre-FW `0x00602001` → post-upload `0x00606078` →
    post-validate `0x0060e078` (IMEM/DMEM DW_OK+CHKSUM_OK, FW_DW_RDY, FW_INIT_RDY).
  - 68256 bytes (DMEM 5792 + IMEM 62464) uploaded in 44 ms; no EALREADY cycle.
  - Enumerated at **bcdUSB 0x0200 (USB 2.0)** — see Known Gaps #7; matters for
    M5 RX throughput, not M1. Host needed Zadig→WinUSB binding for `0bda:8813`.
- **M2 (TRX init: queue mapping + FIFO + LLT + H2C)** — ✅ CODE COMPLETE,
  offline-verified (imports; FIFO math reproduces the kernel reserved-page
  invariant rsvd_boundary == rsvd_drv_addr = 1986; pubq 1858; txdma_pq_map
  0xf5b0 for 3-bulkout; 537 tests pass). **Awaiting HW gate** —
  `test_hw_8814au.py --phase mac_init` (LLT auto-init must clear + H2C ring
  verifies). Scope: `rtw_init_trx_cfg` only (`fifo.py`). The rest of
  `rtw8814a_mac_init` — the `mac_tbl` load + EDCA/SIFS/beacon timing — and
  `rtw_drv_info_cfg` are deferred: EDCA/mac_tbl belong with TX (M6), drv_info
  (RX physts + rxdesc-len quirk) with RX (M5). Bulk-OUT count is detected at
  runtime (`count_bulk_out_eps`) → selects `rqpn_table_8814a` row.
- **M3.a (init tables + 4-path RF access)** — ✅ CODE COMPLETE, offline-verified.
  All 7 tables extracted (`extract_init_tables.py`): mac/agc/bb + rf_a/b/c/d,
  ~29k u32s, phy_cond markers balanced (IF==ENDIF, IF+ELIF==neg for each).
  Chip-local `rf.py` (direct read + sipi write, 4 paths) — see Decisions #1.
  MAC-table replay dispatches 143 writes; RF read/write address math verified
  for all 4 paths; 537 tests pass. **Awaiting HW gate** —
  `test_hw_8814au.py --phase tables` (replay MAC table, read back sample regs).
- **M3.b (phy_set_param: BB/RF enable + conditional table loads + 4-path RF
  readback)** — ✅ CODE COMPLETE, offline-verified. `phy_set_param` (phy.py):
  BB/RF domain enable (4 paths), MAC+BB+AGC+RF(A-D) table loads via the walker,
  A->B/C/D RCK copy, RX-PSEL bracket. Skips EFUSE/tuning bits (crystal_cap,
  config_trx_path CCK antenna, DIG, pwrtrack, init_rfe_reg) — not needed for RF
  bring-up; deferred to M5/M6. **Awaiting HW gate** — `--phase phy`: RF_RCK1_V1
  must read back consistent + non-garbage on all 4 paths.
  - **Caveat — rfe_option is a placeholder (=1) until M4.** The AGC/RF tables are
    rfe-gated (IF/ELIF chains on rfe 0x01..0x0b). Verified the walker selects
    rfe-specific *data* correctly (rfe=2 vs rfe=3 load different values; the
    identical dispatch *count* of 265 is because every branch has equal entry
    count — NOT a walker bug). A wrong rfe loads a valid-but-suboptimal gain
    variant; M4 pins the real value from EFUSE.
- **M4 (EFUSE read)** — ✅ CODE COMPLETE, offline-verified. `efuse.py`: grant +
  1024-B physical dump + word-enable de-map to the 512-B logical map + parse
  `struct rtw8814a_efuse` fields (rfe_option 0xCA, rf_board_option 0xC1,
  xtal_k 0xB9, USB MAC 0xD8). `rfe_option` resolved per `rtw8814a_read_rfe_type`
  (bit7→USB=1, else raw). Wired into `driver.connect()` (read before
  phy_set_param) and `phy_set_param` now uses the **real** rfe_option, retiring
  the M3.b placeholder. De-map verified on synthetic 1-byte + 2-byte-header
  blocks; rfe resolution verified; 537 tests pass. **Awaiting HW gate** —
  `--phase efuse`: decode rfe/MAC/xtal, assert MAC non-garbage.
- **M3.c (channel tune, 20 MHz)** — ✅ CODE COMPLETE, offline-verified. `chan.py`
  ports rtw8814a_set_channel for 20 MHz: switch_band (rfe pinmux 2G/5G + CCK/TX/
  RX-psel + bw_reg adc/agc), switch_channel (per-path RF_CFGCH + CLKTRK fc_area
  + AGC sub-band), cck_tx_dfir, set_bw_mode. **Deferred** (not needed for 20 MHz
  monitor RX): bb_swing/pwrtrack (TX power → M6), adc_clk (A-cut only; no-op on
  our B-cut), spur_calibration (per-spur-channel NBI/CSI notch), 40/80 MHz.
  First tune uses `force_band=True` to establish rfe pinmux (we skip the
  kernel's init_rfe_reg in phy_set_param). Wired into `driver.set_channel` +
  initial ch1 tune in connect(). HW gate (`--phase channel`) = the tune SEQUENCE
  executes cleanly across 2G/5G/5G-high. **Functional validation is M5** (RF
  actually receiving on the tuned channel).
  - **[HW] These PHY/channel registers are write-and-forget — do NOT validate by
    readback.** Confirmed against the cold-boot pcap: the kernel NEVER reads back
    RF_CFGCH / CCK_CHECK / CLKTRK / AGC_TABLE (it writes them blind), and on
    hardware several don't read back as written (RF_CFGCH→const 0xEA;
    CCK_CHECK/CLKTRK/AGC→const 0x575/0xa/bit7 regardless of channel). RF *writes*
    do land (proven by the RF 0x1c RCK readback via the same write path).
  - **[WIRE] The three captures are init-only** (all end ~frame 5260, right after
    FW init + airmon start; identical 58 write-addrs, zero channel-reg writes).
    So there is NO pcap ground truth for channel tune OR RX — both must be
    validated live, not by pcap-diff. (Relevant for M5: RX desc decode comes from
    kernel rx.c + rx_common.py + the 8822bu sibling, not a pcap.)
- **M5 (RX / monitor)** — ✅ CODE COMPLETE, offline-verified. `rx.py`:
  `mac_init_for_rx` (RXFLTMAP + RX_DRVINFO_SZ + rxdesc-len quirk + promiscuous
  RCR_MONITOR 0xf410400f + USB burst) — the RX-side MAC init deferred from M2;
  `apply_monitor_rcr`; `iter_bulk_frames` over the shared 24-byte rx_pkt_desc
  decoder. Driver wires the shared `RxReaderThread` (reader-thread, not
  event-loop polling, per [[project_rx_loop_ui_starvation]]) + endpoint probe,
  stops it before USB release.
  ✅ **DONE 2026-05-26, HW-VERIFIED**: beacon captured + decoded end-to-end
  (bulk-IN → 24-B rx_desc → MPDU → parser → SSID), CR alive 0x4ff, RCR landed
  0xf410400f. Also confirms M3.c's tune on-air.
  - **[RESOLVED] Sensitivity** — was 1 beacon/9s (closest AP only); root cause
    was the CCK packet-detect threshold sitting at the insensitive table default
    (kernel tunes it via a dynamic watchdog we don't run). Pinned REG_CCK_PD_TH
    to LV0 + enabled 2R-CCA/MRC → 64 BSSIDs/9s. See `rx.tune_monitor_cck_sensitivity`.
  - **[RESOLVED] Intermittent RX** — was two compounding ~50% cold-boot issues,
    both fixed:
    1. **RX not frame-aligned** → `iter_bulk_frames` parsed ~0 (chip delivered
       20 KB, parser got ~1 frame). Root cause: **RX aggregation was never
       enabled.** `rtw_usb_dynamic_rx_agg_v1` (BIT_RXDMA_AGG_EN + REG_RXDMA_AGG_PG_TH
       size=5/to=0x20) makes the chip flush bulk transfers on frame boundaries
       (each starts with an rx_pkt_desc), as the kernel's rx_handler requires.
       See `rx.enable_rx_aggregation`. (Also dropped `prime_bulk_in`'s
       clear_halt/drain — it desynced the active stream.)
    2. **RF-deaf** (CCA=0, PHY hears nothing) → re-running `phy_set_param`
       re-rolls the analog lock; `connect()` retries until CRC-OK>0
       (`rx.rf_receiving_frames`). Diagnosed via `--phase rxdiag`/`rxdump` (PHY
       false-alarm/CRC/CCA counters): bad boots showed CCA=0, ruling out
       DMA/IQK. HW: a deaf start auto-recovers in 1 re-init.
    **HW-VERIFIED reliable: 66–70 BSSIDs every run across cold + warm boots.**
  - **[RESOLVED] RSSI** — ported rtw8814a_query_phy_status (jaguar phy_status
    report in the 32-B drv_info): OFDM = 2nd-lowest of the 4 per-path gains −110;
    CCK = AGC LNA/VGA lookup (`rx.parse_phy_status_rssi_8814a`). HW: own AP
    ~−28 dBm, neighbours −54..−86 dBm (sensible distribution). Wired into
    `iter_bulk_frames`.
  - **[BUG fixed] test phase-gating** — `--phase rx` had skipped fw/validate/
    mac_init/efuse (missing from the `needs_*` sets), so the MAC was never
    powered → CR read 0xEA. Replaced with an ordered chain (run everything up to
    the target phase). NOT a driver bug — driver.connect() always ran M1→M5 in
    order; the EFUSE grant-off "fix" made on the wrong theory was reverted.
- **M6 (TX inject)** — ✅ CODE COMPLETE; TX pipe HW-verified. `tx.py`: 40-byte
  tx_pkt_desc (10 u32; NO DATARATE_FB_LIMIT since old_datarate_fb_limit=false,
  unlike the 8812a 40-byte builder) + MGMT→HIGH lane (out_ep[0]=0x02) +
  bulk-OUT writer + deauth builder. Wired into `driver.inject_frame`. HW: 10/10
  bogus-target deauths accepted by the bulk-OUT pipe (desc decodes correct:
  pkt_len/OFFSET=40/QSEL=MGMT/RATE_ID=CCK). **Pending: on-air deauth effect**
  (user's phone test) — TX queues were armed in M2 (priority_queue_cfg + LLT),
  so the frame should go out; just needs RF-confirmation a real client drops.
- **M7 (monitor-mode / no-RX-filter verification)** — ✅ **DONE, HW-VERIFIED.**
  `--phase monitor` classifies each frame's addr1; PASS requires frames whose
  receiver is a unicast MAC that is neither broadcast/multicast nor our own
  (traffic to OTHER stations). HW: **379 frames to 35 other stations** in a ~9 s
  sweep → card is truly promiscuous, no RX address filtering. Cross-cutting
  concern (several cards silently filter): should generalise into a shared check
  across all drivers per [[project_driver_gap_audit]].

## 0. TL;DR for the lead

The single most important finding: **the 8814A's closest already-ported sibling
is the 8822BU, not the 8812AU.** They share `RTW_WCPU_3081` + the iDDMA segmented
firmware path. The legacy 8812au/8821au (8051 + MCUFWDL) path is the *wrong*
reference for the firmware/MAC bring-up.

Practical consequence:
- **Firmware + MAC bring-up** ≈ adapt `chips/rtl8822bu/` (firmware.py, mac.py).
  Mostly reuse, plus one new segment (EMEM, see §3).
- **PHY + RF** is where the real 4×4 delta lives: `rtw8814a_table.c` is **23,930
  lines** (8× the 8812a's 2,812). That's the bulk of the work, but it's
  *mechanical* — flat-u32 tables ported 1:1 per [[feedback_constants_from_source]].
- **`rtw88_base/rf_sipi.py` only knows path 'a'/'b' today** — 8814A needs C & D
  (it `raise`s ValueError otherwise). Concrete, small base-layer change.

---

## 1. Verified recon facts

### 1.1 Chip spec — `rtw8814a_hw_spec` (`rtw8814a.c:2180`)

| Field | Value | Note for us |
|---|---|---|
| `.wlan_cpu` | `RTW_WCPU_3081` | **RISC core, not 8051** → iDDMA FW path (same as 8822b/8821c/8822c) |
| `.fw_name` | `rtw88/rtw8814a_fw.bin` | single blob, **no WoWLAN fw** |
| `.tx_pkt_desc_sz` | 40 | TX desc 40 B (8822b is 48; 8812a is 40) — **verify against pcap** |
| `.rx_pkt_desc_sz` | 24 | matches `rtw88_base/rx_common.py` 24-B decoder |
| `.rx_buf_desc_sz` | 8 | |
| `.phy_efuse_size` | 1024 | EFUSE read (M4) |
| `.txff_size` | (2048-10) × `TX_PAGE_SIZE` | FIFO partition (M2) |
| `.rxff_size` | 23552 | |
| `.band` | 2G \| 5G | |
| `.max_power_index` | 0x3f | |
| `.sys_func_en` | 0xDC | |
| `.rf_base_addr` | `{0x2800, 0x2c00, 0x3800, 0x3c00}` | **4 paths** A/B/C/D |
| `.rf_sipi_addr` | `{0xc90, 0xe90, 0x1890, 0x1a90}` | **4 paths** A/B/C/D |
| `.rf_tbl` | `{rf_a, rf_b, rf_c, rf_d}` | 4 RF init tables |
| `.pwr_on_seq` | `card_enable_flow_8814a` | port table (M2) |
| `.pwr_off_seq` | `card_disable_flow_8814a` | warm-reattach only; **don't** replicate per [[feedback_warm_reattach]] |
| `.usb_tx_agg_desc_num` | 3 | `REG_AUTO_LLT_V1` config |

### 1.2 The WCPU_3081 / iDDMA firmware path

`mac.c:982 _rtw_download_firmware` branches on CPU type:
- `rtw_chip_wcpu_8051()` → `__rtw_download_firmware_legacy` (MCUFWDL — what 8812au uses)
- else (3081) → `__rtw_download_firmware` → `start_download_firmware`
  (`mac.c:697`) → segmented `download_firmware_to_mem` per segment via
  `iddma_download_firmware` (`mac.c:574`).

Firmware header (`validate_fw_hdr`, `mac.c:~410-438`) declares **three** segments:
`dmem_size`, `imem_size`, and **`emem_size`** (present iff `fw_hdr->mem_usage &
BIT(4)`). Each segment gets a 4-B checksum appended (`FW_HDR_CHKSUM_SIZE`).

`rtw_chip_wcpu_3081` also gates, in `mac.c`:
- `REG_H2CQ_CSR = BIT_H2CQ_FULL` after `MAC_TRX_ENABLE` (`mac.c:1125`)
- the modern `__priority_queue_cfg` (FIFOPAGE_INFO regs) vs legacy RQPN (`mac.c:1295`)
- reserved-page layout incl. H2CQ/CPU-instr/fw-txbuf pages (`mac.c:1166`)
- `init_h2c()` H2C ring setup (`mac.c:1301`, no-op on 8051)
- `rtw_drv_info_cfg` "rxdesc len = 0" `REG_TRXFF_BNDY+1 |= 0xF` quirk (`mac.c:1378`)

### 1.2.1 Port gotcha — FW-upload TX descriptor size [SRC]

The wire shows a **40-byte** TX descriptor per FW chunk (= `tx_pkt_desc_sz`),
which is what drives both `build_fw_tx_pkt_desc` and the iDDMA source offset
(`download_firmware_to_mem`, mac.c:650 `desc_size = chip->tx_pkt_desc_sz`).
BUT `send_firmware_pkt` (mac.c:550) computes its ZLP-avoidance `%512` decision
against the kernel's **hardcoded `#define TX_DESC_SIZE 48`** (mac.c:528), NOT the
chip's real descriptor size. For the 8822b these happen to be equal (48); for
the 8814a they differ. Our `firmware.py` keeps them separate: `TX_PKT_DESC_SZ`
(40) for the descriptor + iddma offset, `FW_DLFW_ZLP_TXDESC` (48) for the ZLP
check. For the current blob no chunk actually triggers the +1, but the split is
the faithful port and is robust if the FW changes.

### 1.3 Chip-ops surface — `rtw8814a_ops` (`rtw8814a.c:2050`)

Relevant to scan/monitor/inject (coex/BT ops omitted — not needed):

| op | 8814a fn | maps to |
|---|---|---|
| `power_on/off` | `rtw_power_on/off` (generic) | `rtw88_base/power_seq.py` + ported table |
| `phy_set_param` | `rtw8814a_phy_set_param` | BB/RF init — **M3**, the big one |
| `mac_init` | `rtw8814a_mac_init` | **M2** |
| `read_efuse` | `rtw8814a_read_efuse` | **M4** |
| `query_phy_status` | `rtw8814a_query_phy_status` | RSSI decode — **M5 (RX)** |
| `set_channel` | `rtw8814a_set_channel` | 4-path tune — **M3** |
| `read_rf/write_rf` | `rtw_phy_read_rf` / `..._sipi` | `rtw88_base/rf_sipi.py` **+ path C/D** |
| `set_tx_power_index` | `rtw8814a_set_tx_power_index` | **M6 (TX)** |
| `fill_txdesc_checksum`| `rtw8814a_fill_txdesc_checksum` | `rtw88_base/tx_common.py` — **verify XOR layout for 40-B desc** |

### 1.3.1 Firmware upload — VERIFIED [WIRE]

Extracted from capture-1 and byte-diffed against the linux-firmware blob
(`scripts/rtw88_8814au/extract_rtw8814a_fw.py`):

- **BYTE-FOR-BYTE MATCH**: pcap-reassembled FW == `rtw8814a_fw.bin[64:]`.
- Upload endpoint: **bulk-OUT EP 0x02** (out_ep[0]) — the only OUT pipe with
  >1 KB chunks during bring-up.
- TX descriptor on each chunk: **40 bytes** (confirms `.tx_pkt_desc_sz`).
- Segments: **DMEM 5784 B** (@0x80200000) then **IMEM 62456 B** (@0x80000000),
  each + 8-byte checksum on the wire. **No EMEM.**
- 18 chunks total, pcap frames **279..897** (inside the init window 1..5266).
- The 64-byte `rtw_fw_hdr` never appears on the wire — driver reads it to learn
  segment sizes/dst-addrs, then uploads bodies only.

### 1.4 Captures (ground truth)

`usb_dumps/captures_rtw88_8814au/` — 3 pcaps + `*_logs/main.log`.
- **capture-1 = cold boot.** `pcap_slicer` shows `<hardware_plugin_and_initialization>`
  = **frames 1–5266** (this is where FW upload lives). Channel sweep reaches
  ch165 → confirms dual-band 4×4.
- capture-2 = likely warm boot (kernel skipped FW load — usual pattern).
- capture-3 = second cold/attack capture.

> ⚠️ The `aireplay-ng.log` in these capture dirs contains **real BSSID/client
> MACs** from the capture environment. Per [[feedback_no_ssids_in_commits]],
> those never enter committed code, comments, or this doc. Placeholders only.

---

## 2. Reuse map — what we already have vs. net-new

| Concern | Source of truth | wifit3 reuse | Net-new for 8814a |
|---|---|---|---|
| Vendor control xfer | `rtw88_base/transport.py` | ✅ full | thin `transport.py` subclass |
| iDDMA segmented FW upload | `rtl8822bu/firmware.py` | ✅ DMEM+IMEM+H2CQ+checksum | **+ EMEM segment** (§3) |
| power_seq runtime | `rtw88_base/power_seq.py` | ✅ runtime | port `card_enable_flow_8814a` table |
| phy_cond walker | `rtw88_base/phy_cond.py` | ✅ (scalar-rfe) | confirm 8814a rfe encoding |
| RF SIPI read/write | `rtw88_base/rf_sipi.py` | ⚠️ path a/b only | **add path C/D** (addrs in §1.1) |
| RX desc decode | `rtw88_base/rx_common.py` | ✅ 24-B desc | confirm RSSI/phy-status offsets |
| TX checksum | `rtw88_base/tx_common.py` | ✅ XOR | **verify 40-B desc field layout** |
| MAC init (modern) | `rtl8822bu/mac.py` + `mac.c` | ✅ H2CQ/prioq/init_h2c | 8814a `mac_init` specifics |
| BB/AGC/RF tables | `rtw8814a_table.c` (24k lines) | — | **port 1:1** (M3, mechanical bulk) |
| 4-path channel tune | `rtw8814a_set_channel` | — | **net-new** (M3) |
| EFUSE | `rtl8822bu` efuse path | partial | 8814a `read_efuse` (M4) |

---

## 3. Known gaps & risks (audit before declaring any milestone done)

1. ~~**EMEM segment.**~~ **RESOLVED — no EMEM.** Parsed the real blob
   (`assets/rtw8814a_fw-linux_firmware.bin`): `mem_usage=0x08` (BIT(4) clear), so
   only **DMEM (5784 B @ 0x80200000) + IMEM (62456 B @ 0x80000000)** — the exact
   two-segment shape `rtl8822bu/firmware.py` already uploads. Signature `0x8814`,
   v33.6.0, computed size 68320 == file size. M1 is near-pure 8822bu reuse.
   Note: segment dst addresses come from the FW header fields, not hardcoded —
   confirm M1 reads `dmem_addr`/`imem_addr` from the blob (kernel does).
2. **bulkout_num.** `priority_queue_cfg` selects `page_table[2/3/4]` by USB bulk-OUT
   endpoint count. AWUS1900 likely exposes 4 bulk-OUT. **Confirm endpoint
   descriptors from the pcap / live device** before M2.
3. **rf_sipi path C/D.** Base layer raises on path != a/b. Must extend before any
   RF write on path C/D (M3). Keep path A/B behaviour byte-identical for the
   existing 8812au/8821au/8822bu drivers — this is a shared file.
4. **Always-monitor deviation** per [[feedback_monitor_mode_deviation]]: kernel
   inits for STA mode. After init, M5 must set RCR / RX_FILTR_CFG for promiscuous
   monitor and skip address-match. Audit explicitly; don't assume kernel defaults.
5. **RX loop on event loop** per [[project_rx_loop_ui_starvation]]: build the RX
   reader on the shared reader-thread helper (`chips/rx_reader.py`) from day one,
   not the polling pattern.
6. **40-B TX desc.** `tx_common.py` XOR checksum assumes a desc layout; 8814a is
   40 B. Verify field offsets vs `rtw8814a_fill_txdesc_checksum` before M6.
7. **USB speed** per [[feedback_usb_speed_check]]: AWUS1900 is USB-3 branded —
   probe `bcdUSB` + `wMaxPacketSize` before assuming anything about URB sizing.

---

## 4. Milestone plan

Each milestone ends with a **hardware-test gate**: agent prepares
`scripts/rtw88_8814au/test_hw_8814au.py`, user unplugs/replugs, runs it, the
script self-reports PASS/FAIL. A milestone is **DONE only when verified on
hardware** per the session loop in CLAUDE.md. Methodology: tiny first step,
pcap-diff every step before declaring done ([[feedback_bringup_methodology]],
[[feedback_port_completeness]]).

### M1 — Firmware upload + FW_READY ACK
*Goal: blob lands in the 3081 core and the chip reports ready. No PHY.*
~Demoable, mirrors 8822bu M1.

- **M1.a** Offline: linux-firmware blob is in `assets/rtw8814a_fw-linux_firmware.bin`
  (header parsed — DMEM+IMEM, no EMEM, see §3.1). Extract the blob from capture-1
  (mirror `scripts/rtl8812au/extract_rtw8812a_fw.py`) and byte-verify it matches
  the linux-firmware copy. Pin exact FW-upload frame sub-range via `pcap_slicer`.
- **M1.b** Scaffold `chips/rtw88_8814au/`: `driver.py` (`SUPPORTED_IDS`,
  `SUPPORTED_CHANNELS`, `from_usb_device`), `transport.py`, `constants.py`,
  `firmware.py`. Register in `wlan/manager.py:_all_drivers()`.
- **M1.c** Port power-on seq (`card_enable_flow_8814a`) + reg backup/restore.
- **M1.d** Adapt iDDMA upload (reuse 8822bu — DMEM+IMEM only, dst from header);
  H2CQ; `download_firmware_validate`.
- **🔌 HW gate:** `test_hw_8814au.py` uploads FW, polls `REG_MCUFW_CTRL`, prints
  PASS on FW_READY (`0xC078` per `mac.c:285`).

### M2 — MAC init + FIFO/queue config
*Goal: TRX engine alive, LLT init OK, queues mapped. Still no PHY/RX.*

- **M2.a** Confirm `bulkout_num` from device descriptors → page_table/rqpn index.
- **M2.b** Port `rtw8814a_mac_init` + modern `__priority_queue_cfg` + `init_h2c`
  + `rtw_drv_info_cfg` (incl. rxdesc-len quirk).
- **M2.c** FIFO partition (`rtw_set_trx_fifo_info`, 3081 reserved-page layout).
- **🔌 HW gate:** script runs M1→M2, asserts `REG_AUTO_LLT_V1` auto-init
  completes + `check_hw_ready` passes; no error path hit.

### M3 — PHY/BB/RF init + channel tune (the 4×4 bulk)
*Goal: PHY parameterised, all 4 RF paths up, can tune to a channel.*
Split into three independently HW-gated sub-milestones (lead's call — stop &
verify often to avoid churn on the largest milestone).

- **M3.a — tables.** Port `rtw8814a_table.c` (mac/agc/bb/rf_a/b/c/d) 1:1 →
  `*_tbl.py`. Also extend `rtw88_base/rf_sipi.py` for path C/D (additive; a/b
  byte-identical — no impact to other rtw88 drivers).
  - **🔌 HW gate:** replay MAC+BB tables, read back a sample of written regs,
    assert they stuck (table-replay smoke test; no RF/channel yet).
- **M3.b — PHY param.** Port `rtw8814a_phy_set_param` (phy_cond walker + table
  replay + 4-path RF table load via SIPI).
  - **🔌 HW gate:** run full phy_set_param, read back RF regs on all 4 paths,
    assert non-garbage / expected init values.
- **M3.c — channel.** Port `rtw8814a_set_channel` (4-path tune).
  - **🔌 HW gate:** tune ch1 / ch36 / ch149, read back channel regs per path.

### M4 — EFUSE read
*Goal: read rfe_option / power-by-rate / chip cuts; feed PHY init.*

- **M4.a** Port `rtw8814a_efuse_grant` + `rtw8814a_read_efuse` (1024-B phy efuse).
- **M4.b** Wire EFUSE values into M3 init (rfe_defs selection).
- **🔌 HW gate:** dump decoded EFUSE, sanity-check rfe_option/MAC addr non-garbage.

### M5 — RX (sniff) + monitor mode
*Goal: live frames in the TUI scanner.*

- **M5.a** RX desc decode via `rx_common.py`; confirm RSSI/phy-status offsets
  against `rtw8814a_query_phy_status`.
- **M5.b** Monitor-mode filter rewrites (RCR/RX_FILTR_CFG) per gap #4.
- **M5.c** RX reader-thread (shared helper) per gap #5.
- **🔌 HW gate:** scan ch1/ch6, assert N beacons / M BSSIDs in a fixed window
  (the 8821au "27 BSSIDs/8s" style check), pcap-diff vs airmon capture.

### M6 — TX inject (deauth) → full attack stack
*Goal: deauth recaptures a handshake live — the "DONE" bar for every chip.*

- **M6.a** Port TX desc build + verify 40-B XOR checksum (gap #6).
- **M6.b** `set_tx_power_index` (EFUSE-sourced, regulatory) + arm TX queues.
- **M6.c** Inject deauth on EP for mgmt; confirm on-air with a 2nd known-good card.
- **🔌 HW gate:** deauth a test client, recapture EAPOL M1+M3 / PMKID live.

After M6: mark the card DONE (with date) in `VERIFICATION.md` and add it to the
README supported-cards table.

---

## 5. Decisions (lead, resolved)

1. ~~**rf_sipi path C/D** → extend `rtw88_base/rf_sipi.py` in place.~~
   **REVERSED in M3.a → chip-local `rf.py` instead.** The "extend in place" call
   assumed 8814a reads RF like its 8812au/8821au cousins. It doesn't:
   `rtw8814a_ops.read_rf = rtw_phy_read_rf` is a **direct MMIO read**
   (`read32(rf_base_addr[path] + addr*4)`), same as the 8822b — NOT the 3-wire
   HSSI/PI/SI read that `rf_sipi.py` implements for the 8812a/8821a. So 8814a
   gets its own `rf.py` (direct read + sipi write, 4 paths); the shared file is
   untouched (lower risk than the original plan). `.write_rf` is the shared
   `rtw_phy_write_rf_reg_sipi` semantics, just indexed by `rf_sipi_addr[path]`.
   [Decision confirmed with lead during M3.a.]
2. **EMEM** → **none.** Confirmed by parsing the real blob (§3.1). M1 = DMEM+IMEM
   only, near-pure 8822bu reuse.
3. **M3 granularity** → **split into M3.a/.b/.c** (tables / phy / channel), each
   with its own HW gate. Stop-and-verify per sub-step to minimise churn.

---

## 6. Citations index

- `data_dumps/rtw88-source-v6.18/rtw8814a.c` — chip ops, hw_spec
- `.../rtw8814a_table.c` — BB/AGC/RF init tables (23,930 lines)
- `.../rtw8814au.c` — USB ID table (lead VID:PID `0x0bda:0x8813`)
- `.../mac.c` — iDDMA FW download (697), wcpu_3081 gates (281/1125/1166/1378)
- `.../main.h:1181` — `enum rtw_wlan_cpu` (3081 vs 8051)
- wifit3 `chips/rtl8822bu/firmware.py` — iDDMA reuse reference
- wifit3 `chips/rtw88_base/{rf_sipi,rx_common,tx_common,power_seq}.py`
