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
- Status: probe and `rtl92cu_hw_init` ported (power-on, LLT, queue/MAC init, firmware download, MAC/BB/AGC/RF tables for every variant, CAM reset, RF path switch, IQK, LCK, PA bias, `rtl92c_dm_init`, thermal-meter TX power tracking). `verify_pcap` replays probe + hw_init, ops 0-3210, 3211/3211 byte-for-byte. `connect()` runs both, then stops with `BringUpError` at `rtl_usb_start`'s next step.
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

## Known Problems
- `connect()` cannot bring the card up: monitor/RX config after hw_init, channel tune, the DM watchdog, RX and TX are not ported.
- `rtl92cu_set_hw_reg` handles only the variables hw_init sets (ETHER_ADDR, RCR, WPA_CONFIG, MGT/CTRL/DATA_FILTER); any other raises `NotImplementedError`.
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
- 3211: first read of `REG_RCR` (0x0608), the first op after hw_init (milestone 3 starts here); 22 such reads, ~2 s apart from 3330 on.

## Scripts
- `scripts/chips/rtl8192cu/verify_pcap.py`: replay-diff of probe + hw_init, ops 0-3210 (also `scripts/porting/verify_pcap.py rtl8192cu`).

## Debug log
