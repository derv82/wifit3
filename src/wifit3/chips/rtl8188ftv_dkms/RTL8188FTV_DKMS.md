# RTL8188FTV — DKMS (vendor) port

## Captured Wireless Card
- No-name `0bda:f179` 802.11b/g/n dongle — 1T1R, 2.4 GHz only.
- USB2 (Bus 001, device `063` in the vendor capture; no post-FW re-enumeration).
- Captures: `driver_captures/captures_8188fu/capture-1.pcap` (cold-boot, 30k packets / 57 s: airmon start → 10 s native airodump hop → 15 s fixed-ch1 over-air → `iw set channel` 1–14 → aireplay `--test`) + `capture-1_logs/` (timeline, driver/sysinfo logs, `airodump-fixed-ch1.cap` over-air ref, vendored `driver-source/` + `firmware/rtl8188fufw.bin`).

## Linux Driver Source
- Link: `https://github.com/kelebek333/rtl8188fu` (fork of `ulli-kroll/rtl8188fu`, Rockchip-sourced Realtek vendor tree).
- Type: vendor/DKMS.
- Version: `v4.3.23.6_20964.20170110`, commit `799fa0beacadce84bef6514d5948b454a8037c96` (2026-09-07, kernel-6.12 compat fix), module `rtl8188fu`, captured on `6.12.107+deb13-amd64`.
- Vendored in-repo: `driver_captures/captures_8188fu/driver-source/` (auto-collected by `capture.py`).
- Build notes: stock `Makefile` has `CONFIG_POWER_SAVING=y` / `CONFIG_WIFI_MONITOR=n`; the capture was built with `POWER_SAVING=n` / `WIFI_MONITOR=y`, else no monitor/airmon-ng. Conflicts with `rtl8xxxu` / `r8188eu` (blacklisted at capture).

