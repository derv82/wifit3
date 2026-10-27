# RTL8192CU

## Captured Wireless Card
- 2.4 GHz only. Captured card is an RTL8188CUS, 1T1R (`REG_SYS_CFG` 0x04400735 → `VERSION_NORMAL_TSMC_CHIP_88C`, 0x10).
- `0bda:8176`, USB 2.0 high-speed. Endpoints: bulk IN 0x81, bulk OUT 0x02 + 0x03, interrupt IN 0x84.
- Capture: `driver_captures/captures_rtl8192cu_caponly/capture-2.pcap` (cold boot, 6298 vendor register ops, all EP0 control; no bulk-OUT). No timeline or driver log in the bundle.
- Firmware uploaded in the capture is byte-identical to `rtl8192cufw_TMSC.bin` (16094-byte payload after the 32-byte header, v88.2).

## Linux Driver Source
- Repo: linux-stable, `drivers/net/wireless/realtek/rtlwifi/` (`rtl8192cu/`, plus the shared `rtl8192c/`, `rtl8192ce/*.h`, `usb.c`, `efuse.c`, `wifi.h`).
- Type: mainline rtlwifi (module `rtl8192cu`), not rtl8xxxu and not the Realtek vendor `8192cu` DKMS.
- Pinned: tag v6.12.107, commit `f717995cb7dcd8998ab15516b8006aea09cfde0d`.
- Vendored (sparse checkout): `driver_sources/rtlwifi-source-v6.12.107/`.
- Driver identity evidence from the capture:
  - op 2 writes 0x69 to register 0x0000: `efuse_power_switch` writes `maps[EFUSE_ACCESS]`, which `rtl92cu_hal_cfg` never sets (efuse.c:1109, sw.c:166-185). rtl8xxxu writes 0x69 to `REG_EFUSE_ACCESS` 0x00CF and reads `REG_9346CR` as 16-bit (`rtl8xxxu-source-v6.18` core.c:1788-1803); the capture reads it as 8-bit.
  - EFUSE byte read is `read_efuse_byte` exactly: W 0x31, R/W 0x32, R/W 0x33, R32 0x30 poll, R32 0x30 after the 50 us delay.
  - Firmware goes up in 64-byte control writes, then 8-byte, then 1-byte (`_rtl_fw_block_write_usb`, efuse.c:1299), one page select per 4 KiB page via `REG_MCUFWDL+2`.
  - Power-on (ops 1492-1536) follows `_rtl92cu_init_power_on` read-for-read (by inspection).
- Kernel version: not pinned by the capture (no driver log). The probe window replays byte-for-byte against v6.12.107; the power-on and FW-download sequences match it by inspection.

