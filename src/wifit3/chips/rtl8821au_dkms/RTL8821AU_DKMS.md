# RTL8821AU — vendor/DKMS port ground truth

Cleanroom re-port of the **RTL8821AU / RTL8811AU** (ALFA AWUS036ACS, `0bda:0811`)
from the **Lucid-Duck `8821au-20210708` 5.12.5.2** vendor source — the
DKMS-distributed out-of-tree `rtl88xxau` driver (Realtek PHYDM/ODM stack), **not**
mainline `rtw88`. The two are different codebases; addresses/macros/flow come from
the vendor tree (`usb_dumps_new/captures_rtl8821au/driver-source/`) cross-checked
against the cold-boot pcap, never from the mainline-derived `chips/rtl8821au/`.

- **Sibling, not a replacement.** `chips/rtl8821au/` (mainline) stays. Both
  register for `0bda:0811`, ordered by **`$WIFIT3_RTL8821`**, read fresh per run
  (flips between runs without a restart). **This DKMS port is the blank default**
  (flipped in M9 after the A/B showed it ties 5 GHz / edges 2.4 GHz with correct
  RSSI); `=mainline` (case-insensitive) falls back to the mainline driver.
- **Why this port:** the mainline-derived port inherits `rtw88`'s weaker 2.4 GHz
  monitor RX (AGC/DIG). The vendor PHYDM DIG path is the suspected fix (proven
  3.6× on 8822bu; 8821au's own breadth is a tie/stability play — the headline
  payoff is the 8812au sibling, deferred until after this port's A/B).
- **Scope:** 20 MHz primary only (no 40/80). SW-seq fragmentation is **not**
  reintroduced (hwseq only). `# TODO(8812au):` breadcrumbs mark every point the
  vendor source branches on chip (RF path 1×1 vs 2×2, RFE option, pwr-seq table,
  FW blob, per-rate txpower) so the 8812au decision is cheap later.

## Potential known gaps (audit as the port lands)

- [x] RX poll → shared `RxReaderThread` (`driver.py`), started **before** RX-enable
  (the kernel posts URBs at probe).
- [x] FCS strip before the RX callback — `rx.py` strips the HW-appended FCS
  (`frame_end == MPDU_end`).
- [x] Always-monitor RCR / RX_FILTR_CFG — `mac.py` + `monitor.py` (vendor inits are
  STA; wifit3 is always-monitor).
- [x] DIG/IGI watchdog tick — `dig.py` (without it, gain freezes at the AGC default).
- [x] 5 GHz: RX + tune + TX ported — `chan.py` (`_switch_band_5g`) + `txpower.py`.

## A/B methodology (vs mainline `chips/rtl8821au/`)

Flip with `$env:WIFIT3_RTL8821='mainline'` vs blank/`dkms`. Fixed channel, equal
dwell, replug between runs.

**Metrics tracked every run:**
- total distinct APs (**nAPs**)
- **total** beacons/s and **peak-AP** beacons/s
- **Canary AP — `NETGEAR2G`, BSSID `aa:bb:cc:dd:ee:01`:** its **RSSI (dBm)** and
  **beacons/s**. NETGEAR2G is a strong, nearby router (strongest in range; ~9–10
  beacons/s when healthy) and is the **DIG-health indicator** — a strong AP whose
  beacon rate sags first when the initial gain (DIG/IGI) is mistuned.

> PII: this BSSID is recorded deliberately as the fixed A/B canary. It is on the
> planned git-history PII-scrub list — do not treat it as an exception to the
> "no real BSSIDs in commits" rule for any *other* network.

**Decision rule (inherited from the 8814au A/B):** breadth and canary rate can
trade off. The 8814au DKMS port won on a run showing roughly **−10 canary beacons
but +10 nAPs** vs mainline — broader reach plus the DIG fix that recovered the
canary's *relative* rate settled it. Don't fail this port on a small canary-beacon
delta if nAPs rises and the canary's RSSI/rate is no longer anomalously low vs its
neighbours.

**Mainline baseline — `chips/rtl8821au/`, 2026-06-04, ch1, 30 s:** 22 APs, 2727
beacons; canary `NETGEAR2G` ≈ 230 beacons (**~7.7/s**) — already *below* its ~9–10/s
healthy rate, consistent with the mainline DIG softness this port targets.

## Milestone status

| M | Scope | Offline (replay-diff) | Live HW | Status |
|---|---|---|---|---|
| M0 | Branch + baseline + `scripts/rtw88_pcap_replay.py` + scaffold | — | — | **done** |
| M1 | Power-on → FW download → FW-ready (+ warm reset) | **PASS** (1627 ops byte-exact, incl. 30848 B FW page-write) | **PASS** cold (SYS_CFG=0x04412135) + warm re-entry, WINTINI_RDY | **done** |
| M2 | MAC init (REG_CR → MACTXEN\|MACRXEN) | **PASS** (182 ops byte-exact, 98-entry MAC table) | **PASS** (REG_CR=0xFF) | **done** |
| M3 | BB/PHY + RF init (PHY_REG/AGC_TAB/RadioA, 1×1) | **PASS** (586 ops byte-exact, JaguarSeries phy_cond walker) | **PASS** (xtal=0x9e7) | **done** |
| M4 | 2 GHz channel tune, 20 MHz (RF-SIPI) | **PASS** (74 ops byte-exact, incl. 8811au ant-prologue) | **PASS** (`--phase chan`, RF[0x18] ch1@20M) | **done** |
| M-TXPWR | EFUSE read + 2 GHz per-rate TX power | **PASS** (efuse 1191 ops + txagc 62 ops byte-exact, contiguous M4→M5) | **PASS** (live EFUSE decode matches pcap: crystal_cap=0x27, cck_base[0]=0x31; RX healthy) | **done** |
| M5 | 2 GHz RX + PHYDM RSSI/DIG (value milestone) | **PASS** (44 ops byte-exact §1+§2; 474 live EDCCA ops skipped; §3 monitor 10-op block) | **PASS** (`--phase beacon` ch1/30s: 18 APs, 1754 beacons; canary NETGEAR2G 7.3/s @ −48 dBm; DIG watchdog ticks, FA resets) | **done** |
| M6 | 2 GHz TX (deauth + WEP replay) | **PASS** (unit test — no TX in the cold-boot pcap; fake-txdesc fields + XOR-16 checksum + golden bytes) | **PASS** (user-run: deauth → 37 EAPOL handshakes from the reconnecting client, no pipe fault; WEP replay → 5518 IVs, replay winner locked; ChopChop + WPS PIN/PBC + PMKID extract HW-confirmed 2026-06-05, stock engine over the same inject path) | **done** |
| M7 | 5 GHz: RX + tune + TX | **PASS** (`verify_channels`: all 36 hops byte-exact — 2.4 GHz 2-12 + 5 GHz 36-165, band switch + channel + per-band txagc) | **PASS RX** (`--phase beacon --channel 36`: 5 APs, 388 beacons; bb_swing 5g=0x16a); **user** for 5 GHz deauth | **done** (RX); TX user-verify |
| M8 | Driver Protocol wiring + warm reattach + manager `WIFIT3_RTL8821` | **PASS** (manager tests: env-var ordering + 0bda:0811 claim; Protocol conformant) | **PASS** (live discovery picks the driver per env var; warm re-entry re-inits cleanly — ch1 18 APs, canary 8.3/s) | **done** |
| M9 | A/B matrix + flip default to DKMS | — | **PASS** (fixed-channel A/B: 5 GHz ch149 DKMS 9.5/s ≈ mainline 9.7/s @ −53 dBm; 2.4 GHz ch1 canary ~11 dB stronger; breadth ≥ mainline both bands) | **done** (DKMS is the default; `=mainline` opt-in) |
| M10 | Endurance / stress soak | — | **PASS** (30-min dual-band 38-ch hop @ 0.25 s, no degradation: active BSSIDs 105→100, ratio 0.95; 2.4 GHz ~62–80 and 5 GHz ~29–37 both flat the whole run; frames steady ~1.4–1.8 k/60 s. Benign diag WARNs: 6.96% OUI "garbage" = broadcast/wildcard BSSIDs (all `ff:ff:ff:ff:ff:ff`); 36.1% beacon-ch mismatch = fast-hop adjacent-channel bleed. Report `scripts/diag/reports/rtl8821audkms_20260605-191552.md`) | **done** |

## M5 (RX) implementation spec — wire-verified, IMPLEMENTED

Distilled from the M5 cold-boot mapping (capture-1, dev 39). **All of M5 is now
implemented and verified** (replay-diff byte-exact + live beacon count): `rx.py`
(RX-desc + 8821a RSSI), `monitor.py` (RCR 0x9000382F), `dig.py` (InitHalDm §2 +
the live EDCCA PSD search + the DIG watchdog), `mac.hal_init_misc_pre/post` (§1
post-tune tail), `driver.py` (`connect()` M1→M5 + RX reader started before monitor
+ DIG watchdog). The spec below records the wire mapping the port was built from;
all register values are wire-confirmed unless flagged. Mirrors the `rtl8814au_dkms`
sibling.

**Live A/B result (2026-06-04, ch1/30s, DIG on):** 18 APs, 1754 beacons (58/s);
canary NETGEAR2G 220 beacons (**7.3/s**, −48 dBm) — ties the mainline baseline (22 APs,
NETGEAR2G 7.7/s), as predicted for the 8821au (own breadth tied; the headline payoff is
the deferred 8812au sibling). The full A/B matrix is M9.

**Wire boundaries:** M5 post-tune tail begins **frame 7609**; InitHalDm runs
7617–8643, **except the EDCCA PSD-search loop 7693–8563 which reads live PSD and is
NOT byte-replayable**; airmon's STA→monitor dance 8647–8883 is **skipped** (always
monitor); the monitor opmode block is **8893–8911** (verify out-of-line).

