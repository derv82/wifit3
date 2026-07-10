# RTL8814AU (8814au_dkms)

A cleanroom port of the Realtek PHYDM/ODM vendor source (morrownr `8814au` 5.8.5.1).
RTL8814AU silicon: 4T4R (2T4R on a non-superspeed USB link), dual-band 802.11ac,
HALMAC + PHYDM, firmware-based. ALFA AWUS1900, USB `0bda:8813`. This is NOT the
mainline-`rtw88`-derived `chips/rtw88_8814au/` — different addresses, init flow, and
firmware-download mechanism. The whole reason for the re-port is the vendor driver's
2.4 GHz monitor RX breadth (the mainline driver miscalibrates and reads close APs ~36 dB
low). Standalone — does not import `chips/rtw88_base/`. Playbook: `docs/porting/METHODOLOGY.md`.

## Status

Registered as the DEFAULT driver for `0bda:8813` (`WIFIT3_RTL8814=mainline` falls back).
Cold init + firmware boot, 2.4 + 5 GHz monitor RX, RSSI, and the full attack suite
(deauth, handshake, PMKID, WEP replay + chopchop, WPS PIN + PBC) all hardware-proven.
5 GHz inject/deauth confirmed on air (a live ch36 deauth captured the reconnect 4-way,
34/34 EAPOL on target). `verify_pcap` reproduces all three cold boots byte-for-byte
through the turn-on tail; the per-channel tunes (2.4 + 5 GHz) are byte-diffed by
`verify_channels.py`. Monitor RX is fully promiscuous both directions (ToDS M2/M4 seen),
so WPA handshake capture works.

The 2026-06-05 soak flagged intermittent 2.4 GHz dropouts under sustained hopping (one 60 s
bucket with zero 2.4 GHz APs; 5 GHz unaffected), but a 30-min re-soak on 2026-07-06 did not
reproduce it — 2.4 GHz held 57-79 active APs every bucket, no dropout (see Debug log). Scan +
Stress are flagged in VERIFICATION.md (the Stress flag predates this re-soak). 40/80 MHz bonded widths
are out of scope (20 MHz primary only). The full data-frame `update_txdesc` TX path is
not ported (inject/deauth/replay use the minimal mgmt descriptor at a fixed rate); the
USB3 firmware/burst branch is unported (latent gap if a card ever links USB3).

## Gotchas

**The 8814A's strong-AP RX inversion was CCK packet-detection, not gain.** A close
CCK-only beacon (1 Mbps) was heard worst in the room while loud OFDM neighbours were
fine — the classic "gain too high / saturation" look, but it is NOT that. Root cause:
CCK-PD runtime adaptation was unported. Init seeds the most-sensitive level (0xa0a LV_0
= 0x40); the kernel then *adapts* it, raising to LV_1 (0x83) when the CCK false-alarm MA
exceeds 1000. Left at LV_0 on a busy channel, the over-sensitive detector is swamped by
CCK false alarms and misses the real strong CCK beacons. Forcing LV_0→LV_1 roughly
doubled reference-AP reception. Ported faithfully in `watchdog._cck_pd` (carries
`cck_fa_ma` + `cck_pd_lv`); the MA seeds from the first tick's raw count, even a 0
[phydm_cck_pd_th:1041] — the byte-exact vendor sequence the pcap gate replays at tick #2.

**This card is 2T4R on USB, not 4T4R.** The efuse antenna option says 4T4R, but a
non-superspeed link resolves `rf_path = RF_2T4R → max_tx_cnt = 2`, so the TX-power PG
loader never loads the 3rd-stream diff (`efuse.MAX_TX_CNT = 2` caps it). The 2.4 GHz gate
never caught this (its 3rd-stream diffs are 0 or nss=1-only); path C's 5 GHz fuse — a
nonzero 3rd-stream BW20 diff the wire does *not* apply — exposed it. nss=1 (the inject
rate) is unaffected.

**Firmware does NOT block-write over EP0.** It uses the 3081 IDDMA reserved-page path:
the blob streams as beacon-queue TX packets on bulk EP `0x02` (40-byte TX desc + payload),
and the 3081 DDMA channel copies each block into MCU IMEM/DMEM with a running checksum.
There is no 8814au blob in linux-firmware — the vendor C array (`array_mp_8814a_fw_nic`,
68320 B) is the source of truth. The legacy `_WriteFW`/`_BlockWrite` path is dead code.