## Python Port Details
- VID:PID: the full `rtl8192c_usb_ids[]` table (72 IDs) in `__init__.py`; no other package claims them.
- Status: probe, `rtl92cu_hw_init` and the monitor-mode bring-up ported: `rtl_usb_start`, `rtl_op_configure_filter`, `rtl_op_config` channel switch (TX power, RF_CHNLBW, channel access, bandwidth; 20 and 40 MHz; 1T1R/1T2R/2T2R, UMC B-cut, regulatory 0-3, dynamic TX power levels), the `rtl92c_dm_watchdog` tick, and active monitor / RX ACKs. `verify_pcap` replays the whole capture, 6298/6298 ops byte-for-byte. RX descriptor decode, TX and `inject_frame` are not ported.
- Firmware: `assets/rtl8192cufw_TMSC.bin`, `_A.bin`, `_B.bin` (linux-firmware `rtlwifi/`, SHA-256 `c083f2db…`, `32846065…`, `479e5e67…`), picked by `rtl92cu_init_sw_vars` (sw.c:48): UMC A-cut 1T → `_A`, UMC B-cut → `_B`, else `_TMSC`. The alt name `rtl8192cufw.bin` is not shipped.
- Related ports: none share code (rtl8xxxu also drives this silicon upstream; no rtl8xxxu 8192C port exists here).
- Non-obvious in the port:
  - `maps[EFUSE_ANA8M]` holds the `rtl_var_map` enum index 18, not `ANA8M` (sw.c:182); the SYS_CLKR check in `efuse_power_switch` tests mask 0x12.
  - `rtlhal.hw_type` is `HARDWARE_TYPE_RTL8192E` (0) during probe; `rtl92cu_hw_init` sets `HARDWARE_TYPE_RTL8192CU` (hw.c:884). `efuse_power_switch` branches on it.
  - `IS_92C_SERIAL` is false for RF-type bits 0b11, so a 92C 1T2R chip (version 0x13) gets `rf_type = RF_1T1R` (mac.c:97, fw_common.h:31-35).
  - `_out_ep_mapping` passes `ischipn` as `bwificfg` to `threeoutepmapping` (trx.c:173), so a 3-OUT normal chip always takes the WMM mapping.
  - `rtl_get_hwinfo` returning 1 (autoload fail, EEPROM boot, bad EEPROM ID) skips the TX-power and board-type parse entirely (hw.c:334).
  - `cfg->ops` calls go through `RtlHalOps` (`sw.rtl8192cu_hal_ops`), which also breaks the phy.c ↔ phy_common.c ↔ rf.c import cycle; `rf.py` and `phy.py` import each other as modules.
  - `rtl92c_phy_set_bb_reg` and `rtl92cu_phy_set_rf_reg` fold `data` into the mask unmasked (phy_common.c:44, phy.c:61): IQK's `tx0_a` and LCK's `lc_cal | 0x08000` spill past their masks, and the port keeps that.
  - `set_bw_mode` in `_rtl92cu_init_mac` is a no-op: the HAL is still `_HAL_STATE_STOP` until `rtl_usb_start` calls `set_hal_start` after hw_init.
  - `_rtl92c_phy_set_rfpath_switch` writes `REG_LEDCFG0` BIT(23) and 0x878 BIT(13) only while the HAL is stopped, i.e. during hw_init.
  - `rtl92c_dm_init_dynamic_txpower` tests `rtlhal.board_type`, which rtl8192cu never sets; high-PA boards still skip `dm_savepowerindex`.
  - `rtl92c_dm_check_txpower_tracking` is a no-op on the first hw_init (tracking is enabled by `rtl92c_dm_init`, which runs last); a second hw_init on the same `RtlPriv` triggers the thermal meter, a third runs the tracking callback.
  - `txpower_track_control` and `cck_inch14` are never set by rtlwifi, so the tracking callback only reads the thermal meter, swing indices and re-runs LCK/IQK on drift.
  - The path-A IQK retry loop re-reads 0xe9c on every pass that does not break (phy_common.c:1266, a misplaced brace in the source); the port keeps the read.
  - `RADIOB_1T` has one element; its loop would read past the end, but no chip reaches it (1T tables imply `rf_type` 1T1R, so path B is never configured).
  - mac80211 calls in monitor mode: `start` → `configure_filter` → `config(CHANGE_CHANNEL)`; no `add_interface` (rtlwifi sets no `NL80211_EXT_FEATURE_ACTIVE_MONITOR`, and `rtl_op_add_interface` rejects MONITOR). `rtl_ips_nic_on/off` do nothing on USB (`inactiveps` cleared, usb.c:318).
  - `rtl_op_start` fires the watchdog timer once straight away (core.c:141); the driver runs the first tick right after bring-up, then every `RTL_WATCH_DOG_TIME`, under the same lock as tunes.
  - The watchdog opens with the `REG_RCR` read of `HW_VAR_FWLPS_RF_ON` (hw.c:1416); nothing else reads `REG_RCR`.
  - `rtlphy->mcs_offset` (read by rf.c's TX power) is never filled for rtl8192cu, unlike `mcs_txpwrlevel_origoffset`; its terms add 0.
  - `rtl92c_phy_sw_chnl_callback` breaks out after the first step with a delay (the RF_CHNLBW write, 10 ms), so `postcommoncmd` never runs.
  - `struct dig_t` has both `cur_sta_cstate` (set by `rtl_dm_diginit`) and `cursta_cstate` (read by dm_common.c); `cur_igvalue` is a u32.
  - Unlinked, the watchdog's DIG, CCK-PD, RF-saving, dynamic TX power and EDCA-turbo steps write nothing; `rf_saving` reads 0x874/0xc70/0x85c/0xa74 once. `rtl92c_dm_refresh_rate_adaptive_mask` is commented out in `rtl92c_dm_watchdog`. `btcoexist.bt_coexistence` is never set for rtl8192cu, so BT coexist is off the graph. `rtl92cu_led_control` writes no register.
  - `REG_RXFLTMAP1` is cleared by hw_init ("Reject all control frame"), so ACKs never reach RX despite `RCR_ACF`; `_enable_rx_acks` sets the ACK subtype bit (`IEEE80211_STYPE_ACK >> 4`) through `HW_VAR_CTRL_FILTER`.
  - `enter_active_monitor` is `rtl_op_add_interface`'s STATION path: `set_network_type` (MSR infra, beacon functions off; unlinked, so `set_check_bssid(false)` keeps RCR's BSSID checks off), `HW_VAR_ETHER_ADDR`, `HW_VAR_RETRY_LIMIT`. `exit_active_monitor` is `rtl_op_remove_interface` (MSR no-link; `REG_MACID` keeps the address, as in the source).
  - Not ported (STA/AP only): the watchdog's linked traffic/LPS and roaming blocks, `_rtl92cu_set_media_status` ADHOC/AP, `HW_VAR_CORRECT_TSF`, `BASIC_RATE`, `BSSID`, `AID`, H2C join/power-mode variables.

