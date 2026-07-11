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
- Status: **complete for this card, with [known problems](#known-problems)** — full bring-up, 2.4 + 5 GHz monitor RX + RSSI, and TX (deauth, handshake, PMKID, WEP replay + chopchop, WPS), all hardware-proven. `verify_pcap` reproduces all six cold-boot captures 100% byte-for-byte; `verify_pcap_selftest.py` confirms the gate FAILs on a mutation in each op-class.
- Related port: mainline rtw88 @ `chips/rtw88_8814au/`.
- Non-obvious in the port (would cost a maintainer time):
  - Firmware uploads as beacon-queue TX packets on bulk EP `0x02` (3081 IDDMA), not an EP0 block-write.
  - RF registers are memory-mapped: write via the per-path LSSI reg (`0xc90`/`0xe90`/`0x1890`/`0x1a90`), read via `read32(base + addr*4)` (`0x2800`/`0x2c00`/`0x3800`/`0x3c00`); `0xfe`/`0xffe` are 50 ms settles.
  - TX-power-by-rate + regulatory-limit tables are compiled off; per-rate power collapses to `clamp(efuse_base + nTX_diff + 2, 0, 63)`.

## Known Problems
- **Card can wedge (radio silence) when it crosses 5 GHz → a 2.4 GHz channel and parks there.** Hopping hides it (a re-tune un-wedges); only a static dwell exposes it. Port-vs-silicon is unresolved — a fresh-replug kernel card didn't reproduce it, a cycled one did (Debug log). Graded low.
- 2.4 GHz RX under sustained hopping is jittery — one soak saw a 60 s dropout, a later one didn't.
- 20 MHz primary only (40/80 out of scope); the USB3 firmware/burst branch is unported.

## Driver Entry Points
Feature → where to start reading. Names match the vendor C (grep `usb_dumps_new/captures_rtl8814au/driver-source/`).
- Bring-up: `driver.connect` → `_bringup` (EFUSE → firmware → MAC/BB/RF → tune → DIG seed → turn-on → monitor); mirrors `rtl8814au_hal_init`.
- EFUSE / chip params: `efuse.read_chip_params` — `rfe_type`, `crystal_cap`, MAC, per-path TX-power, bb-swing.
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