**TX-power tables are dead code in this build.** It compiles with
`CONFIG_TXPWR_BY_RATE_EN=0` and `CONFIG_TXPWR_LIMIT_EN=0`, so the whole power-by-rate
(`phy_reg_pg`) and regulatory-limit (`txpwr_lmt`) machinery returns 0 / a non-binding
ceiling. The per-(path,rate) index collapses to `clamp(efuse_base + nTX_diff + 2, 0, 63)`.
None of that table machinery is ported.

**Band detection is stateless — the chip holds the current band in a register.**
REG_CCK_CHECK (0x454) bit7 records the band (5G→0x80, 2.4G→0x00); the tune path reads it
back and switches only on a real 2.4↔5 GHz crossing. No software prev-band state is
tracked, so any crossing (e.g. 165→2) is handled. The band switch is the first op of
the channel-tune path.

**RF register access is memory-mapped, not at the RF address.** A write rides the
per-path LSSI write register (A 0xc90 / B 0xe90 / C 0x1890 / D 0x1A90) as
`(addr<<20 | data) & 0x0FFFFFFF`; a read is a direct `read32(base + addr*4)` (base A
0x2800 / B 0x2c00 / C 0x3800 / D 0x3c00). Pseudo addresses 0xfe/0xffe are 50 ms settling
delays, not writes.

**Monitor RX deliberately diverges from the cold-boot pcap.** The capture was taken under
airmon-ng driving a *STA-initialised* driver through the cfg80211 STA→monitor dance (~300
ops). wifit3 is always-monitor, so `monitor.enter_monitor` runs only the vendor monitor
opmode entry (the last 10 ops: Set_MSR NOLINK + RCR `0x90003b2f` accept-all + RXFLTMAP0/1/2
`0xffff`), skipping the STA-mode artifacts. So `verify_pcap` stops at the turn-on tail;
the monitor block is verified out-of-line by `verify_pcap.verify_monitor_block`.

**A crc/icv-error frame is skipped, not bailed on.** `rx.iter_frames` continues the
USB-aggregated buffer walk past a bad frame (the vendor STA does `goto _exit` for
`mp_mode==0`); monitor must keep the good frames aggregated after a bad one. Only a
malformed descriptor length (no recoverable next-frame boundary) ends the walk. Frames
are FCS-stripped before the callback ([project_rx_frames_include_fcs]).

**Per-channel 2.4 GHz spur/NBI notch is real and was nearly skipped.** On 2.4 GHz ch 4-8
(spur 2440 MHz) and ch14 (2480) the tune computes a per-channel NBI notch tap; every other
channel disables NBI. The original skip-rationale ("2.4G has no spur") mispredicted, hidden
because `verify_pcap` only diffed ch1 (a no-spur channel). On 5 GHz only ch153 needs a real
notch at 20 MHz (the other 5 GHz notches are `#if 0` in the vendor source); ch140's case is
rfe-0-only, so this rfe-1 card needs no ch140 notch.

**WinUSB RX timeout errno differs from libusb.** `transport.bulk_in` originally treated
only errno-110 as a benign timeout, so the Windows error (`[Errno 10060] ... timed out` —
no "timeout" substring) was re-raised; 5 consecutive timeouts on quiet DFS channels tripped
`RxReaderThread`'s fatal limit and killed RX. Fixed by catching pyusb's `USBTimeoutError`
(+ errno 110/10060). 5 GHz has many empty channels and hit this where busy 2.4 GHz never did.

**The 5 GHz TxBBSwing fuse is burned on this card** (0xC7 = index 1, -3 dB, all four
paths) — unlike the unburned 2.4 GHz fuse (0xC6, 0 dB), so the 0 dB default would have been
wrong on 5 GHz. Both decode in `efuse`; `chan._set_bb_swing` writes the per-path TxScale.

## Orientation

`driver.connect()` chains EFUSE → firmware → MAC/BB/RF config → channel tune → TX power →
DIG seed → turn-on tail → monitor entry, then starts the RX reader + DIG watchdog. Bring-up
mirrors `rtl8814au_hal_init`; names match the vendor C, so grep
`usb_dumps_new/captures_rtl8814au/driver-source/` to cross-reference.

