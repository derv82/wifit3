# RTL8812AU — vendor/DKMS port ground truth

Cleanroom port of the **RTL8812AU / 2T2R** (ALFA AWUS036ACH, `0bda:8812`) from the same
**Lucid-Duck `8821au-20210708` 5.12.5.2** multi-chip `rtl88xxau` vendor source the 8821au
port used (it implements 8812a in `hal/rtl8812a/` + `hal/phydm/rtl8812a/`). Built on the
shared **`chips/rtl88xxau_base/`** jaguar core (proven by the 8821au replay-diff) plus the
8812a-specific deltas. **No vendor 8812 cold-boot pcap exists**, so verification is:
shared base (8821 replay-diff) + golden-hashed 8812 init tables + structural cross-check
vs the mainline `captures_rtw88_8812au/` + **live HW on the AWUS036ACH**.

- **Sibling, not a replacement.** The mainline `chips/rtl8812au/` stays as the fallback.
  Manager wiring behind `$WIFIT3_RTL8812` is **M8 (not yet done)**.
- **8821au is frozen** — this port reuses `rtl88xxau_base/` (copies of the 8821's
  zero-delta core); the shaped modules (mac/bb/chan/txpower/dig/monitor/efuse/rx) are
  per-chip and ported fresh from the 8812a vendor C.
- **Scope:** 20 MHz primary only. SW-seq fragmentation not reintroduced (hwseq only).

## Hardware facts (AWUS036ACH, live)

- **USB endpoints DIFFER from the 8821au:** bulk-IN **0x81** (8821 = 0x84), **three**
  bulk-OUT **0x02/0x03/0x04** (8821 = 0x09, four), int-IN 0x85. The transport probes
  bulk-IN dynamically so RX reads the right EP; but **TX (M6) must use bulk-OUT 0x02**,
  not the base default 0x09, and the MAC queue-priority must use the **3-out-EP** mapping
  (`_InitNormalChipThreeOutEpPriority`), not the 4-EP `0xC5A0` M2 currently writes. Both
  are TX-side (M6) so they did not block the RX milestones — **fix before M6**.
- **REG_SYS_CFG (0xF0) = 0x04411137** — cut nibble [15:12] = 1 → **B-cut** (not C-cut),
  so the C-cut spur / rCCAonSec SIPI paths are off.
- **EFUSE (live decode):** crystal_cap (0xB9) = **0x0e**, MAC (0xD7) = `00:c0:ca:ba:be:b5`
  (ALFA OUI), **rfe_type (0xCA) = 3**, bb_swing 2.4G = 0 dB (0x200) / 5G = −3 dB (0x16a),
  both paths. 2-path PG decoded (path-A 2.4G cck_base[0]=0x13, path-B=0x19).
- **Firmware:** `array_mp_8812a_fw_nic`, 27030 B, header sig 0x9501, first body byte 0x02.

## Milestone status

| M | Scope | Live gate | Status |
|---|---|---|---|
| M0 | `rtl88xxau_base/` extract + 8812 tables + FW (golden-hashed) | tables hash-stable | **done** |
| M1 | Power-on + FW download → FW-ready | REG_MCUFWDL=0x000607c6 (WINTINI_RDY) | **done** |
| M2 | MAC init (`_8812AUsb` variants) → REG_CR | REG_CR=0xff (MACTXEN\|MACRXEN) | **done** (3-EP queue fix pending — see above) |
| M3 | BB/RF init incl RADIO_B (2T2R) | xtal field; RF[A/B,0x00] distinct, both respond | **done** |
| M4 | 2.4 GHz tune, both paths | RF[A/B,0x18] ch1@20MHz | **done** |
| M-TXPWR | EFUSE 2-path + per-rate TX power | EFUSE sane; TXAGC = per-path PG (A=0x13, B=0x19) | **done** |
| M5 | 2.4 GHz RX + RSSI + DIG | **beacons heard** | **BLOCKED — RX demod not working** |
| M6 | 2.4 GHz TX (deauth + WEP) | live TX (user) | not started |
| M7 | 5 GHz RX + tune + TX | live 5 GHz beacons | not started (chan.py has 2.4G only; `_switch_band_5g` raises) |
| M8 | manager wiring `WIFIT3_RTL8812` | discovery | not started |
| M9 | A/B + flip default | A/B | not started |