**§1 Post-tune hal_init tail** [SRC] usb_halinit.c after :1595, in order:

| Reg | Addr | W | Val | note |
|---|---|---|---|---|
| invalidate_cam_all (CAMCMD) | 0x0670 | 4 | 0xC0000000 | poll+clear loop — read source for exact seq |
| REG_HWSEQ_CTRL | 0x0423 | 1 | 0xFF | |
| REG_BAR_MODE_CTRL | 0x04CC | 4 | 0x0201FFFF | |
| Nav-limit | 0x0652 | 1 | 0x00 | |
| *(InitHalDm — §2)* | | | | |
| REG_QUEUE_CTRL | 0x04C6 | 1 | RMW `& 0xF7` (→0x04) | |
| REG_FWHW_TXQ_CTRL+1 | 0x0421 | 1 | 0x0F | Tx-report en |
| REG_EARLY_MODE_CONTROL+3 | 0x02BF | 1 | 0x01 | |
| REG_TX_RPT_TIME | 0x04F0 | 2 | 0x3DF0 | |
| REG_SDIO_CTRL_8812 | 0x0070 | 1 | 0x00 | |
| REG_ACLK_MON | 0x003E | 1 | 0x00 | |
| REG_USB_HRPWM | 0xFE58 | 1 | 0x00 | |

