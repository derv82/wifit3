# RTL8814AU (8814au_dkms)

## Captured Wireless Card
- Dual-band 2.4 + 5 GHz — **4T4R silicon, 2T4R over USB-2**.
- Device: ALFA AWUS1900, `0bda:8813`, captured over USB 2.0.
- Captures: `find . -name captures_rtl8814au` (two dirs).

## Linux Driver Source
- Link: https://github.com/morrownr/8814au — **not** aircrack-ng/rtl8814au.
- Type: DKMS (PHYDM/ODM vendor tree).
- Version: commit `b1866ce2b857a8dfe2e147e19eb8eca0a842ce18` (v5.8.5.1, 2026-02-11).
- Location (vendored in-repo): `usb_dumps_new/captures_rtl8814au/driver-source/`.

## Python Port Details
- VID/PID: `0bda:8813`; the default driver for it (`WIFIT3_RTL8814=mainline` selects the rtw88 port).
- Status: **complete for this card, with [known problems](#known-problems)** — full bring-up, 2.4 + 5 GHz monitor RX + RSSI, and TX (deauth, handshake, PMKID, WEP replay + chopchop, WPS), all hardware-proven. `verify_pcap` reproduces all six cold-boot captures 100% byte-for-byte; `verify_pcap_selftest.py` confirms the gate FAILs on a mutation in each op-class. Runs on **any RTL8814AU card's EFUSE**, not only this ALFA's fuse burns — the rfe_type / rf_path branches are runtime-detected (see [EFUSE variants](#efuse-variants--any-card-support)); non-rfe-1 branches are ported-from-C but hardware-untested.
- Related port: mainline rtw88 @ `chips/rtw88_8814au/`.
- Non-obvious in the port (would cost a maintainer time):
  - Firmware uploads as beacon-queue TX packets on bulk EP `0x02` (3081 IDDMA), not an EP0 block-write.
  - RF registers are memory-mapped: write via the per-path LSSI reg (`0xc90`/`0xe90`/`0x1890`/`0x1a90`), read via `read32(base + addr*4)` (`0x2800`/`0x2c00`/`0x3800`/`0x3c00`); `0xfe`/`0xffe` are 50 ms settles.
  - TX-power-by-rate + regulatory-limit tables are compiled off; per-rate power collapses to `clamp(efuse_base + nTX_diff + 2, 0, 63)`.

## EFUSE variants — any-card support
The port runs on any RTL8814AU card's EFUSE, not only the captured ALFA (rfe_type=1, RF_2T4R
over USB 2). Two kinds of EFUSE data: **values** (crystal_cap, TX-power, bb-swing, MAC) are
consumed by computation, so any burn already works; **branches** are `rfe_type` (RFE pinmux +
spur/NBI) and `rf_path` (antenna option 0xC9 × USB link speed → `max_tx_cnt`). The BB/RF tables
(`bb_phy_reg_tbl`, `rf_radio_*_tbl`) carry every rfe/cut/package row inline and are resolved by
`phy_cond` — a different card walks ported-but-untested *rows*, not unported code.

Generalized branches (all keyed on the runtime EFUSE; the rfe=1 wire is byte-unchanged — see
`verify_pcap`):
- `chan._set_rfe_reg_2g` / `_set_rfe_reg_5g` / `set_rfe_reg_init` — `PHY_SetRFEReg8814A` rfe_type
  switch (cases 0/1/2; rfe∉{0,1,2} → no-op bInit + rfe-0 pinmux, exactly as the vendor).
- `chan._spur_nbi` — CSI notch gated to rfe∈{0,1,2}·ch153; NBI enable/disable gated to
  rfe∈{0,1,6,7} (a non-{0,1,6,7} card leaves NBI untouched, matching the vendor).
- `watchdog._hw_setting_nbi` — same rfe∈{0,1,6,7} gate.
- `efuse._rf_path_decision` / `_max_tx_cnt` — `rtl8814a_rfpath_decision`: antenna 0xC9 → rf_path,
  promoted to RF_3T3R (max_tx=3) only on a SuperSpeed link. Over USB 2 every card is RF_2T4R.

`connect()` logs the detected config once — `rfe_type / rf_path / antenna / max_tx / link /
crystal_cap / mac / bb_swing`, tagged `[untested variant]` when rfe_type≠1 — so an odd card is
diagnosable from one line.

### Untested variants (ported-from-C, hardware-unverified; residual gaps)
We only have the rfe=1 ALFA capture, so non-rfe-1 branches can't be pcap-gated — they are ported
1:1 from the vendor C and run "give-it-a-shot", not fail-loud. Known residual gaps:
- **rfe_type=0 ch140 8814AE MP-Rx AGC notch** (`phy_SpurCalibration_8814A` 0x82c/0x830 save/restore)
  — NOT ported (stateful, DFS-only, an AE-module tweak); an rfe-0 card on ch140 falls through to
  the benign NBI/CSI reset.
