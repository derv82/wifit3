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

## Provenance

- Vendor source: `usb_dumps_new/captures_rtl8821au/driver-source/` (8812a in
  `hal/rtl8812a/` + `hal/phydm/rtl8812a/`).
- 8812 tables/FW: `scripts/rtl8812au_dkms/extract_tables.py` (golden-hashed).
- Live harness: `scripts/rtl8812au_dkms/test_hw.py` (`--phase open|efuse|fw|mac|phy|chan|txpower|beacon`).
- `[SRC]` = vendor C `file:line`.