## M5 RX blocker (the open problem)

**Symptom:** the RX hardware path works — frames are DMA'd to the host on bulk-IN 0x81 —
but the demodulator outputs **pure garbage**: every frame has the HW CRC-error bit set,
and the MPDU contents are random (invalid frame-control bytes, random BSSIDs, random
lengths up to ~2 KB). With the monitor RCR (0x9000382F, ACRC32 cleared) the HW drops all
of them → **0 frames**; only a permissive RCR (accept CRC errors) lets the garbage
through. The receiver is **deaf to real 802.11 signal** — not marginally corrupting real
beacons, but failing to lock onto them at all.

**What is verified working at M5:**
- The full bring-up chain (M1–M-TXPWR) runs clean every time.
- Monitor entry sets RCR=0x9000382F, RXFLTMAP0/1/2=0xFFFF, MSR=NOLINK (all read back OK).
- **The DIG/AGC watchdog works:** IGI seeds at 0x20, adapts 0x20→0x1c as the no-link FA
  count settles from 65535 (initial reset) to a healthy ~140/2 s. So gain control is live.
- RX path reg 0x808 = 0x3e028233 (CCK-enable BIT28 set).

**Ruled out:**
- **Not rfe_type** — tested rfe_type 0, 1, and 3; all deaf.
- **Not gain saturation** — the DIG settled the gain (FA low) and still 0 real frames.
- **Not the bulk-IN endpoint / RCR / RXFLTMAP** — all confirmed correct by readback.

**Leading suspects for the next investigation (in rough priority):**
1. **Incomplete `rtw_phydm_init`.** `rtl8812_InitHalDm` = `dm_InitGPIOSetting` +
   `rtw_phydm_init` → `odm_dm_init` (phydm.c:1786). The current `dig.init_hal_dm` ports
   only a subset (GPIO, CCK-PD, NHM, LNA-enable, OFDM RX-gain). `odm_dm_init` also runs
   `phydm_supportability_init`, `phydm_rfe_init`, **`phydm_rx_phy_status_init`**,
   `phydm_dig_cckpd_coex_init`, `phydm_adaptivity_init` (the PWDB-EDCCA search — ported
   off), `phydm_rf_init`, `phydm_dc_cancellation`, etc. One of these (esp.
   `phydm_rx_phy_status_init` or the adaptivity/EDCCA init) may be the RX-demod enable.
2. **RX IQ calibration (`PHY_IQCalibrate_8812A`).** The 8821 port skipped IQK and still
   RX'd; the 8812 may genuinely need RX IQK to demodulate (uncalibrated I/Q → garbage).
   This is the strongest "garbage demod" suspect.
3. **AGC table / 2.4G RX path.** The `array_mp_8812a_agc_tab_diff_lb/hb` tables are not
   applied; confirm whether the main AGC table alone suffices, and whether a 2T2R OFDM RX
   path enable (rRxPath, vs the rTxPath the band switch sets) is missing.

**Diagnostic to run first on a clean boot:** capture the **mainline `chips/rtl8812au/`
baseline** (ch1, 30 s). It confirms the antenna/HW hear beacons at all (isolating the
fault to the DKMS RX init) and is the M5/M9 A/B target. This could not be captured this
session — the card wedged after repeated bring-up cycles and needs a physical replug;
the mainline driver's warm-reattach reported the bulk-IN stalled.

## M5 update — register diff vs the working mainline (session 2)