## Python Port Details
- VID/PID: `0bda:f179`; selected by the `rtl8188ftv` family row in `device/manager.py` (this package wins by default, `WIFIT3_RTL8188FTV=mainline` opts back to `chips/rtl8188ftv`).
- Status: **M1–M8 green; cold bring-up + live RX/TX verified.** M1 (probe chip-version read: cut 1, SMIC, 1T1R),
  M2 (EFUSE map + full parse suite: ID 0x8129, MAC/VID:PID/chplan-0x20 all at
  independent ground truth; hidden-report C2H handshake + probe power-off),
  M3 (power-on, verified three times across both captures), M4 (LLT +
  TX-report + FW download + ready, verified three times: probe + open on
  capture-2, open on capture-1). `verify_pcap` PASS on both captures: M5a
  (antenna selection + MAC table, 119 ops both captures), M5b (BB config +
  crystal, 280 ops both captures), M5c (RFENV setup + RadioA table with
  B6/B2 readback loops + TxPowerTrack load, 180 ops both captures), M5d
  (MISC02 queues/pages/filters, 43 ops both captures), M5e (beacon/burst/USB
  agg/drop-check/lifetime/turn-on, 41 ops both captures), M5f ch1 tune
   (38 ops) + TX power (40 ops, 78 total, both captures), M5h start (CAM
  invalidate `W32 0x670=0xC0000000` + MISC11 tail `0x423=0xFF` /
  `0x4CC=0x0201FFFF` + GPIO `R/W 0x40`, 5 ops both captures), DM-init
  prologue (CCK/RX-path + DIG IGI + NHM + adaptivity + CFO ATC + thermal
  swing, 22 ops both captures), LC standalone (TX-pause branch + RF 0x18
  backup/LCK/ready-poll/restore, 68 ops both captures), IQK standalone
  (path-detect + Path-A TX/RX x2 workers + similarity break + matrix fill
  + BB recover + RF-path restore, 410 ops both captures, final=0),
  thermal trigger (RF 0x42 BIT17|BIT16, 8 ops both captures), monitor
  entry (MSR NOLINK + RCR all-accept + RXFLTMAP2, 4 ops both captures),
  station opmode (`hw_var_set_opmode` STATION via `rtw_hal_init_opmode`:
  BCN_CTRL TSF-UDT + MSR + `StopTxBeacon` + BCN_CTRL `0x19`, 8 ops both
  captures,   `RegFwHwTxQCtrl`/`RegReg542` threaded as hal state from M5e),
  channel-switch unit (`SwChnl` + `SpurCal` incl. PSD/notch branch +
  `PostSetBW` + `RF6052BW` + `SetTxPowerLevel` with CCK/OFDM remnants):
  ch1 open-restore (78 ops, both captures) and the full session after it
  (capture-1): airodump hops 1,7,13,2,8,3,9,4,10,5,11,6,12 + fixed-ch1 +
  `iw` sweep 1-13 + final ch1, each verified standalone against the same
  unit (78 ops skip-spur, 93-104 ops PSD-spur with/without notch) to the
   cap1-op1279 / cap2-op2914 frontier. Watchdog ticks (`dm.watchdog_tick`:
   RX-FIFO check + FA hold/reads/release + DIG (unlinked bounds/thresholds,
   change-gated write) + adaptivity EDCCA (ability-clear mode2 path) +
   CCK-PD (fail-count threshold, change-gated) + RA retry count
   (`phydm_ra_dynamic_retry_count` noisy-flip `0x430`/`0x434`, first tick
   only) + thermal trigger/callback alternating on `TM_Trigger`: all 28
   cap1 ticks + 27 cap2 ticks verified, zero skips. Thermal tracking
   callback (`track.tracking_callback`: 4-average thermal vs EFUSE `0x1A`,
   MP USB `2GCCKA` delta pair since `TxRate` stays MGN_1M, MIX_MODE
   `setIqkMatrix` + limit-row CCK swing + TxAGC sections): 3 setpwr
   callbacks cap1 (eff 28/29/31 → Absolute +1/+2/+3, C80 idx 29/30/31) +
   2 cap2 (eff 28/29 → +1/+2); every other query fires with offset 0 and
   no wire. Runtime CCK remnant evolves +0 (open) → +1 (hops) → +2
   (fixed + sweep ch1-10) → +3 (sweep ch11-13 + final), OFDM remnant stays
   0; remnants are first-principles callback state, every switch lane
   verifies with no peeking. One cap2 region (ch9 switch x callback tick,
   127 ops) is a USB control-transfer race between the watchdog and ioctl
   threads and is merge-walked (both scripts, every op consumed exactly
   once). RX path (`rx.iter_rx`: 24B desc +
  drvinfo + shift walk, 8B align, `RPT_SEL` C2H split, crc-stop like the
  source) decodes all 9354 bulk-IN completions: 16164 packets, 41 AP
  BSSIDs incl. all 3 log-known APs, 0 parser exceptions
  (`scripts/chips/rtl8188ftv_dkms/verify_rx.py`, on demand). Per-packet
  RSSI (`rx.signal_dbm`: CCK `odm_CCKRSSI_8188F` LNA/VGA table on
  drvinfo[5] for desc rates ≤ 3, OFDM `((drvinfo[4]>>1)&0x7F)-110` like
  `ODM_PhyStatusQuery_92CSeries`; `None` when `physt=0`, dispatch maps
  to −100) cross-checks against airodump PWR logs: BEWAVE_AP median
  −52 exact, Dzial −62 vs −57, dlink −46 vs −49 (air variance +
   averaging). Bring-up is one contiguous verified flow from probe through
   monitor entry, then a unified event walk (switches + ticks + one race)
   to the last op of both captures (cap1: 29 switches + 28 ticks,
   rem +3/+0; cap2: 28 switches + 27 ticks + 1 race, rem +2/+0), all
   first-principles. TX is a standalone bulk-OUT gate (`verify_tx.py`):
   87 station-capture URBs rebuild byte-exact (48 MGMT incl. the
   disconnect deauth, 39 BE DATA). `driver._inject_frame` sends MGMT
   and DATA on the monitor template; warm state (`is_chip_warm`: MCUFWDL
   != 0x05 or CR != 0) logs a warning and runs the cold bring-up over
   it, proven live 2026-09-24 (MCUFWDL=0xc6/CR=0x06ff warm, full RX mix
   after). Replug only if the scanner stays empty. Live RX proven
   2026-09-24 (hal tail was the gate); live deauth proven same day
   (client drop; 40 broadcast deauths on the wire, AP-spoofed TA,
   seq 0-39, all checksums valid). The C2H hidden report never posts
   live (0xFD echo; descriptive caps only, no functional impact). DKMS
   is the default (`DkmsFamily`), `WIFIT3_RTL8188FTV=mainline` opts out.
- Live verification 2026-09-24 (AR9271 witness, ch1): RX at the known-good bar (9.7 bcn/s, 11/11 channels tuned); breadth ties the mainline sibling (4 = 4 APs, RSSI ±0 dB — both hit the local strong-AP ceiling; the AR9271 hears 19, a 1T1R sensitivity gap, not a driver one), so the DKMS default is non-regressive. On-air TX-ACK 100/100 (copies collapse to 1; a dead target piles to the retry limit at 0 ACKs). Auto-ACK re-proven NONE (spoofed 8/100, silicon 8/100, controls 0). 20-min 13-ch hop soak flat (trend 4→4, ratio 1.00). Handshake/PMKID/WPS not run on DKMS here (no lab-AP/harness this session) — primitives are all proven (inject + AP-ACKs-our-forged-src + ACK tap), so they are expected-equivalent to the mainline sibling pending a hands-on pass.
- Related port: `chips/rtl8188ftv/` (same silicon, mainline `rtl8xxxu` 8188F vector, at kernel parity). Shares no code with it.
- Non-obvious in the port:
  - Wire is USB vendor-control `bRequest 0x05` register access (8-bit `usb_read8`/`usb_write8` ladder, `MAX_VENDOR_REQ_CMD_SIZE 254`) + bulk-IN EP `0x81` RX; FW download rides control transfers (`rtw_writeN`/`rtw_write8`), never bulk.
  - No `start`/`stop` cfg80211 ops: `iw set channel` hits `cfg80211_rtw_set_monitor_channel`, monitor entry is `cfg80211_rtw_change_iface` → `Ndis802_11Monitor` on the same netdev (no `wlan0mon` twin; capture keeps `wlx…`).
  - A 2 s `dynamic_chk_timer` → `traffic_status_watchdog` → `hal_dm_watchdog` tick plus the SW-LED blink timer interleave control traffic in monitor mode.
  - Monitor RCR appends FCS (`mode._RCR_MONITOR` BIT31), so every RX frame carries a trailing 4-byte FCS. The parser ignores it, but an ACK arrives as 14 bytes, not 10 — the ACK tap must match `len in (10, 14)`.
  - ACK RX-tap: `rx.admit_ack_frames`/`drop_ack_frames` toggle RXFLTMAP1 bit 13 (monitor entry leaves it at 0x0400, PS-Poll only); `_rx_dispatch` feeds 0xD4 ACKs to `record_ack`, enabling `inject_frame_slow_retry` software ACK-retry. Live-verified: the FTV tallies 100/100 ACKs as an `rx_autoack` prober.
  - Channel 14 is regulatory-disabled (`iw` rejects before the chip is touched); the sweep is ch1–13.
  - Probe ends powered OFF: `hal_read_mac_hidden_rpt` powers off (`CardDisable`, no deinit/FIFO quiesce) when HW init hasn't completed, so every cold plug downloads FW twice (probe + open).
  - `FirmwareDownload` always closes with `InitializeFirmwareVars` (`HMETFR=0x0f`), even on failure; the `&`-vs-`==` precedence in the self-reset gate is a live trap.
  - Poll-count timing varies run to run (power-flow 34 vs 37 ops); the gate must never assert exact op counts, only byte-exact replay.

## Known Problems
- Capture-1 opens mid-bring-up: all 30,642 frames are dev 63 starting at the
  power flow (tshark was still spinning up through enumeration + probe), so M1
  (chip version) + M2 (EFUSE) verify only against capture-2. Fixed going
  forward (`capture.py` `_wait_for_dump` + the enumeration check in
  `capture_vendor_8188fu.sh`).
- Capture-1 and capture-2 have no TX: aireplay never got a frame past
  the driver (see the monitor-TX notes below), so those pcaps carry zero
  bulk-OUT. M8 landed against capture-6 (station phase: associate +
  DHCP + ping + disconnect, 87 bulk-OUT URBs on EP 0x02 MGMT / 0x03 BE).
- Monitor-mode TX is unusable with this vendor build, twice over:
  `rtw_monitor_xmit_entry` drops any injected frame whose radiotap header
  is not exactly 12 bytes (silently: frees the skb, reports success, so
  aireplay counts them sent), and with carrier permanently OFF in monitor
  mode the qdiscs sit deactivated/frozen (`tx_dropped++`, zero URBs, even
  over AF_PACKET with PACKET_QDISC_BYPASS → ENOBUFS). Station mode is the
  working path (carrier ON at MLME connect). wifit3 injects via PyUSB
  bulk-OUT, so none of these kernel gates apply to the port; the station
  MGMT reference (44 probes + auth + 2 assoc + disconnect deauth, seq
  0-47) covers the deauth descriptor byte-for-byte.
- Solved: the frontier duplicate `R32 0xC80=0x390000E4` was the redundant
  second `odm_TXPowerTrackingInit` — `ODM_DMInit` (`phydm.c`) calls it
  directly right after `phydm_rf_init` already did; both funnel into
  `ThermalMeterInit` → `getSwingIndex` (`PHY_QueryBBReg(0xC80,
  0xFFC00000)`), everything else sw-only, so the two reads land
  back-to-back with nothing between. Value is the BB-table default
  (`halhwimg8188f_bb.c`: `0xC80, 0x390000E4`), never written before IQK
  fill. `dm.tracking_init_second`, 1 op, both captures.
- Solved: RF `0x55` BIT19-clear was `rtw_rf_set_tx_gain_offset` (core/rtw_rf.c,
  8188F case): `rtw_bb_rf_gain_offset` runs after the opmode enqueue
  (`CONFIG_RF_POWER_TRIM` set, efuse kfree flag `0x01` = `KFREE_FLAG_ON`,
  zero bb_gain → offset 0 → `RF_TX_GAIN_OFFSET_8188F(0)` = 0); the masked
  `write_rfreg(0x55, 0x0FC000, 0)` is one 7-op LSSI read + 1 write, no
  `0x55` literal exists because the offset travels as `write_rfreg` arg
  (the `DBG_871X` readbacks compile out). Readback `0x82060` → write
  `0x2060`. `track.kfree_gain_offset`, 8 ops, both captures.
- Solved: the 78 ops between thermal trigger and opmode are
  `init_hw_mlme_ext` → `set_channel_bwmode(ch1, BW20)` → the already-ported
  `chan.switch_channel(ch1, rem 0, 0)` (RF18 masked RMW + ch1 spur skip +
  PostSetBW + BW20 RF + TXAGC sections, all no-change on first call), with
  the usb_halinit tail just before it (`misc.hal_init_tail`: NAV_UPPER
  `ceil(30000/128)=0xEB`, FWHW_TXQ_CTRL BIT12, MACTXEN|MACRXEN). The whole
  bring-up is now one contiguous verified flow from probe through monitor
  entry; LED init is register-clean (SW strategy, `misc.init_hw_led`
  early-returns).
- Solved: the 1M-lane question dissolved with first-principles remnants —
   the cited `0xE08` value (`0x0390202D`, final-ch1 switch) is base `0x1D`
   + remnant +3, and every CCK lane in both captures equals base + the
   callback's section-uniform `Remnant_CCKSwingIdx`. The two skipped port
   terms are provable no-ops in this build: `RegEnableTxPowerLimit` is 0
   (Makefile `CONFIG_CALIBRATE_TX_POWER_TO_MAX=y`), so
   `PHY_GetTxPowerLimit` returns `MAX_POWER_INDEX` before its table lookup
   and the `min(byRate, limit)` clamp never bites; the limit call's
   `CurrentChannel` already equals the new channel because
   `PHY_HandleSwChnlAndSetBW8188F` stores it before
   `phy_SwChnlAndSetBwMode8188F` runs the level.
- Solved: the tracking-callback delta-table gap. The live pair is the MP
   USB `2GCCKA_P/N` (`ODM_ConfigRFWithTxPwrTrackHeaderFile` ←
   `phy_RF6052_Config_ParaFile`, USB branch; header path always runs since
   `CONFIG_LOAD_PHY_PARA_FROM_FILE` is off): index [2,3,4,5] reads
   [1,2,2,3], the unique match in the tree for the recorded
   Absolute +1/+2/+3. `TxRate` stays MGN_1M (`pDM_Odm->TxRate` is 0 with no
   TX in monitor; `HwRateToMRate(0)` defaults to MGN_1M), so
   `GetDeltaSwingTable_8188F` picks the CCK pair. The 4-average explains
   the firing points (cap1 eff 28/29/31, cap2 eff 28/29); all other
   queries land on offset 0 with no wire.
- Solved: the first-tick `0x430`/`0x434` writes are
   `phydm_ra_dynamic_retry_count` (`phydm_rainfo.c`): `phydm_NoisyDetection`
   scores the FA/CCA counters (smooth init 0, `pre_b_noisy` init false),
   the first tick of each capture decides noisy and programs
   `0x430=0`/`0x434=0x04030201` once; later ticks never flip back.
- M8 landed (MGMT): `tx.py` builds the 40B descriptor
  (`rtl8188f_fill_default_txdesc`: bcmc mac_id 1, QSLT_MGNT, 11B raid 8,
  CCK-1M rate, HWSEQ_EN, retry 6 assoc-flow / 12 disconnect-inject,
  SPE_RPT only on the wait-ack disconnect deauth, XOR checksum over the
  first 32 bytes with the checksum field zeroed, 8B pad only when
  `(size + 40) % 512 == 0`) and sends `[desc | frame]` on bulk-OUT EP
  0x02; `verify_tx.py` replays all 87 station-capture URBs byte-exact
  (44 probes + auth + 2 assoc + deauth, mgnt_seq 0-47, plus 39 BE DATA
  with their own seq 1-39 and the EAP/ARP/DHCP 1M rule, EP 0x03).
   `driver._inject_frame` sends MGMT and DATA on the monitor template
   (frame-seq == desc-seq, `mgnt_seq` state; per-link station DATA rules
   stay unported — wifit3 injects in monitor mode only). Live deauth
   proven 2026-09-24 (client drop). Warm state warns and runs the cold
   bring-up over it (proven same day); replug only if the scanner stays
   empty. A skip-redundant-work warm-reattach could use a warm-plug
   vendor reference (`capture --iface`).
- Firmware-based hard-MAC: no auto-ACK for forged MACs — `FAKE_MAC = NONE`, re-proven on hardware 2026-09-24 (AR9271 prober: spoofed 8/100, own silicon MAC 8/100, controls 0/100; `enter_active_monitor` not overridden, so `rx_autoack` skips the spoofed pass). The vendor stack does not change this silicon limit. WPS/PMKID instead rely on the now-wired software ACK-retry (`_enable_rx_acks` + `inject_frame_slow_retry`), not HW auto-ACK.

## Driver Entry Points
- Bring-up: `driver.connect` → M1 probe + M2 EFUSE → M3 power →
  M4 FW#2 (open, `assets/rtl8188fufw.bin`) → M5a-f → M5h + DM-init +
  LC + IQK + thermal trigger → station opmode + monitor entry, then
  `RxReaderThread` (bulk-IN `0x81` → `rx.iter_rx` → `WlanFrameParser`).
  Cold-only (replug resets); `set_channel` reuses the switch unit with
  hal remnants (post-bring-up +0/+0 until tracking lands);
  TX/injection raises (no bulk-OUT reference).
- EFUSE / chip params: (M2) `ReadAdapterInfo8188FU` → `Efuse_PgPacketRead` + `HalEfuseMask8188F_USB` + `Hal_EfuseParse*`.
- Power off: (M2 tail) `CardDisableRTL8188FU` (LPS-enter + card-disable flows, no deinit at probe).
- Firmware: (M4) `firmware.download_firmware` / `start` + `init_firmware_vars` (128-B ladder + checksum/ready polls).
- Monitor entry: (M5) `cfg80211_rtw_change_iface` → `hw_var_set_monitor`.
- Channel tune: (M6) `cfg80211_rtw_set_monitor_channel` → `set_channel_bwmode` → `rtw_hal_set_chnl_bw`.
- RX: (M7) `rtl8188fu_inirp_init` + `recvbuf2recvframe` + `rtl8188f_query_rx_desc_status`.
- TX / inject: (M8) `tx.build_mgnt_desc` + `tx.inject_mgnt_frame`
  (`rtl8188fu_hal_xmit` / `mgnt_xmit` + `rtl8188f_update_txdesc` +
  `rtw_get_ff_hwaddr`); `verify_tx.py` replays the station-capture
  bulk-OUT byte-exact.
- ACK detection: RX-tap admit/drop (M7 tail); auto-ACK verdict deferred to hardware.

## Scripts
- `scripts/chips/rtl8188ftv/capture_vendor_8188fu.sh` — reproducible vendor capture (pin + monitor-enabled DKMS build + `capture.py` + bundle self-check).
- `scripts/chips/rtl8188ftv_dkms/verify_pcap.py` — cold-boot byte gate,
  probe through the last op of both captures (switches + ticks + race).
- `scripts/chips/rtl8188ftv_dkms/verify_tx.py` — standalone bulk-OUT
  byte gate against the station TX reference (capture-6.pcap).

## Debug log
- 2026-09-22 — vendor capture triage: 30k packets / 57 s, ~6k control setups all `bRequest 0x05`, bulk-IN `0x81` with live RX sizes, zero bulk-OUT (aireplay `No such BSSID available` against `a8:5e:45:04:ce:e0`); `iw set channel` rc=0 on ch1–13, ch14 rejected (`channel is disabled`, regulatory). Monitor lives on the `wlx…` netdev itself.
- 2026-09-24 — live run deaf on RX (bring-up 100%, hopping, zero
  bulk-IN payloads, zero errors) while captures stream RX from identical
  bytes. Two driver-vs-walk gaps found by diffing `_cold_bring_up` against
  the walk order: (1) M2-tail FW#1 ran before power_on#1 (vendor order is
  power_on#1 → C2H-req → FW#1 → C2H-collect → off; FW#1 went into an
  unpowered chip, C2H read back our own 0xFD) — fixed; (2) the driver
  skipped `misc.hal_init_tail` (MACTXEN/MACRXEN!), the mlme ch1 switch,
  `tracking_init_second` and `kfree_gain_offset` — added in walk order.
  Cross-check: neither capture has any RX payload before monitor entry,
  which follows the hal tail in both. Pending live proof on a fresh plug.
- 2026-09-24 — live RX proven (rich 70-505B frame mix after the hal-tail
  fix; APs in the scanner). Same session showed `inject failed` on DATA
  frames: the monitor path (`update_monitor_frame_attrib` + plain dump)
  puts DATA through the shared MGMT template (mac_id 1, MGNT queue,
  raid 8, retry FALSE), so `tx.inject_frame` now covers MGMT and DATA;
  per-link station DATA rules stay unported.
- 2026-09-24 — AR9271-witnessed verification + ACK-tap wiring. RX breadth
  ties mainline, on-air TX-ACK 100/100, auto-ACK re-proven NONE, 20-min
  soak flat (see Status). Wired the RX-ACK tap (`admit_ack_frames` +
  `record_ack`); a first cut missed every ACK because the monitor RCR
  appends FCS (ACK is 14 B, not 10) — fixed, then the FTV tallied 100/100
  as a prober. Open: handshake/PMKID/WPS campaign runs on DKMS await a
  lab-AP hands-on pass (no `wps_pin.txt`/harness/connected client here).