- **Amplifier-type / external-PA bb-swing fallback** (`hal_ReadAmplifierType_8814A` →
  `PHY_GetTxBBSwing_8814A`) — only reached on autoload-fail or a non-standard registry BB-swing;
  a normally-burned card decodes bb-swing from the efuse 2-bit index (the ported path).
- **TX-power-tracking typeN tables** (`powertrack_tbl.py` is the rfe∉{0,2,5,7,8} default set) —
  an rfe∈{0,2,5,7,8} card runs the runtime thermal TX-power track against the default deltas
  (TX-power only; RX/scan unaffected).
- **USB-3 SuperSpeed burst/FW path** (`_InitBurstPktLen`, link-speed not EFUSE) — a USB-3-attached
  card still uses the USB-2 burst/agg values (0x1e / 0x2005); `rf_path` is promoted to RF_3T3R but
  the 3rd TX stream only changes nss≥3 power, which fixed-nss=1 monitor inject never uses.
- txpwr-limit / phy-reg-pg typeN tables are compiled off in this morrownr build (by-rate/limit
  disabled) — N/A here.

## Known Problems
- **Card can wedge (radio silence) when it crosses 5 GHz → a 2.4 GHz channel and parks there.** Hopping hides it (a re-tune un-wedges); only a static dwell exposes it. Port-vs-silicon is unresolved — a fresh-replug kernel card didn't reproduce it, a cycled one did (Debug log). Graded low.
- 2.4 GHz RX under sustained hopping is jittery — one soak saw a 60 s dropout, a later one didn't.
- 20 MHz primary only (40/80 out of scope); the USB3 firmware/burst branch is unported (see [EFUSE variants → Untested variants](#untested-variants-ported-from-c-hardware-unverified-residual-gaps)).

## Driver Entry Points
Feature → where to start reading. Names match the vendor C (grep `usb_dumps_new/captures_rtl8814au/driver-source/`).
- Bring-up: `driver.connect` → `_bringup` (EFUSE → firmware → MAC/BB/RF → tune → DIG seed → turn-on → monitor); mirrors `rtl8814au_hal_init`.
- EFUSE / chip params: `efuse.read_chip_params` — `rfe_type`, `crystal_cap`, MAC, per-path TX-power, bb-swing, `antenna_option`/`rf_path`/`max_tx_cnt` (see [EFUSE variants](#efuse-variants--any-card-support)).
- Firmware upload: `firmware.bring_up` (3081 IDDMA bulk path).
- BB / RF init: `bb.phy_bb_config`, `rf.phy_rf_config` — flat-u32 tables walked by `phy_cond`.
- Channel tune / band switch: `chan.set_channel_bw`, `chan.switch_wireless_band_2g` / `_5g`.
- RX: `rx.iter_frames` + `rx.decode_rssi`; the `RxReaderThread` posts before `monitor.enter_monitor` opens the RCR.
- TX / inject: `tx.build_mgmt_txdesc` + `driver._inject`.
- Runtime watchdog: `watchdog.tick` — DIG (IGI ∈ [0x1c, 0x2a]), CCK-PD, EDCCA/CCX, thermal + IQK.

## Scripts
- **Gates:** `verify_pcap.py` (byte-diff the whole cold-boot capture), `verify_pcap_selftest.py` (mutate each op-class, assert the gate FAILs), `verify_efuse_pcap.py` (efuse read).
- **Live RX:** `scan_hw.py` (beacon/AP count, `--band`), `ab_scan.py` (replug A/B vs mainline), `rx_saturation_probe.py` (per-AP CCK/OFDM + `cck_pd_lv` trace).
- **Register diffs:** `cck_state_diff.py`, `rf_state_diff.py`, `dump_tune_regs.py` (live vs kernel).
- **TX (live, targeted, `--dry-run`):** `deauth_hw.py`, `wep_replay_hw.py`.
- **Vendor extraction:** `extract_fw.py`, `extract_bb_tables.py`, `extract_rf_tables.py`.
- **Smoke:** `test_hw.py`.
- **Wedge investigation (one-offs, deletable once the wedge is closed):** `rx_scan_wedge{,_linux}.py`, `rx_death_repro{,_linux}.py`, `rx_dwell_char.py`, `rx_pipe_probe.py`, `rx_wedge_{poke,regdiff,cure,settle}.py`, `hopdwell_watch.py`.

## Debug log

### 2026-07-10 — the 5→2 hop→dwell RX wedge (unresolved)
After a 5→2 band cross followed by a static dwell, RX can go dead (four RF paths read mode `0x00`=0,
`fa`=0). Ruled out: the RX reader (a `--pause-cross` A/B was 5/12 vs 4/12) and a settle race (10/20 ms
no help). Re-issuing `switch_wireless_band_2g` revives it but caps ~15% residual. Port-vs-silicon is
**contested**: a matched N-trial repro had our port 12/32 (~38%) and a *cycled* kernel card 9/32
(~28%), but a *fresh-replug* kernel card was 0/10 — consistent with the airmon-cycle degradation
confound, so the kernel rate isn't established.
Repro: `rx_scan_wedge{,_linux}.py`.