## Known Problems
- RX is not ported: `connect()` starts no bulk-IN reader (`_rtl_usb_receive`), so no frames reach the RX callback.
- `close()` cancels the watchdog and releases the interface; `rtl_op_stop` / `rtl92cu_card_disable` are not ported.
- `rtl92cu_set_hw_reg` / `rtl92cu_get_hw_reg` raise `NotImplementedError` for the STA/AP variables listed above.
- `tests/chips/test_inject_frame.py` skips this package (`TX_NOT_PORTED`) until the TX path exists.

## Driver Entry Points
- Probe (`rtl_usb_probe`, usb.c:951) → `usb.rtl_usb_probe`
- Chip version (`rtl92c_read_chip_version`, mac.c:27) → `mac.rtl92c_read_chip_version`
- EFUSE read (`rtl_efuse_shadow_map_update`, `read_efuse`, efuse.c) → `efuse.py`
- EEPROM parse (`rtl92cu_read_eeprom_info`, hw.c:393) → `hw.rtl92cu_read_eeprom_info`
- Endpoint config (`rtl8192cu_endpoint_mapping`, trx.c:188; `_rtl_usb_init`, usb.c:236) → `trx.py`, `usb._rtl_usb_init`
- Firmware name + load (`rtl92cu_init_sw_vars`, sw.c:31; `rtl_fw_do_work`, core.c:72) → `sw.rtl92cu_init_sw_vars`, `core.rtl_fw_cb`
- hw_init (`rtl92cu_hw_init`, hw.c:863) → `hw.rtl92cu_hw_init`; MAC init helpers (rtl8192cu/mac.c) → `mac.py`
- Firmware download (`rtl92c_download_fw`, rtl8192c/fw_common.c:129) → `fw_common.py`; page/block writes (efuse.c:1299-1350) → `efuse.py`
- MAC/BB/RF tables (rtl8192cu/table.c) → `table.py`, selected in `hw._rtl92cu_phy_param_tab_init`; applied by `phy.py`, `rf.py`
- BB/RF register access, IQK, LCK, RF path switch (rtl8192c/phy_common.c) → `phy_common.py`; LCK body (`_rtl92cu_phy_lc_calibrate`) → `phy.py`
- DM init + thermal tracking (rtl8192c/dm_common.c; `rtl_dm_diginit`, core.c:1932) → `dm_common.py`, `core.py`
- Adapter start (`rtl_usb_start`, usb.c:681; `rtl_init_rx_config`, base.c:590) → `usb.rtl_usb_start`, `base.py`
- mac80211 ops (`rtl_op_configure_filter`, `rtl_op_config`, `rtl_op_add_interface`, `rtl_op_remove_interface`, core.c) → `core.py`; driven by `driver._bringup`, `set_channel`, `enter/exit_active_monitor`
- Channel switch (`rtl92c_phy_sw_chnl`, `rtl92c_phy_set_txpower_level`, phy_common.c:546-890) → `phy_common.py`; CCK/OFDM TX power (rtl8192cu/rf.c:36-357) → `rf.py`; channel access, network type, check-BSSID, get/set_hw_reg (rtl8192cu/hw.c) → `hw.py`
- Watchdog (`rtl_watchdog_wq_callback`, base.c:2036; `rtl92c_dm_watchdog`, dm_common.c:1416) → `base.py`, `dm_common.py`; scheduled by `driver._watchdog`
- CAM reset (cam.c:155) → `cam.py`
- Register I/O (`_usbctrl_vendorreq_sync`, usb.c:28) → `transport.RTL8192CUTransport`