The decisive diagnostic: a **raw register diff (DKMS vs the working mainline driver**, both
read by `ctrl_transfer(0xC0,0x05,addr,…)`). Result: **my config is byte-identical to mainline
across ~50 BB/MAC RX registers.** The HW is fine — mainline reads **28 APs / 2918 beacons**
ch1/25 s on the same cold card (canary `NETGEAR2G` −31 dBm ~9.8/s; this is the M5/M9 A/B target,
and notably NOT bottom-tier). The only register differences:
- `0xC50/0xE50` IGI: mainline 0x1c, mine 0x20 (dynamic — the watchdog reaches 0x1c).
- `0x10C` TRXDMA_CTRL: mainline **0xe5f4**, mine **0xc5a4** — the queue→DMA map; my M2 wrote
  the **4-EP** mapping but this card is **3-EP** (see fix-ups below).
- `0x114` TX page boundary: mainline 0xf7, mine 0xf9 (likely just rtw88's own choice).
- RFE/RX-path/AGC (`0xCB0/0xCB4/0xCB8/0x900/0x808/0x824/0x82C/0x8AC/…`): **all identical.**

**Forcing IGI=0x1c AND TRXDMA_CTRL=0xe5f4 to mainline's values — still 0 frames.** So the
BB/MAC config is not the cause. Newly **ruled out** this session: path-B interference (forced
1T1R via `PHY_BB8812_Config_1T` — still deaf, so path A itself is broken); `board_type` (0xD8);
IGI; the TRXDMA queue map; IQK (commented out in the vendor init, usb_halinit.c:1404/1674/1814);
`rRxPath` (already 0x33 = both paths).

**Start the next session here:** the one thing NOT yet compared is the **RF (radio) register
state** (LNA/mixer/PLL) — BB regs matching does not prove the RF regs do. The RF-reg SIPI diff
(raw read via the 0x8B0 latch + 0xD04/0xD08 readback works on both drivers) was set up but the
card wedged before it ran. Leading hypotheses now that BB is excluded: (1) **RF receiver chain /
PLL lock** — RF[0x18] reads ch1 but the synth may not be locked / an LCK or RX-RF enable is
missing ("noise demod" = wrong LO); (2) **RX DMA/FIFO vs the vendor firmware** (mainline rtw88
and the DKMS run different firmware).

## M2 fix-ups needed (TX-side; surfaced during M5 debug)

This card is **3 bulk-OUT (0x02/0x03/0x04) + 1 bulk-IN (0x81)**, not the 8821's 4+1:
- M2 `_InitQueueReservedPage` / `_InitQueuePriority` must use the **3-out-EP** path
  (`OutEpQueueSel=HQ|LQ|NQ`, `OutEpNumber=3`) → TRXDMA_CTRL should land on 0xe5f4, not the
  4-EP `0xC5A0` the code writes now.
- M6 TX must send on bulk-OUT **0x02** (the base transport default 0x09 is the 8821's).

## M5 update — RF register diff + the "RF won't enter receive mode" wall (session 3)

**RX is PROVEN achievable: forcing the RF (radio) registers to the working mainline
driver's values yields 22 APs (canary −56 dBm).** So the port can RX; the blocker is
isolated to the RF state. Findings, in order:

- **RF-register SIPI diff (DKMS vs working mainline):** RF[A/B] 0x00, 0x30, 0x31, 0x32,
  0x42, 0x65, 0xb0, 0xb1 differ; all BB/MAC/RFE registers match (incl. 0xCB0/0xCB4/0xCB8
  RFE pinmux, 0x40/0x4C GPIO, 0x808/0x860/0x874).
- **`_lna_setting` (in `dig.init_hal_dm`) is a bug:** it writes RF 0x30/0x31/0x32 =
  0x18000/0x3f7ff/0xc26bf. The vendor only calls `halrf_rf_lna_setting_8812a` INSIDE the
  EDCCA search, not unconditionally. Remove it from the deterministic init path.
- **cut logic (confirmed against vendor `check_positive`, halhwimg8812a_bb.c):** the cut
  check is raw **equality** (not a bitmask), with `cut_version_for_para = (cut==ODM_CUT_A)
  ? 15 : cut`. So A-cut→15, **B-cut→1**, C-cut→2. Our walker is missing the A-cut→15
  remap and the NGFF(bit1→5)/TRSWT(bit5→6) board_type bits — port both. This chip is
  B-cut → `cut_version=1`.
- **EFUSE external-LNA/PA:** 0xBC=0x33, 0xBD=0x88, 0xBF=0x88 → ExternalLNA/PA on both
  bands, all Type fields = 0 → ODM board_type = 0xD8 (GLNA bit4|GPA bit3|ALNA bit7|APA
  bit6). Setting board_type=0xD8 makes the RADIO walker resolve the **external** gain
  branch and fixes RF 0x65 to mainline's 0x931d1.
- **BUT board_type=0xD8 makes RX WORSE, not better** — it fails even with the RF regs
  forced, while board_type=0 (internal gain branch) + forcing works (weakly). Read: the
  external branch lowers internal gain expecting the external LNA's boost; if the external
  LNA isn't at its operating gain, the path is under-driven. board_type=0 (full internal
  gain) partially compensates → weak RX.
- **There is NO `halrf_config_rfe_8812a` / external-LNA-power function in this source.**
  `rfe_type=3` drives only `phy_SetRFEReg8812` (the 0xCB0 pinmux — present, matches
  mainline) and `phy_InitRssiTRSW` (software vars). So there's no missing GPIO-enable step.
- **RF 0x42/0xb0/0xb1 are dynamic** (TX-power-tracking / IQK-intermediate state, per the
  external agent) — symptoms, not config. The RADIO table doesn't even write 0x42/0xb0.
- **RF 0x00 is a mode/status register:** the RADIO table writes 0x00=0x00010000 but the
  read-back operating value is 0x33e69 (mine) vs 0x33da9 (mainline, receiving). It reflects
  the RF state machine, which on the DKMS port never enters "receive" — the core symptom.

**Conclusion / where to start next:** the BB+MAC+RFE config is byte-correct; the RF radio
**operating state** never enters receive mode (RF 0x00 status differs; forcing the RF regs
works only weakly). No register write I've found flips it cleanly. Open hypotheses, now
that config is excluded: (1) **RF PLL/LO not locked** despite RF[0x18]=ch1 — needs an LCK
(LC-tank) calibration the port skips; (2) a phydm/firmware RX-path commit (different FW
than rtw88) that puts the RF into RX. The decisive next experiment: compare the RF[0x18]
PLL-lock status bits and try porting `phy_LCCalibrate_8812A` / the RX-path calibration.

## M5 update — LCK ruled out; blocker is the RF-value combination (session 4)

**Correction to session 3: RF 0x18 bit16 (PLL-unlock) is NOT the blocker.** The
confirmed-working config (board_type=0 + forcing RF 0x00/0x30-32/0x42/0x65/0xb0/0xb1 to
the mainline values → 22 APs) **still reads RF[A,0x18]=0x17c01 (bit16=1)**. So a working
receiver runs with path-A bit16 set. The LC-calibration lead was followed to ground:
- Ported `phy_LCCalibrate_8812A` (RF_LCK=0xB4 BIT14 enter/leave; RF 0x18 BIT15 cal-begin;
  REG_TXPAUSE=0x522; cont-TX 0x914), with and without the standby-mode preamble.
- The cal-begin bit (RF 0x18 BIT15) **does not latch** (reads back 0 immediately), and
  bit16 never clears. The vendor's LCK call is itself commented out in hal_init, and the
  channel-tune auto-cal already locks path B (bit16=0) without it. So LCK is a dead end.

**The real, narrow blocker:** my RADIO-table + init produces an RF radio state that does
not receive. Forcing RF 0x00/0x30-32/0x65 (+ the "dynamic" 0x42/0xb0/0xb1) to mainline's
values is the ONLY thing that restores RX — and only when board_type=0 (internal-gain
RADIO branch). Confirmed dead ends:
- board_type=0xD8 (the EFUSE-correct external-LNA branch) makes RX *worse* — deaf even
  with all RF regs forced. The external branch lowers internal gain expecting an external
  LNA boost that isn't materialising.
- No subset works: {zero 0x30-32}+{0x00} and +{0x65} both fail; only the full forced set
  works. So it's a multi-register RF-state combination, not one register.
- The forced config is weak (−56 dBm vs mainline's −31 dBm) and uses hardcoded mainline
  values (not portable) — a diagnostic, not a shippable fix.

**Honest read:** the vendor RADIO_A/RADIO_B application (board=0 phy_cond branch) leaves
the RF in a degraded-but-functional state on path A; the EFUSE-correct external branch is
deaf. The clean fix needs either (a) the external-LNA operating-gain path engaged (no
register/function found that does it — `rfe_type=3` only drives the 0xCB0 pinmux), or
(b) a faithful full `rtw_phydm_init` (the many sub-inits skipped) that may set the RF
operating state correctly. This is the open problem; M1–M-TXPWR are solid and RX is
*proven achievable*, just not yet cleanly.

## M5 update — vendor pcap arrives; RX *receives garbage*, not deaf (session 5)

A real vendor cold-boot pcap now exists (`usb_dumps_new/captures_8812au/`, **morrownr
8812au**, monitor-enabled). Built reproducible RE tooling around it and ran a full
pcap-vs-port register diff on live HW. Two confirmed fixes landed; the core blocker is
**re-diagnosed** and several prior leads are killed.

**RE tooling (durable, in `scripts/rtl8812au_dkms/`):**
- `pcap_regtrace.py` — decode a capture's ordered vendor register writes (RF via BB
  0xC90/0xE90 SIPI) → `ref/morrownr_capture2_bringup.txt` (the oracle). capture-2/3 are
  COMPLETE; capture-1 is truncated, ignore it.
- `trace_bringup.py` — record our port's REAL emitted writes from one live bring-up,
  same format → `ref/ourport_bringup.txt`.
- `diff_trace.py` — final-value-per-register diff (RF decoded per path,addr).
- `rx_diag.py` — permissive-RCR (CRC/ICV-accept) RX classifier + raw bulk-IN byte stats.

**Oracle is trustworthy:** morrownr's `hal/rtl8812a/` ≈ Lucid-Duck's (the source this port
mirrors) — ~19-line diff, ZERO in the RF/PHY path; `phydm.c` differs by 6 lines. So the
pcap validates exactly our code path (A3/A4 resolved).

**FIX 1 (landed, verified): EFUSE board params were never threaded into the phy_cond walks.**
`rf.phy_rf_config`/`bb.phy_bb_config` defaulted to `JaguarParams()` (board_type=0, cut=0),
so the walker took the INTERNAL-gain branch. `efuse.read_chip_params` now decodes the
external-LNA/PA flags (0xBC/0xBD/0xBF → board_type **0xD8**) + cut (REG_SYS_CFG[15:12], A→15
remap → **1**); `build_jaguar_params` feeds them through. The RADIO/AGC gain rows
(RF 0x34/35/36/3C/61-65/86/8B both paths) now match the oracle byte-for-byte — **RF diff
23→1**. (`board_type=0xD8` "made it worse" in session-4 only because the walker bug left
the OTHER rows wrong; with correct threading it's right.)

**FIX 2 (landed, faithful): `_rf_serial_read` omitted the rCCAonSec CCA-off/on bracket.**
`phy_RFSerialRead` (rtl8812a_phycfg.c:105-133) toggles rCCAonSec_Jaguar (0x838 BIT3) OFF
before / ON after every RF read with `offset != 0`, gated `!(C_CUT || 8821)` — so it RUNS
on this B-cut 8812a. Our comment misattributed it to an `IS_TEST_CHIP` toggle in
`PHY_SetRFReg8812` (which has none). Added, gated per-transport (`t._rf_read_cca_off`, set
in `rf.phy_rf_config`) so the frozen 8821au is untouched. **Did NOT change RX** (and did not
clear RF[A]0x18 bit16 — that's PLL-relock status, see below), but it's a real port gap.

**THE BIG REFRAME — RX is not deaf; the demod produces garbage:** `rx_diag` with a
permissive RCR shows the HW **delivers ~13.8 KB across 10 bulk-IN reads in 12 s** — but
every sub-packet has `crc_err=1` and, past the 24B desc + 32B drvinfo, an **invalid
frame-control** (0xe9/0x92… bad protocol-version). So:
- NOT an RX-DMA / delivery / "OFDM-not-routed" problem (bytes flow).
- NOT a parser bug (`iter_frames` correctly skips crc_err garbage).
- The demod **triggers on energy but never locks onto real 802.11** → noise-garbage.
This matches session-1's "garbage demod," now proven to be delivered (prior sessions saw 0
frames because the monitor RCR drops crc_err).

**Ruled out this session:** RX-DMA/aggregation/RCR/inirp (bytes flow); host parser; board
gains (now match oracle); RF[A]0x18 bit16 as a cause (it's PLL-relock status — cosmetic in
the written value, and session-4 received with it set); the CCA-stale-read theory for bit16
(CCA fix applied, bit16 unchanged → it's real status, not a latch).

**Remaining oracle diff after both fixes (23 vals, 1 RF):** RF[A]0x18 bit16 (cosmetic);
TX-side 3-EP queue/page map (0x10C/0x114/0x200/0x209/0x280/0x420-45D — the known M2 fix-up);
path-B TXAGC left at table default 0x12121212 vs 0x1A1A1A1A (txpower only fully writes
path A — **M-TXPWR path-B gap**); monitor RCR (intentional accept-all); MAC-addr 0x610-615
(we omit, fine for promiscuous monitor).

**The open problem (unchanged core):** the demod produces garbage even though gains+RF match
the *receiving* vendor driver. Leading suspects, none yet tested:
1. **External-LNA operating power / TR-switch RX state** — the external-gain branch lowers
   internal gain expecting an LNA boost; if the LNA isn't powered in RX, the demod is
   under-driven and locks onto noise. The RFE *pinmux* (0xCB0) matches the oracle, but the
   RX-state GPIO/TR-switch drive may not — and the airmon **RX-START** segment (capture-2
   frames >12500) is **not yet decoded**; that's where a working driver actually begins RX.
2. **RX IQ calibration / clock** — uncalibrated I/Q → rotated constellation → garbage bits.
   (Vendor IQK is reportedly commented out, but verify against the pcap's start segment.)
3. **A demod path-enable that only shows at RX-start**, not in cold-boot init.

**Start next session here:** (a) `pcap_regtrace.py --max-frame 18000` and diff the
RX-START segment (12500-18000) against cold-boot to find what the vendor writes to *begin
receiving* (GPIO/RFE/TR-switch/IQK/RX-path); (b) classify whether the garbage MPDUs are
corrupted-real or pure-noise by dumping full MPDUs; (c) test forcing the external-LNA GPIO.

### RESOLVED — root cause is missing runtime IQK (capture-proven)

Both next-step checks ran and settle it:
- **RX-desc decode is byte-correct** — the vendor's OWN real RX buffers (capture-2 frames
  13035-13075) decode cleanly through our `rx.py` (valid beacons, real BSSIDs, crc_err=0),
  while our HW's buffers are genuine crc_err noise at the same offsets. It is a demod
  problem, full stop — NOT parser, NOT delivery, NOT gains (those match the oracle).
- **The vendor runs IQK; we don't.** `PHY_IQCalibrate_8812A` fires **4×** in the RX-START
  window (capture-2 frames **12535/13237/13311/13793**), NEVER in cold-boot (≤12499).
  Signature: RF 0x18 tone setup (0x07C01→0x07C0A) + IQK ctrl 0x08AC/0x08B0/0x08C4 + the FIR
  coefficient arrays **0x0C20-0x0C4C (path A) / 0x0E20-0x0E4C (path B)** rewritten 36-40×
  each in the IQK gradient. Init-time IQK is `/* */`-commented (usb_halinit.c:1668-1681) —
  the basis of the earlier "IQK not needed" note — but **runtime** IQK fires from phydm
  (`bNeedIQK`, after channel-set), which a cold-boot-faithful port never reaches.
- This also re-explains the "path-B TXAGC 0xE34-4C = 0x1A vs 0x12" oracle diff: those
  0xE20-0xE4C values are **IQK coefficient outputs**, not TX power.

Ported `PHY_IQCalibrate_8812A` (the live `halrf_8812a_ce.c` CE IQK) into `iqk.py` and ran it
after `chan.set_chnl_bw`.

**CORRECTION (HW-tested — IQK is NOT the core blocker):** with IQK enabled, RX is STILL
crc_err garbage (path-A IQC took a value, path-B fell to the 0x100 default). And the pcap is
decisive: the IQK one-shot (0x980=0xFA / 0xC60=0x77777777 ADDA-on) is **absent from all three
captures** — morrownr RECEIVES real beacons **without** running IQK. So the "capture-proven
IQK" claim above was WRONG: the Part-2 agent misread channel-tune frames (RF 0x18 channel
byte, 0x8AC `PostSetBwMode`, 0xC20-0x4C TXAGC sweep) as an IQK signature. `iqk.py` is a
faithful port and is kept (the chip may need it once base RX works), but it does not resolve
M5 alone. Verified rx-desc decode is byte-correct (the vendor's OWN capture-2 RX buffers,
frames 13035-13075, decode to valid beacons through our `rx.py`).

**The open problem (sharpened):** the demod produces garbage even though (a) gains match the
oracle, (b) RF matches except the cosmetic RF[A]0x18 bit16, AND (c) morrownr receives with
the SAME gains and NO IQK. So the RX difference is in the **~23 remaining cold-boot diffs**
(mostly TX-FIFO/3-EP: 0x10C/0x114/0x200/0x209/0x280/0x420-45D; a few BB: 0xA20 CCK / 0xC54 /
0xC68; path-B TXAGC 0xE34-4C; plus the omitted 0x0080/0x001D/0x0003/0x01CC/0x0520/0x0524/
0x0610-15) OR a **timing/sequencing gap my final-value diff can't see** — notably the
**RADIO-table udelay pseudo-addrs (0xfe..0xf9) we skip entirely** (`sipi.is_rf_delay_addr` ->
no write AND no delay), which could leave the RF un-settled. **Next decisive experiment:**
force our cold-boot register state to the oracle's values and re-test RX (does the static
config diff explain the garbage? if yes, bisect; if no, it's timing) — and add the RADIO
delays. RX delivery works (20 KB/12 s on bulk-IN); the descriptors are well-formed; only the
demodulated MPDU content is noise.

## Provenance

- Vendor source: `usb_dumps_new/captures_rtl8821au/driver-source/` (8812a in
  `hal/rtl8812a/` + `hal/phydm/rtl8812a/`).
- 8812 tables/FW: `scripts/rtl8812au_dkms/extract_tables.py` (golden-hashed).
- Live harness: `scripts/rtl8812au_dkms/test_hw.py` (`--phase open|efuse|fw|mac|phy|chan|txpower|beacon`).
- `[SRC]` = vendor C `file:line`.