No-ops (gated, do not emit): 0x460 FAST_EDCA (wifi_spec), IQK/PWtrack/LCK (commented), FWHW_TXQ BIT12 (CONFIG_XMIT_ACK).

**§2 InitHalDm** [SRC] rtl8812a_dm.c:213 → phydm.c:1786 odm_dm_init, wire order:
- `dm_InitGPIOSetting`: RMW 0x0040 clear GPIOSEL_ENBT → 0x40=0x00.
- `phydm_dig_init`: read 0xC50 mask 0x7F (AGC default IGI=0x20); **no write**; seeds NHM + watchdog.
- `phydm_cck_pd_init`: write 0xA0A=0x83 (8821a value — reproduce from wire).
- `phydm_env_monitor_init` (NHM): toggle 0x994 BIT8; 0x998=0x302C2824, 0x99C=0x403C3834, 0x9A0 byte0=0x44, 0x994[31:16]=0x484C, 0x990=0xFFFF1027. th[i]=((IGI−14)<<1)+4·i. **Identical to `rtl8814au_dkms/dig.py:_env_monitor_init`.**
- AGC gain commit (path A via BB): 0x8B0=0xEF000000; 0xC90 rows 0x0000F80E/0x00800103/0x2F001003/0x9BB02F03; close 0x8B0/0xC90=0x0000F00E.
- **`phydm_adaptivity_init` + EDCCA PSD search — 8821a-ONLY, LIVE-ONLY (not replayable):** 0x8A4 bytes0/1 = E97F7F7F/E9E27F7F; loop {write 0x8FC=0x09020000, read 0xFA0 (PSD), read+write 0x8F8=0xC0020040, step 0x8A4 thresholds up}; ends 0x520 BIT15, 0x524 BIT11. Port the `phydm_search_pwdb_lower_bound` **algorithm** (adaptivity.c:237,666; th_edcca_hl_diff=7); DO NOT hardcode wire values.
- RX-gain commit: 0x910 ×5 = 00FC0000,00EC0000,002C0000,002C0000,002C0000 (8814 dig.py:_rf_gain_table tail shape).

