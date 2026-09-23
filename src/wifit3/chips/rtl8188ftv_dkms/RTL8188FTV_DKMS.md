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
- Status: **M1–M4 green.** M1 (probe chip-version read: cut 1, SMIC, 1T1R),
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
  cap1-op1279 / cap2-op2914 frontier. Runtime CCK remnant evolves
  +0 (open) → +1 (hops) → +2 (fixed + sweep ch1-10) → +3 (sweep
  ch11-13 + final), OFDM remnant stays 0; remnants are peeked per
  instance from the recorded lanes (the producing callback's delta table
  is open, see below) while every other lane verifies. RX path (`rx.iter_rx`: 24B desc +
  drvinfo + shift walk, 8B align, `RPT_SEL` C2H split, crc-stop like the
  source) decodes all 9354 bulk-IN completions: 16164 packets, 41 AP
  BSSIDs incl. all 3 log-known APs, 0 parser exceptions
  (`scripts/chips/rtl8188ftv_dkms/verify_rx.py`, on demand). Next:
  thermal tracking callback + 2s watchdog ticks. Until the bring-up
  verifies end to end, keep `WIFIT3_RTL8188FTV=mainline`.
- Related port: `chips/rtl8188ftv/` (same silicon, mainline `rtl8xxxu` 8188F vector, at kernel parity). Shares no code with it.
- Non-obvious in the port:
  - Wire is USB vendor-control `bRequest 0x05` register access (8-bit `usb_read8`/`usb_write8` ladder, `MAX_VENDOR_REQ_CMD_SIZE 254`) + bulk-IN EP `0x81` RX; FW download rides control transfers (`rtw_writeN`/`rtw_write8`), never bulk.
  - No `start`/`stop` cfg80211 ops: `iw set channel` hits `cfg80211_rtw_set_monitor_channel`, monitor entry is `cfg80211_rtw_change_iface` → `Ndis802_11Monitor` on the same netdev (no `wlan0mon` twin; capture keeps `wlx…`).
  - A 2 s `dynamic_chk_timer` → `traffic_status_watchdog` → `hal_dm_watchdog` tick plus the SW-LED blink timer interleave control traffic in monitor mode.
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
- Capture-1 and capture-2 have no TX: aireplay `--test` found no such BSSID
  (`No such BSSID available`), so the pcaps carry zero bulk-OUT. M8
  (TX/injection) needs a capture against a visible AP.
- Open: the frontier op (2nd `R32 0xC80=0x390000E4`, back-to-back with the
  thermal-swing read, identical in both captures) has no source after
  exhaustive elimination — single `getSwingIndex` call site, single-execution
  DMInit chain, zero-read antenna/path/beamforming/dynamic inits, no
  leading-zero/decimal/computed-address spellings, no function-pointer
  dispatch. LC verifies standalone from its `0xD03` anchor past it.
- Open: RF `0x55` BIT19-clear (8 ops, both captures, right after the
  station opmode-set) with no caller found yet — no `SetRFReg(...,0x55)`
  literal or `0x5x` RF symbol exists in the tree; shape is a single
  partial-mask RMW (`rf.set_rf_reg` handles it once attributed).
- Open: pre-tracking 1M lane writes `0x02` instead of base (ch10 cap1,
  ch7 cap2) while 2M/5.5M/11M in the same section write base and the
  ch1-open instance keeps base — limits are section-wide, remnants are
  section-uniform, byRate tables have no per-channel 1M hole, so no known
  term produces a 1M-only -30. Post-callback instances write base+1
  (remnant CCK +1, OFDM +0), which the port threads as state.
- Open: the first tracking callback (thermal read `0x1070E0` = 28, delta 2
  vs EFUSE `0x1A`, MIX_MODE with `setIqkMatrix` hand-verified to ele_A
  `0xF4`/ele_C `0x001` + CCK swing tables at the LIMIT row 20 + CCK
  TxAGC re-apply) runs, yet every delta-swing table in the tree reads 0
  at index 2 (static DEFAULT/`_8188E`, runtime arrays with no para file
  since `BIT5` is clear in `rtw_load_phy_file`), which would force swing
  offset 0 and skip `SetPwr` entirely. The wire proves effective +1, so
  the running box's table source is unaccounted for — the callback port
  waits on it.
- Mapped, unported: thermal tracking callback (`setIqkMatrix` values
  hand-verified: ele_A `0xF4`, ele_C `0x001`) + `iw`-sweep switches
  (same unit, remnant CCK +1) + fixed-ch1.
- Firmware-based hard-MAC (from the mainline bring-up: no auto-ACK for forged MACs); the vendor stack is not expected to change that silicon limit — `FAKE_MAC = NONE`, to be re-proven on hardware.

## Driver Entry Points
- Bring-up: `driver.connect` → (M1) probe `rtw_drv_init` + `read_chip_version` → (M2) `ReadAdapterInfo8188FU` → (M3) `_InitPowerOn_8188FU` → (M4) `rtl8188fu_hal_init` → `rtl8188f_FirmwareDownload` → (M5) `PHY_MACConfig8188F` / `PHY_BBConfig8188F` / `PHY_RFConfig8188F`.
- EFUSE / chip params: (M2) `ReadAdapterInfo8188FU` → `Efuse_PgPacketRead` + `HalEfuseMask8188F_USB` + `Hal_EfuseParse*`.
- Power off: (M2 tail) `CardDisableRTL8188FU` (LPS-enter + card-disable flows, no deinit at probe).
- Firmware: (M4) `firmware.download_firmware` / `start` + `init_firmware_vars` (128-B ladder + checksum/ready polls).
- Monitor entry: (M5) `cfg80211_rtw_change_iface` → `hw_var_set_monitor`.
- Channel tune: (M6) `cfg80211_rtw_set_monitor_channel` → `set_channel_bwmode` → `rtw_hal_set_chnl_bw`.
- RX: (M7) `rtl8188fu_inirp_init` + `recvbuf2recvframe` + `rtl8188f_query_rx_desc_status`.
- TX / inject: (M8) `rtl8188fu_hal_xmit` / `mgnt_xmit` + `rtl8188f_update_txdesc` + `rtw_get_ff_hwaddr`.
- ACK detection: RX-tap admit/drop (M7 tail); auto-ACK verdict deferred to hardware.

## Scripts
- `scripts/chips/rtl8188ftv/capture_vendor_8188fu.sh` — reproducible vendor capture (pin + monitor-enabled DKMS build + `capture.py` + bundle self-check).
- `scripts/chips/rtl8188ftv_dkms/verify_pcap.py` — cold-boot byte gate (M1-M5c).

## Debug log
- 2026-09-22 — vendor capture triage: 30k packets / 57 s, ~6k control setups all `bRequest 0x05`, bulk-IN `0x81` with live RX sizes, zero bulk-OUT (aireplay `No such BSSID available` against `a8:5e:45:04:ce:e0`); `iw set channel` rc=0 on ch1–13, ch14 rejected (`channel is disabled`, regulatory). Monitor lives on the `wlx…` netdev itself.
