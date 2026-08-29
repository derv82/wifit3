# RTL8821AU (8821au_dkms)

Cleanroom re-port of the RTL8821AU / RTL8811AU (ALFA AWUS036ACS, `0bda:0811`) from Lucid-Duck's [PR #194](https://github.com/morrownr/8821au-20210708/pull/194) to the
`morrownr/8821au-20210708` 5.12.5.2 vendor source; the DKMS out-of-tree `rtl88xxau` driver (Realtek
PHYDM/ODM stack), not mainline `rtw88`. The two are different codebases; flow comes from the vendor
tree cross-checked against the cold-boot pcap, never from the mainline-derived `chips/rtl8821au/`.

## Status

- Cold init, FW boot, MAC/BB/RF init, 2.4 + 5 GHz RX + tune: working on hardware.
- TX (deauth, fake-auth, WEP replay, ChopChop, WPS PIN/PBC, PMKID): user-confirmed live.
- Warm reattach and a 30-min dual-band 38-channel soak: clean (no degradation).
- `verify_pcap` / `verify_channels`: clean against the cold-boot pcap (all 36 hops byte-exact).
- 5 GHz deauth/TX (ch149): user-confirmed live.
- Runs on **any RTL8821AU/RTL8811AU EFUSE burn**, not just this ALFA's — the two fuse-derived
  branches (ext-LNA RFE pinmux, phy_cond board_type) are runtime-detected (see
  [EFUSE variants](#efuse-variants--any-card-support)); non-reference branches are ported-from-C,
  hardware-untested.

## Gotchas

**Sibling, not a replacement.** `chips/rtl8821au/` (mainline) stays. Both register for `0bda:0811`,
ordered by `$WIFIT3_RTL8821` read fresh per run (flips between runs without a restart). This DKMS
port is the blank default; `=mainline` (case-insensitive) falls back to the mainline driver. The
reason for the port: mainline inherits `rtw88`'s weaker 2.4 GHz monitor RX (AGC/DIG), and the vendor
PHYDM DIG path is the suspected fix — though for 8821au itself the A/B came out a tie/stability play;
the headline payoff is the deferred 8812au sibling that this port keeps cheap.

**The OFDM RSSI `>>1` is mandatory.** This is Jaguar-2: `pwdb_all` is the sum of both DC paths,
halved before the dBm conversion (`((pwdb_all>>1)&0x7F)−110`). Dropping it reads ~2× too strong and
saturates 5 GHz OFDM beacons to ~0 dBm; 2.4 GHz hides it because beacons there are CCK.

**bb_swing is per-band from EFUSE** (0xC6 2G / 0xC7 5G): this card reads 0 dB (0x200) on 2.4 GHz but
−3 dB (0x16A) on 5 GHz. It must come from the fuse, not a constant — it was the only ch36 divergence
before being threaded through.

**TX power collapses to the PG base.** The Lucid-Duck Makefile sets `CONFIG_TXPWR_BY_RATE_EN=0` /
`CONFIG_TXPWR_LIMIT_EN=0`, so the index reduces to `base[rate-section][ch-group] + diff[1TX]` clamped
[0,63] — no by-rate, no limit, no init amends; the JAGUAR odd-index workaround does not fire on a
normal chip like the AWUS036ACS.

**Start the RX reader before RX-enable.** The kernel posts URBs at probe (before the monitor RCR
write), and this chip has RX-starvation history — `RxReaderThread` must start before `enter_monitor`.

**The EDCCA PSD-search loop is live-only**, not byte-replayable: it reads live PSD and steps
thresholds, so the port reproduces the `phydm_search_pwdb_lower_bound` algorithm rather than the wire
values. The replay-diff gate skips this window deliberately.

**Scope:** 20 MHz primary only (no 40/80). SW-seq fragmentation is not reintroduced (hwseq only).
`# TODO(8812au):` breadcrumbs mark every point the vendor source branches on chip (RF path 1×1 vs
2×2, RFE option, pwr-seq table, FW blob, per-rate txpower) so the 8812au decision stays cheap.

## EFUSE variants — any-card support

Two kinds of fuse data. **Values** (crystal_cap, per-rate TX-power, bb_swing, MAC) are consumed
by computation, so any burn already works. **Branches** select code paths — these are where a
non-reference card diverges, and they are now gated on the runtime fuse (`efuse.read_chip_params`
→ `driver`), so a value-less path stays the reference AWUS036ACS byte-for-byte (`verify_pcap`
proves it). The card reads `ext_lna_2g=0`, `board_type=0x00` (blank amplifier bytes 0xBC/0xBD/0xBF).

**Runtime-gated branches (reference = default; non-reference ported-from-C, hardware-untested):**
- **ExternalLNA_2G RFE pinmux** — `chan._set_rfe_2g` ports both arms of `phy_SetRFEReg8821`
  (2.4 GHz): bypass (`ext_lna_2g=0`, pinmux b'111, `0xCB4=0x10000077`) vs external-LNA-on
  (`ext_lna_2g=1`, pinmux b'010, `0xCB4=0x10100077`). Keyed on `LNAType_2G[3]` (efuse 0xBD). The
  5 GHz RFE (`phy_SetRFEReg8821` else-branch) has no external-LNA arm → `_switch_band_5g` is
  card-independent.
- **phy_cond `board_type`** — the 8821a BB/AGC/RADIO tables carry board-gated condition rows
  (e.g. AGC `0x8000020c` = interface-USB + ALNA|APA). `efuse.build_jaguar_params` threads the ODM
  board_type (from the four external PA/LNA flags of `Hal_ReadPAType_8821A`) into
  `bb.phy_bb_config` / `rf.phy_rf_config`. An external-PA/LNA card then walks its own
  (ported-but-untested) rows instead of the reference's.

**Deliberately NOT threaded (not a real-world variant on this build), with the vendor reason:**
- **cut_version / package** — every 8821a table condition header has cut[27:24] = QFN[15:12] = 0,
  so no row is silicon-cut- or package-gated. `cut_version` stays 0 (no effect on any taken row).
- **phy_FixSpur_8812A** — a no-op on 8821 (it is `IS_VENDOR_8812A_C_CUT || IS_HARDWARE_TYPE_8812`
  only), so `chan` correctly omits it for all cards; the RF-read CCA-on-secondary toggle is likewise
  always skipped on 8821 (`IS_HARDWARE_TYPE_8821`), so `rf._rf_serial_read` omits it.
- **rf_type** — the 8821a is 1T1R; path-B is written unconditionally only where the vendor does.
- **AGC-table-select MP-chip branch** — `IS_VENDOR_8821A_MP_CHIP` is TRUE for every normal (retail)
  chip, so `chan` hardcodes the `0xC1C[11:8]` arm; only test/non-normal silicon takes the
  `0x82C[1:0]` arm (not a shipped card).
- **antenna-diversity DPDT gate** — `phydm_set_ext_band_switch_8821A` is vendor-gated on
  `drv_ant_band_switch && AntDivCfg==0`, but `CONFIG_ANTENNA_DIVERSITY` is compiled out
  (autoconf.h:97), so `AntDivCfg` is always 0 and the DPDT always fires; `chan._ext_band_switch`
  runs it unconditionally to match. (This card's `rf_board_opt` BIT3=1, so an antdiv-enabled build
  WOULD gate it — a latent build-time dependency, not a runtime fuse gap.)

**Residual not ported:** the BT `board_type` bit (`ODM_BOARD_BT`) — needs the
`REG_MULTI_FUNC_CTRL` `BT_FUNC_EN` probe + the BTCoexist stack; a BT-combo module is out of scope
(same call as the 8812/8814 siblings). `type_glna/gpa/alna/apa` stay 0 (`Hal_ReadPAType_8821A`
never sets them — those are 8812-only), so a board_type-set card matches only the type-0 rows.

## Orientation

Start at `driver.connect` — it runs power-on → FW → MAC → BB/RF → tune → RX in the kernel's order.
RX-desc decode + the 8821a RSSI tables are in `rx.py`; monitor RCR (0x9000382F) in `monitor.py`; the
PHYDM DIG init + watchdog in `dig.py`. Channel/band hop is `chan.set_channel_bw` (`phy_SwBand`
switches band only on a 2.4↔5 crossing). EFUSE decode is `efuse.read_chip_params`; per-rate TXAGC in
`txpower.set_tx_power` (+ `_5g`). One descriptor builder, `tx.build_mgmt_txdesc`, serves all
injection (40-byte fake TX desc + XOR-16 checksum, bulk-OUT ep 0x09). Bulk-IN is ep 0x84.

Names match the vendor C, so grep the bundle's `driver-source/` to cross-reference.

## Scripts

- `verify_pcap.py` — the cold-boot byte gate.
- `verify_channels.py` — byte-diffs every `iw set channel` window (all 36 hops) against the runtime tune.
- `verify_efuse_pcap.py` — replay-diffs the EFUSE read block.
## Debug log

### 2026-06-04 — A/B vs mainline; the DIG-health canary

The A/B against mainline `chips/rtl8821au/` (fixed channel, equal dwell, replug between runs) tracks
breadth (nAPs, beacons/s) plus a fixed canary AP: a strong nearby router whose beacon rate sags first
when initial gain (DIG/IGI) is mistuned, so it reads as a DIG-health indicator. Decision rule
inherited from the 8814au A/B: breadth and canary rate can trade off — don't fail the port on a small
canary-beacon delta if nAPs rises and the canary's RSSI/rate is no longer anomalously low. On
2026-06-04 (ch1/30s, DIG on) the DKMS port tied the mainline baseline as predicted for 8821au;
the headline payoff is the deferred 8812au sibling. M9 flipped DKMS to default after the fixed-channel
matrix showed it ties 5 GHz, edges 2.4 GHz (canary ~11 dB stronger), breadth ≥ mainline both bands.

### 2026-06-05 — endurance soak

30-min dual-band 38-channel hop at 0.25 s showed no degradation: active BSSIDs 105→100, both bands'
rates flat the whole run, frames steady ~1.4–1.8 k/60 s. The diag WARNs are benign — the OUI
"garbage" is broadcast/wildcard BSSIDs (`ff:ff:ff:ff:ff:ff`) and the beacon-channel mismatch is
fast-hop adjacent-channel bleed.

### 2026-07-11 — generalize across EFUSE burns (any 8821au/8811au card)

Swept the vendor source for every 8821-relevant fuse-derived branch and closed the two that
hardcoded the reference discriminator (see [EFUSE variants](#efuse-variants--any-card-support)):
`ext_lna_2g` (RFE pinmux, `phy_SetRFEReg8821`) and `board_type` (the phy_cond BB/AGC/RF walker
inputs). Both default to the reference values, so `verify_pcap` (2532 ops through M4+TXpwr, +44
through M5) and `verify_channels` (36/36 hops) stay byte-exact through the generalized code. The
other candidate branches (cut/package, phy_FixSpur, RF-read CCA, MP-chip AGC select, antenna-div
DPDT) are card-independent on this build (documented with the vendor reason). Added `test_efuse.py`
+ `test_chan.py` for the variant (non-reference) values. BT board_type bit + `type_*` remain
unported residuals (BT-combo module out of scope).