**§3 Monitor block** [SRC] rtl8812a_hal_init.c:3663,3710 — **DONE in monitor.py.** RCR=0x9000382F (clears ACRC32|AICV vs 8814's 3B2F). 10 ops: Set_MSR(0x102 RMW→NOLINK), read+write RCR 0x608, read 0x6A0/6A2/6A4, write =0xFFFF. Verify out-of-line anchored on 0x608=0x9000382F.

**§4 RX desc + RSSI** — **DONE in rx.py.** 24-B desc bit-layout = 8814's. 8821a RSSI: CCK
table {5:−38,4:−30,2:−17,1:−1,0:15}−2·vga (byte 5); OFDM ((pwdb_all>>1)&0x7F)−110 (byte 4).
FCS stripped. **The OFDM `>>1` is mandatory** [SRC phydm_phystatus.c:933] — pwdb_all is the
sum of both DC paths, halved before the dBm conversion. (An earlier version dropped it,
claiming "no >>1, that's the 8814 path" — a wrong skip-rationale: it read ~2x too strong and
saturated 5 GHz OFDM beacons to ~0 dBm. 2.4 GHz hid it because beacons there are CCK.)

**§5 Bulk-IN + reader:** bulk-IN ep **0x84**, bulk-OUT 0x09. RX DMA already on from M2 (REG_CR=0x063F); the monitor RCR opens the gate. **Start `RxReaderThread` (chips/rx_reader.py; read_once=bulk-IN 0x84, dispatch=rx.iter_frames) BEFORE `enter_monitor`** — kernel posts URBs (8885) before the RCR write (8899), and this chip has RX-starvation history (see rx_reader.py docstring).

**§6 DIG watchdog (runtime, ~2 s)** [SRC] phydm_dig.c:1336 — same algo as 8814 dig.py:watchdog_tick, **single-path**: read IGI 0xC50[6:0]; FA = OFDM 0xF48[15:0] + (CCK 0xA5C[15:0] if 0x808 BIT28); reset 3-pulse 0x9A4 BIT17(1→0)/0xA2C BIT15(0→1)/0xB58 BIT0(1→0); new IGI no-link step {+2,+1,−2} @ FA {2000,4000,5000}; clamp [0x1C,0x2A]; write **only 0xC50**[6:0].

**Verification:** replay-diff §1 + §2(minus EDCCA) contiguous from frame 7609; §3 out-of-line; EDCCA + RX-decode are **live-only → beacon count ch1/6/11 vs the 22-AP / NETGEAR2G baseline**. Commit M5 once the beacon count works.

## M6 (TX) — deauth + WEP replay

One descriptor builder serves all injection. `tx.build_mgmt_txdesc` ports
`rtl8812a_fill_fake_txdesc` [SRC] rtl8812a_xmit.c:265 — the 40-byte fake TX
descriptor (FIRST/LAST_SEG, OFFSET, PKT_SIZE, QUEUE_SEL=QSLT_MGNT, RATE_ID, OWN,
HWSEQ_EN, USE_RATE, BMC, SEC_TYPE=0, TX_RATE) + the XOR-16 checksum
(`rtl8812a_cal_txdesc_chksum`, over the first 32 B). Field bit positions are
**identical to the 8814au_dkms sibling**; the 8812a additionally sets FIRST_SEG and
OWN (ported). `driver.inject_frame` prepends it and sends `[desc | frame]` on
bulk-OUT ep 0x09, serialized via `_io_lock`.

- **Deauth, fake-auth, WEP ARP replay all ride this one path.** The replayed ARP is
  already WEP-encrypted, so SEC_TYPE=0 (inject raw, no HW re-encryption); the vendor's
  `bDataFrame` SEC_TYPE branch never applies. WEP runs through the stock
  device-agnostic `WepCampaign`/`WlanInterface` — no port-specific attack code.
- **No replay-diff:** the cold-boot pcap is passive monitor RX, so there are no TX
  frames to diff. Verified instead by `tests/chips/rtl8821au_dkms/test_tx.py` (field
  positions + checksum + golden bytes); live TX is the user's job
  (`scripts/rtl8821au_dkms/{deauth_hw,wep_replay_hw}.py`).
- **TX power** is now the EFUSE-calibrated per-rate level (see M-TXPWR), applied at
  connect and re-applied per channel in `set_channel`.

## M-TXPWR (EFUSE + per-rate TX power) — 2.4 GHz

`efuse.read_chip_params` runs the probe-phase EFUSE read (ReadEFuseByte byte loop ->
PG-block -> 512 B logical map) and decodes crystal_cap (0xB9 → replaces the M3
hardcode), the MAC (0x107), and the path-A PG TX-power block (pg_txpwr_saddr=0x10:
6 CCK + 5 BW40 group bases + nTX diff nibbles). `txpower.set_tx_power` then writes the
direct 8812a TXAGC registers (0xC20..0xC44, path A / 1SS) + the 0xC54 training word.

- **Power formula collapses to the PG base.** The Lucid-Duck Makefile sets
  `CONFIG_TXPWR_BY_RATE_EN=0` / `CONFIG_TXPWR_LIMIT_EN=0`, so `hal_com_get_txpwr_idx`
  reduces to `idx = base[rate-section][ch-group] + diff[1TX]`, clamped to [0, 63] (no
  by-rate, no limit, no amends at init; the AWUS036ACS is a normal chip, so the JAGUAR
  odd-index workaround does not fire). For ch1 this reproduces the wire exactly:
  CCK 0x31, OFDM 0x2d, HT/VHT 0x2b, training 0x131921 (MCS7 −10/−8/−6).
- **Replay-diffable, contiguous.** The EFUSE read (frames 65–2475) is verified by
  `verify_efuse_pcap.py`; the txagc sweep (frames 7485–7607) now replays contiguously
  between M4 (ends 7483) and M5 (starts 7609), closing the gap the M5 differ skipped.
- **5 GHz TX power is M7** (the PG block's 5 GHz half + UNII groups are not yet decoded).

## M7 (5 GHz) — RX + tune + TX power

`chan.set_channel_bw` is the runtime hop: `phy_SwBand` reads the band marker
(REG_CCK_CHECK 0x454 BIT7) and switches band (`PHY_SwitchWirelessBand8812`) only on a
2.4<->5 crossing, then selects the channel (fc_area 0x860 + RF_MOD_AG RF-0x18 band bits
+ channel byte) at 20 MHz. `set_chnl_bw` (connect/M4) keeps the unconditional 2.4 GHz
band switch (mirroring usb_halinit). The 5 GHz band switch ports the 8821a `BAND_ON_5G`
path: ext-band-switch (DPDT band=2b'10), RFE PA/LNA on (0xCB0[15:12]=5,[7:4]=4), CCK_CHECK
BIT7 set, TX-FIFO-idle wait, OFDMCCKEN, AGC-table 0xC1C[11:8]=1, rTxPath/rCCK_RX, 11A
basic rates, bb_swing.

- **bb_swing is per-band from EFUSE** (0xC6 2G / 0xC7 5G): this card reads 0 dB (0x200)
  on 2.4 GHz but **−3 dB (0x16A)** on 5 GHz — the one value that has to come from the
  fuse, not a constant (it was the only ch36 divergence before being threaded through).
- **TX power**: `txpower.set_tx_power_5g` writes the same direct TXAGC registers minus
  CCK, using the EFUSE 5 GHz PG block (14 UNII group bases + OFDM/BW20 diffs) and the
  `_ch_group_5g` UNII mapping.
- **Replay-diffed exhaustively**: `verify_channels.py` slices every `iw set channel`
  window from the cold-boot hop log and byte-diffs the runtime tune — all 36 hops PASS
  (2.4 GHz 2-12, 5 GHz 36-165), including the 2.4->5 crossing's band switch.
- **Live**: 5 GHz RX confirmed (ch36, 5 APs); 5 GHz deauth/TX is the user's verify.
  # TODO: ch153 spur notch (minor RX polish), 40/80 MHz width.

## Live HW access

Milestones are developed + verified **offline** against the cold-boot pcap via the
replay-diff gate (no device needed). Live smoke tests (`test_hw.py`) are a
secondary confirmation and require the ACS to be free: if a `wifit3` app instance
(or another session) holds it, `claim_interface` returns "Access denied" *before*
any bring-up runs — that is the holder, not a wedge and not a port defect. A true
USB wedge (handle open denied even with no holder) needs a one-time replug, which
only the user can do. Warm re-runs from a prior bring-up are fine: `card_enable_flow`
begins with CARDDIS→CARDEMU and re-inits a partially-powered chip.

## Provenance

- Vendor source: `usb_dumps_new/captures_rtl8821au/driver-source/` (Lucid-Duck
  `8821au-20210708` 5.12.5.2, branch `kernel-6.18-compat`).
- Cold-boot captures: `usb_dumps_new/captures_rtl8821au/capture-{1,2,3}.pcap`
  (+ `_logs/main.log` for `pcap_slicer.py`, `iw.log` for per-channel windows).
- Acceptance gate: `scripts/rtl8821au_dkms/verify_pcap.py` +
  `verify_channels.py` over `scripts/rtw88_pcap_replay.py`.
- `[SRC]` = vendor C `file:line`; `[WIRE]` = cold-boot pcap frame.