## Capture phase map (capture-2, vendor ops)
- Frames before 54: USB-core enumeration (descriptors, SET_CONFIGURATION); no vendor requests. `verify_pcap` starts at the first vendor op.
- 0: `REG_SYS_CFG` read (chip version; 88C so no `REG_HPON_FSM` read).
- 1: `REG_9346CR` read.
- 2-1489: `efuse_read_all_map` (power switch on, `read_efuse`, power switch off).
- 1490-1491: `configvernoutep` reads `REG_NORMAL_SIE_EP+1`/`+2`.
- 1492: `rtl92cu_hw_init` starts (t=6.3 s; the 5.9 s gap is mac80211 registration until interface up, no register traffic).
- 1492-1536: `_rtl92cu_init_power_on`. 1537-2048: `rtl92c_init_llt_table`. 2049-2109: queue pages, TRX buffer, priority, interrupts, WMAC, EDCA, beacon params.
- 2110-2386: `rtl92c_download_fw`. 2387-2472: MAC table. 2473-2830: `rtl92cu_phy_bb_config` (PHY_REG, PG table has no wire ops, AGC). 2831-2991: `rtl92c_phy_rf_config` (RADIOA).
- 2992-3004: BB block on, CAM reset, security config, `REG_MACID` (zeros: no vif yet). 3005-3010: RF path switch. 3011-3176: IQK (3 passes, 1T). 3177-3192: LCK. 3193-3199: `_rtl92cu_hw_configure`. 3200-3210: `_initpabias` (EFUSE 0x1FA = 0xfe → path A bias). `rtl92c_dm_init` has no wire ops.
- From 3211 two kernel contexts write the wire: mac80211's (filter, tunes) and the watchdog work. Only tick 1 interleaves with the other context.
- 3211: tick 1 opener (`HW_VAR_FWLPS_RF_ON` reads `REG_RCR` = f0002a0e). 3212: `configure_filter` writes `REG_RCR` = f000790f (control + other-BSS + allmulti). 3213-3233: rest of tick 1: false-alarm counters 3213-3228, `rf_saving` first-run reads 3229-3232, `RF_T_METER` trigger 3233.
- 3234-3281: first `rtl_op_config`, ch 1: TX power 3234-3259, RF_CHNLBW 3260, 10 ms, channel access (slot, 4 AC params, SIFS) 3261-3271, `set_bw_mode` 3272-3281. 3282-3329: ch 10.
- 3330-3354: tick 2: thermal-meter read and tracking callback (thermal 0x0c sets the LCK/IQK baselines).
- 3355: second `configure_filter` (same RCR; mac80211 refilters when the capture tool comes up; replayed as an `FIF_ALLMULTI` change).
- 3356-4646: hops, each two tunes (mac80211 goes back to ch 1 first): 1,1 then 1 + {7, 13, 2, 8, 3, 9, 4, 10, 5, 11, 6, 12}, ~0.27 s apart. 4708-4803: 1,1.
- 4804-4844: tick 8: thermal 0x0e, `delta_lck` 2 → `rtl92c_phy_lc_calibrate` 4829-4844 (`REG_TXPAUSE` ff, RF 0x18 | 0x08000, 100 ms, `REG_TXPAUSE` 00).
- 4992-6272: 1 + N for N = 2..13, ~1 s apart.
- Watchdog ticks (22, every 2.016 s): 3211, 3330, 3644, 4238, 4647, 4665, 4690, 4804, 4845, 4863, 4888, 4906, 4931, 4949, 4974, 5088, 5305, 5515, 5732, 5942, 6159, 6273-6297. 18 ops trigger the thermal meter, 25 read it and run the tracking callback.

## Scripts
- `scripts/chips/rtl8192cu/verify_pcap.py`: replay-diff of the whole capture, ops 0-6297 (also `scripts/porting/verify_pcap.py rtl8192cu`). The mac80211 context and each watchdog tick replay in their own threads; a scheduler grants each capture op to the thread blocked on it and spawns a tick at each `REG_RCR` read.

## Debug log
