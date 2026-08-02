# RTL8812AU (8812au_dkms)

A port of morrownr's vendor/DKMS driver for the RTL8812AU (2T2R, 2.4 + 5 GHz 802.11ac;
ALFA AWUS036ACH, `0bda:8812`), on the shared `chips/rtl88xxau_base/` Jaguar core plus the
8812a-specific modules. C-cut silicon, legacy (non-HALMAC) efuse path.

## Status

Cold init, FW, MAC, BB/RF, channel tune, TX-power, phydm init, and monitor entry are
byte-for-byte against morrownr's cold-boot captures (both capture-2 and capture-3). 2.4 GHz
monitor RX works on hardware (clean beacons, stable 10+ min on ch1), and is the manager
default for `0bda:8812` (`WIFIT3_RTL8812=mainline` falls back). 5 GHz tune is byte-for-byte
verified across all 37 hops including both band crossings and all DFS channels; 5 GHz RX off
the antenna is HW-confirmed at kernel parity (2026-06-28 wifit3-vs-`rtw88_88xxa` A/B: 5 GHz breadth
57=57, ~9.0–9.18/s on the populated non-DFS channels, RSSI −0.1 dB).

TX is HW-confirmed on 2.4 GHz — deauth, WEP (fake-auth + ARP replay), ChopChop, and WPS
(PIN brute + PBC), all via the stock engines over `inject_frame` (bulk-OUT 0x02). No TX pcap
exists (the capture's build injected nothing), so TX is gated by live scripts, not a byte
diff. 5 GHz TX is HW-confirmed too (2026-06-28: deauth / handshake / PMKID / WPS-PBC fired live
on 5 GHz, auto-ACK working). A 30-min dual-band soak showed no
degradation.

## Gotchas

**The monitor / airmon tail is the RX fix — not skippable.** The demod produces `crc_err`
garbage unless the port reproduces morrownr's monitor RX-START tail (`monitor.set_monitor_mode`):
after the phydm EDCCA PSD search, morrownr re-runs a full channel tune (`set_channel_bw` +
`set_tx_power`) before entering monitor opmode. That re-tune restores the RF/BB to a clean
receive state after the EDCCA search has toggled the LNA / walked the thresholds. A direct
`enter_monitor` that skips it gets garbage. (Not bisected to the minimal sub-write; the
channel re-tune is the prime suspect, RPWM-wake/opmode not ruled out.)

**The monitor RCR is a separate delivery gate from the demod fix.** morrownr/airmon's tail
leaves RCR `0x90000001` (`AAP|APP_PHYST|APPFCS`, leaning on RXFLTMAP). That reproduces
airmon's exact chip state but does NOT deliver management/broadcast frames (beacons) into
wifit3's RX pipeline — it lacks the accept-mgmt/broadcast class bits and RXFLTMAP alone does
not substitute. So the driver re-opens the filter to wifit3's own monitor RCR `0x9000382F`
(accept all *good* frame classes; CRC/ICV-error frames still dropped) right after the tail.
The tail's RF re-tune is the demod fix; the RCR is the delivery gate — both are needed.

**No runtime IQK.** morrownr receives with no IQK (the one-shot is absent from all captures).
Run `rx_diag.py --no-iqk`. `iqk.py` is a port, kept but unused.

**This chip is C-cut, not B-cut.** `REG_SYS_CFG[15:12] = 1` and `read_chip_version_8812a`
adds +1 for the 8812, so `CUTVersion = 2 = C_CUT`. Two RF consequences: the rCCAonSec CCA
toggle ("FIX-2") must NOT run (C-cut skips it — `rf._rf_read_cca_off = False`), and
`phy_FixSpur_8812A` takes the C-cut branch (3 writes incl. 0x8C4, per-path inside
`phy_SwChnl`), not the 1-write non-C-cut branch.

**Three bulk-OUT endpoints.** bulk-IN 0x81, bulk-OUT 0x02/0x03/0x04, int-IN 0x85 → the
3-out-EP queue map (`TRXDMA_CTRL` low bits `0xF5B0`); the 3-EP path does NOT run
`init_hi_queue_config`. TX must send on bulk-OUT 0x02.

**8051 reset is BIT3 on the 8812** (BIT0 on the 8821) — `REG_RSV_CTRL+1`, threaded as
`reset_8051_bit` through `base/firmware.bring_up`.

**morrownr build flags, deduced from the bytes:** `CONFIG_BEAMFORMER_FW_NDPA` ON → TX page
boundary `0xF7` (not 0xF9) and the RXFLTMAP1 NDPA bit (`0x0420`); `CONFIG_RF_POWER_TRIM` OFF.
EFUSE: rfe_type 3, board_type 0xD8 (ext-LNA/PA both bands), bb_swing 2.4G 0 dB. Per-nTX PG
diffs are CUMULATIVE (BW20-2S = base + diff[1TX] + diff[2TX]); the TX-power training word
compares as u32. Firmware: `array_mp_8812a_fw_nic`, 27030 B.

**mainline RF-wedges on dual-band hopping; this port does not.** On a 2.4+5 GHz hop the
mainline `chips/rtl8812au/` driver wedges RX at ~110 s ("RF synth lost lock during hopping, a
known rtw88 limitation, worst on 5 GHz") and stays dead; dkms climbs throughout a 240 s (and
7 min) run with no wedge, and cold-boots to recover a chip mainline's warm-reattach can't.
Matched-coverage throughput is tied — the earlier "mainline faster" gap was only mainline
skipping DFS.

## EFUSE / silicon variants — any-card support

The port runs on any RTL8812AU card that enumerates as `0bda:8812`, not only the captured
ALFA AWUS036ACH (rfe_type=3, C-cut, board_type 0xD8). Two kinds of discriminator: **values**
(crystal_cap, per-rate TX power, bb-swing, MAC) are consumed by computation, so any burn
already works; **branches** (rfe_type, silicon cut, board_type ext-PA/LNA) select code paths.
The BB/RF/AGC tables (`bb_phy_reg_tbl`, `bb_agc_tbl`, `rf_radio{a,b}_tbl`) carry every
board_type/cut row inline and are resolved by `phy_cond` (`build_jaguar_params` threads the
runtime cut + board_type) — a different board_type/cut card walks ported-but-untested *rows*,
not unported code.

Generalized branches (all keyed on the runtime EFUSE / cut; the rfe=3 / C-cut wire is
byte-unchanged — see `verify_pcap` capture-2 6213 + capture-3 6376, `verify_channels` 37/37):
- `efuse._parse_rfe_type` — `Hal_ReadRFEType_8812A` BIT7 external-PA/LNA decode (3/0/2/4) + the
  2013 type-4-with-ext-amp workaround. Was hardcoded to 0 for any BIT7-set burn; now decodes
  from the `_ext_amplifier_flags` (ext-PA/LNA) the port already reads for board_type.
- `chan._set_rfe_2g` — added the vendor `rfe_type == 5` case (path-A partial pinmux byte +
  inv-byte RMW clearing BIT0). It previously fell through the 0/1/2/4 branch and mis-wrote a
  full 0x77777777 pinmux + inv 0x000. (`_set_rfe_5g` already had all cases 0–6.)
- `chan._fix_spur` + `rf._rf_read_cca_off` — `phy_FixSpur_8812A` cut branch (non-C-cut does
  only the 2.4 GHz 0x8AC[9:8] workaround, no 0x8AC[11:10]/0x8C4[30]) and `phy_RFSerialRead`'s
  CCA-on-secondary toggle (non-C-cut brackets every masked RF read). Both were hardcoded to
  C-cut; now gated on the runtime `is_c_cut` (`REG_SYS_CFG[15:12]+1 == C_CUT(2)`).

`connect()` logs the detected config once — `sys_cfg / cut / rfe_type / board_type /
crystal_cap / mac / bb_swing`, tagged `[untested variant]` when the card is not (rfe_type=3,
C-cut) — so an odd card is diagnosable from one line.

### Untested variants (ported-from-C, hardware-unverified; residual gaps)
Only the ALFA (rfe=3, C-cut) capture exists, so the branches below are ported 1:1 from the
vendor C and run "give-it-a-shot", not fail-loud:
- **Non-C-cut FixSpur + RF-read CCA toggle** — the CCA toggle is functionally load-bearing (a
  B-cut card's masked RF reads latch stale without it); ported but hardware-untested.
- **rfe_type ≠ 3 RFE pinmux** (cases 0/1/2/4/5/6, both bands) — ported, only rfe=3 is pcap-gated.
- **`phy_InitRssiTRSW` rfe==3 `rssi_trsw` seed** — sets driver-struct vars only consumed by
  `odm_LNAPowerControl` (not run in this port); no wire effect, so not ported for other rfe.
- **BT-coex RFE sub-path** (`CONFIG_BT_COEXIST` rfe_type 1) — non-BT board; not ported.
- **`phy_SpurCalibration_8812A`** — `mp_mode`-gated, never runs in this (mp_mode=0) build.
- **Registry RFE / amplifier overrides** (`GetRegRFEType`/`GetRegAmplifierType`) — userland has
  no registry, so the decode assumes AUTO(64) throughout.

## Orientation

C-cut 2T2R Jaguar; `REG_SYS_CFG = 0x04411137`. Cold bring-up runs efuse → FW → MAC → BB/RF →
channel → TX-power → phydm-init → monitor through `base/firmware.bring_up`. The RX fix lives
in `monitor.set_monitor_mode` (the re-tune tail) plus the driver's RCR re-open. 5 GHz band
switch is `chan._switch_band_5g` + `_set_rfe_5g` (phy_SwitchWirelessBand8812 5G branch, all
rfe_types). `Rtl8812auDkmsDriver` (`driver.py`) starts the RX reader before the monitor RX
gate, runs a 2-path DIG watchdog (`dig.watchdog_tick`), then enters monitor. TX rides the
shared base fake-txdesc (`rtl8812a_fill_fake_txdesc`) on bulk-OUT 0x02.

Names match the vendor C — vendor source + captures in `driver_captures/captures_8812au/`
(8812a HAL in `hal/rtl8812a/` + `hal/phydm/`). capture-2/3 are complete; capture-1 is
truncated. The confirm-`0bda:8812`-enumerates-first habit matters: the squishy USB-C→A
adapter falls out, and a vanished device is a fallen plug, not a result.

## Scripts

- `verify_pcap.py` — the cold-boot byte gate; feeds recorded reads back so RMWs and the EDCCA search reproduce. Run on BOTH capture-2 and capture-3.
- `verify_channels.py` — runtime-tune gate; byte-diffs every `iw set channel` hop (incl. band crossings + DFS) with no anchoring.
- `rx_diag.py` — live RX classifier (`--no-iqk`; `--channel 36` for 5 GHz).
- `deauth_hw.py` / `wep_replay_hw.py` — live TX gates (no TX pcap exists).
- `extract_tables.py` — golden-hashed tables/FW.

## Verification coverage

The cold-boot gate diffs every driver-emitted control read/write contiguously, 0 skipped
(6213 ops on capture-2, 6376 on capture-3); the ~163-op delta is only the EDCCA PSD search
running longer on one boot's RF environment, both reproduced byte-for-byte. The EDCCA search
is reproduced (recorded PSD fed back), not stripped. Not in the gate, each accounted for:
bulk-IN RX (chip→host input); channel hops (= `set_channel_bw` + `set_tx_power`, proven
byte-identical to the monitor-tail re-tune); and the DIG watchdog (`dig.watchdog_tick`) —
ported from kernel but NOT byte-diffed against the capture's runtime ticks, the one
ported-but-not-byte-verified path, and the next step for long-run gain stability. The 1 µs
inter-write `ODM_delay_us(1)` gaps are not USB ops (invisible to a byte diff) and are swamped
by control-transfer latency; the 8812 RADIO tables have no `0xfe`/`0xffe` settle-delay rows.

## Debug log

### 2026-06-05 — RX root cause + the WRONG leads (do not re-chase)

Demod produced `crc_err` garbage until the port reproduced the monitor RX-START tail.
Progression while isolating it: minimal monitor, no search → 117 KB garbage; + live EDCCA
search → 467 KB garbage; + the monitor tail → real beacons (27 APs, valid frame-control, real
OUIs, ~6.8 MB/12 s). Disproven earlier theories, kept here so they aren't re-chased: "B-cut"
(it's C-cut — corrected the FIX-2 and FixSpur branches above); "needs runtime IQK" (morrownr
runs none); "the monitor/airmon dance is skippable" (it is the RX fix). Same campaign brought
TX up across 2.4 GHz: deauth burst deauthed a client and its reconnect 4-way (incl. M2/M4
ToDS) was captured; WEP fake-auth + ARP replay locked a winner at hundreds of IVs/s; ChopChop
recovered keystream byte-by-byte to full plaintext; WPS PIN-brute and PBC both succeeded —
all stock engines over `inject_frame`, no TXPAUSE/REG_CR surgery.

The Monitor RCR gap surfaced only when the driver ran for real: the old "RCR is not the RX
blocker" note had only ever been tested via rx_diag's permissive `0x90003B2F` override, never
`0x90000001` alone, which is why the delivery gate was missed until the driver used airmon's
actual value.

### 2026-06-05 — 30-min dual-band soak, no degradation

`scripts/diag/sweep.py --skip-baseline --longrun-min 30 --hop-interval 0.25` over all 38
channels (2.4 + 5 GHz incl. DFS): active BSSIDs 118→121 (first-3 vs last-3 60 s buckets,
ratio 1.03), 5 GHz active count flat ~49–57 the whole run — the band that wedges mainline at
~110 s — frames steady ~1.6–2.2 k/60 s. Two diag WARNs are fast-hop measurement artifacts,
not driver faults: OUI "garbage" 4.85% = broadcast/wildcard BSSIDs the parse-quality probe
rejects by design (all `ff:ff:ff:ff:ff:ff` probe-request addr3); beacon-channel mismatch
31.1% = the probe snapshotting `current_channel` after the RX hopped on at 0.25 s/hop plus
2.4 GHz adjacent-channel bleed.