The probe-phase efuse read (`efuse.read_chip_params`) runs first and recovers `rfe_type`
(the BB-walker branch discriminator), `crystal_cap`, and the MAC live from the card — nothing
is hardcoded. BB/RF config walk flat-u32 tables through the shared phydm conditional walker
(`phy_cond._walk_table`); only `driver1` is compared (cut/package/interface/rfe nibbles),
and empirically only `rfe_type` selects branches in this card's taken path. Channel tune
(`chan`) unifies the 2.4 + 5 GHz fc-area / RF-mod / AGC-select into one range table. RSSI is
`rx.decode_rssi` (OFDM `((pwdb_all>>1)&0x7f)-110` for an `is_mp_chip`; CCK via a gain-index
lookup). The runtime DIG/AGC watchdog (`dig.watchdog_tick`) adapts IGI within [0x1c, 0x2a]
every 2 s, serialized with `set_channel`; `--no-dig` toggles it for A/B testing.

## Scripts

- `verify_pcap.py` — replays all three cold boots; byte-diff gate through the turn-on tail.
- `verify_efuse_pcap.py` — byte-diffs the probe-phase efuse read.
- `verify_channels.py` — per-channel tune byte-diff (2.4 + 5 GHz), the standing tune gate.
- `scan_hw.py` — live beacon/AP count (`--band 2g|5g|all`); the end-to-end RX check + ESSID-variance canary.
- `ab_scan.py` — staggered replug-between-runs A/B vs the mainline driver.
- `rx_saturation_probe.py` — per-AP CCK/OFDM, `--cck-pd`/`--cck-rx-path` sweeps, DIG `cck_pd_lv` trace.
- `cck_state_diff.py` / `rf_state_diff.py` — live MAC/BB and per-path RF state vs the kernel.
- `dump_tune_regs.py` — dump live tune registers.
- `extract_fw.py` / `extract_bb_tables.py` / `extract_rf_tables.py` — pull the FW blob + flat-u32 tables from the vendor C.
- `deauth_hw.py` / `wep_replay_hw.py` — live deauth and WEP replay harnesses (targeted-only, `--dry-run`).

## Debug log

### 2026-06 — strong-AP RX deficit: CCK-PD adaptation (root cause)

The 2.4 GHz reference AP (CCK-only, 1 Mbps beacons) was heard worst of the room (~3/s,
ranked #6-8) while loud OFDM neighbours were fine and the aggregate looked healthy — the
saturation *look*, but not saturation. Ruled out in source (not via the gate): init/tune
match the kernel; live regs were correctly 20 MHz/ch1; IQK/LCK/pwr-track are commented out
for the 8814A; RxGainOffset / `phy_ModifyInitialGain` are MP-only no-ops. The actual cause
was unported CCK-PD runtime adaptation (now a Gotcha). Two supporting RX-path fixes landed
the same effort: decode moved off the asyncio loop onto the reader thread (the loop held the
GIL and gapped reads under the 4T4R flood), and the RX reader is started *before*
`enter_monitor` opens the accept-all RCR (kernel order: post URBs, then write RCR
— [project_rx_reader_start_ordering]). Result: ~3/s ranked #6-8 → 6.7/s, median 7, ranked
#1, zero dead seconds. Residual ~6.7 vs MT7921's 8.1/s (same room/USB2) is chip-inherent —
a bias-free deep-dive (operational-tail by-register, live MAC + every BB page byte-diff, all
4 RF paths) found no remaining RX divergence; the 8814A is simply a weaker CCK receiver in a
busy channel (its own kernel's 8.7/s was on a *quiet* channel).

### 2026-06 — DIG regression refuted by controlled A/B

The runtime DIG watchdog was the prime suspect for a strong-AP RX regression; a fixed-channel
and hopping A/B (DIG ON vs OFF) refutes it. The per-tick `fa_cnt` stays bounded and bounces
(it does not climb monotonically), so the OFDM/CCK/page-F CCA reset pulses do clear the
counters. Fixed-ch1 30 s, the strongest AP (~-44 dBm) held 150-197 beacons with DIG ON,
matching DIG OFF (150-185). IGI rides up to the 0x2a ceiling on a busy band and steps back
down on quiet channels; 0x2a is the least-sensitive bound but sits well below the strong AP's
level, so it doesn't deafen RX. A single DIG-OFF outlier drove the original "halved frames"
worry and did not repeat; the earlier "collapse" was an uncontrolled hop-vs-fixed comparison
at a different time. Beacon *count* follows beacon interval / multi-BSSID radios, not RSSI.

### 2026-06 — the one unported watchdog member (RX-neutral)

The halrf TX-power thermal-delta tail (the `verify_pcap` frontier at tick #2) is the only
periodic write the watchdog doesn't model. Confirmed RX-neutral by op-trace (not by label):
the tick's RX work (DIG, EDCCA, NHM, FA reads) is all modeled; the unported tail reads a
power/thermal reference (read-only here), reads/writes-back the TX AFE unchanged, and writes
the TX digital scale (`bb_swing`/`ele_D`) while preserving the low RX-IQ bits — TX power only,
no RX register modified. Porting it would complete the watchdog + advance the gate but is
large/stateful (per-path swing LUT + thermal MA) and RX-neutral.

### 2026-06-05 — soak: intermittent 2.4 GHz dropouts under sustained hopping

The 30-min dual-band soak (`scripts/diag/reports/rtl8814audkms_20260605-231039.md`) showed
the signal-strength fix (-45 vs -81 dBm) did NOT make 2.4 GHz RX solid: one full 60 s bucket
with zero 2.4 GHz APs (5 GHz unaffected), periodic dips, the jitteriest frame rate of any
soaked card. No progressive degradation (98→98) and 5 GHz rock-steady, so it survives, but
the mainline fallback stays until the 2.4 GHz AGC/DIG/band-recovery path is byte-diffed.

### 2026-07-06 — re-soak: 2.4 GHz dropout not reproduced

A 30-min dual-band hopping re-soak (`scripts/diag/reports/rtl8814audkms_20260706-030622.md`,
22 channels) did NOT reproduce the 2026-06-05 dropout: 2.4 GHz held 57-79 active APs across
all 31 buckets (min 57, no zero bucket), 5 GHz 28-45, active-BSSID trend rose (101→119, ratio
1.18). No death, no progressive degradation. Parse-quality WARNs are hopping artifacts (OUI
garbage 5.0% is dominated by ff:ff:ff:ff:ff:ff broadcast; beacon-channel mismatch 22% is the
known cross-tune-window effect). Cleared from BUGS.md.

### 2026-07-09 — M3c halrf: thermal TX-power tracking ported (verify_pcap → IQK frontier)

The watchdog's halrf member was a stub (thermal re-arm only); the thermal-DELTA correction that
fires from tick #2 was unported — the verify_pcap operational frontier. Ported the vendor
`odm_txpowertracking_check_ce` + `..._callback_thermal_meter` MIX_MODE path (`powertrack.py` +
`powertrack_tbl.py` delta-swing tables): the two-phase arm/read gate, thermal averaging,
`odm_get_tracking_table` per-path index walk, and the per-path TXAGC (0xX94) / BB-swing (0xX1C)
writes; plus `odm_clear_txpowertracking_state` (incl. the `thermal_value → eeprom` re-base at
halphyrf_ce.c:162) and the `phy_SetBBSwingByBand` `default_ofdm_index` band adjust on each hop.
`eeprom_thermal` now reads from efuse 0xBA. The tick-2 BB-swing (0x197) is COMPUTED (eeprom 0x23,
thermal 25 → delta 10 → 2G down-table 4 → idx 24−4=20 → `tx_scaling_table_jaguar[20]`), not
hardcoded. verify_pcap capture-1 now reaches the 8814A IQK backup (`R 0x0520`, op 10338 — the next
milestone, `do_iqk_8814a`); capture-3 reaches op 13509 (4 ticks, both bands, incl. the 5 GHz
band-switch correction) before the pre-existing LED-mid-hop harness-interleave limit. The gate's
`Walk.run` now credits ops matched before a mid-handler divergence (fail-closed unchanged), so the
frontier reports the true deepest-reproduced op. RX-neutral (TX-power thermal compensation) — ported
for capture fidelity, not an RX fix. Preceded by the faithful CCK-PD unconditional-seed fix that
cleared the tick-2 CCK divergence.

### 2026-07-09 — verify_pcap: drain the interleaved LED blink (async producer)

The LED-blink timer (0x0060 R/W) fires on its own ~2 s cadence, so the wire splices its op pair
into whatever handler is mid-flight — a channel tune's txagc burst or an IQK one-shot. The
single-cursor harness dispatched hops/ticks/LED atomically, so a mid-handler LED desynced the walk
(the frontier on capture-2 op 8212 / capture-3 op 13509). Added an optional `interleave` hook to the
shared `ReplayTransport` (default off — no effect on other chips) that the 8814au recipe drives to
drain each interleaved blink through the real `led_blink` at the cursor (byte-verified, non-recursive
— it skips 0x0060 so the blink's own ops don't re-enter). Result: capture-2 → op 29274/29811 (98%,
reaches the aireplay TX bulk-OUT), capture-3 → op 29862/30582 (98%, reaches an IQK at tick #40).
capture-1 unchanged (its IQK precedes any LED). Remaining frontiers: the 8814A IQK (cap-1/cap-3) and
the aireplay injection (cap-2).
