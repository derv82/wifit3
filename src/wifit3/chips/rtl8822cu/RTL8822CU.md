# RTL8822CU

## Captured Wireless Card

- D-Link AC13U, `2001:3329`, RTL8822C, 2.4 GHz and 5 GHz.
- Enumerates at USB2 in both usable captures, so the USB3 arm of the 0x0280 size and timeout write is not capture verified.
- Enumerates 3 bulk OUT endpoints.
- Not a B cut part: capture-1 reproduces byte identical with no 0x1018 (`REG_ANAPAR_MAC_0`) write.
- The C2H MAC hidden report gives max_tx_cnt 2 for this device; the EFUSE PG side gives 0, so the report is what raises it [firmware.py:read_mac_hidden_rpt, efuse.py:hal_rfpath_init].
- EFUSE 0xC9 is 0x4F, outside the accepted trx_path_bmp set [rtl8822c_ops.c:528-534], so `eeprom_trx_path_bmp` resolves to 0.
- EFUSE 0xC8 high nibble is 0, so the part runs TXPWR_PG_WITH_PWR_IDX and not TSSI.
- capture-1 sweeps 2.4 GHz ch1 to ch12; capture-2 adds ch13 and ch14, which is why its 5 GHz CCK reference differs from capture-1's.

## Linux Driver Source

- Out of tree DKMS vendor driver: `dkms.conf` gives PACKAGE_NAME realtek-rtl88x2cu and PACKAGE_VERSION 5.15.8-52~20230728. Not mainline.
- Upstream: https://github.com/libc0607/rtl88x2cu-20230728, branch `main` at commit 132c1e32b93c (2026-04-13, the branch head). Every `file:line` cite below resolves against that tree.
- DRIVERVERSION [include/rtw_version.h:1] is "v5.15.8.5-3-g88098843f.20240412_COEX20221215-3130". That is Realtek's own build version carried in the source; g88098843f is not a commit in the repo above.
- README.md:2 self describes as "RTL8812CU/RTL8822CU Linux Driver v5.15.8.5-3 20240412 FPV Mod".
- The 8822c files are authoritative for this chip; the sibling 8822b files are a different chip.
- Build configuration the port matches: `CONFIG_TXPWR_LIMIT_EN = n` [Makefile:102], `CONFIG_RTW_TX_NPATH_EN` compiled in [Makefile:22], `CONFIG_RTW_PATH_DIV` never defined.

## Python Port Details

- Claimed in `constants.USB_IDS_RTL8822CU`: 0BDA:C82C, 0BDA:C82E, 0BDA:C812, 0BDA:D820, 0BDA:D82B (Realtek demoboard defaults), 2357:0137 (FAST / TP-Link), 2001:3329 (D-Link AC13U).
- 13B1:0043 (Alpha) is commented out because rtl8822bu and rtl8822bu_dkms already declare it as the Linksys WUSB6400M.
- 2357:0137 is shared: `device/manager.py` `_VIDPID_FAMILIES` maps it to `VidPidFamily(default="mt76x2u", resolve=_resolve_2357_0137)`.
- `_resolve_2357_0137` reads the live device descriptor: any interface exposing bulk IN endpoint 0x85 routes to `mt76x2u`, otherwise to `rtl8822cu`; `resolve_driver` then pulls that package's `Claim`.
- `supported_ids()` skips a shared VID:PID for a package that is not the family default, so rtl8822cu never enters 2357:0137 into the static map.
- Scope is monitor mode at 20 MHz: RX, monitor bringup, channel tune, TX inject, watchdog tick. Managed / STA, 40 and 80 MHz, SDIO and PCIE, and the 2 or 4 bulkout layouts are out of scope.
- `SUPPORTED_CHANNELS` is 1 to 14 plus 36, 40, 44, 48, 149, 153, 157, 161, 165 [driver.py:73]. Channel 14 takes the vendor's CCK only shaping branch [phy.py:1180].
- The port was produced and checked by separate scouting, porting and reviewing agents against the vendor C.
- Related ports: `rtw88_base` supplies the transport, the power sequence runner and the RX descriptor parse.
- Hardware free unit tests: `test_driver`, `test_transport`, `test_tx`, `test_efuse_txpower`, and the TX power chain (`test_txpower_pg`, `test_txpwr_index`, `test_txpwr_tables`, `test_txagc`).

### Verify harness

- `scripts/chips/rtl8822cu/verify_pcap.py` replays the recorded cold boot pcap at the `ctrl_transfer` layer through `rtw88_pcap_replay.ReplayDevice` against one monotonic op cursor.
- Cold init runs the shipped `_bringup` plus `_monitor_entry`; the operational phase dispatches each interleaved burst (channel hop, phydm watchdog tick) to its real driver handler, keyed by a unique opener op.
- The recorded bulk IN stream is replayed as well: RX is pumped from the captured bulk IN FIFO past the frontier through `rx.iter_bulk_frames` (capture-1: 6882 frames, 3429 beacons).
- The first op that opens no handler is the frontier; exit 0 requires full reproduction with no frontier and no divergence.
- capture-1: 9571 bringup ops, 65 hops, 34 ticks, frontier op #25502 (frame 78447), a 90 byte bulk OUT TX inject inside `aireplay-ng --test`. TX inject is not dispatched in the operational walk.
- capture-2: 9341 bringup ops, 67 hops, 35 ticks, frontier op #25784 (frame 66967), the same 90 byte inject.
- capture-3 reaches no operational frontier: it errors during bringup at op #4721 (frame 9571) with `CFG_PARAM seq 3: no FW ACK within 500 ms`, exit code 2.
- Run: `.venv/Scripts/python.exe scripts/chips/rtl8822cu/verify_pcap.py capture-1`.

### EFUSE fields read (`efuse.py`)

- `rfe_type` (logical 0xCA, `EEPROM_RFE_OPTION`): RF front end type, selects the BB/RF table target and feeds `send_general_info`. `constants.RFE_TYPE` overrides it (default `None` reads from EFUSE).
- `tpt_mode` (0xC8[7:4], `EEPROM_TX_PWR_CALIBRATE_RATE_8822C`): read raw, then `txpwr_pg_mode` maps <=3 to PWR_IDX, <=7 to TSSI_OFFSET, else PWR_IDX with a log line. Selects the TX power path.
- `dis_dpd_rate` (derived from `txpwr_pg_mode`): PWR_IDX gives 0x3FF, otherwise 0x000; passed into `config_bb_rf` for phydm parameter init.
- `crystal_cap` (0xB9, `EEPROM_XTAL_B9_8822C`, default 0x3F): non combo boards use raw 0xB9, combo boards use the new policy over 0xB9 then 0x110/0x111 (default 0x40). Written by phydm during `config_bb_rf`.
- `bluetooth_coexist` (0xC1[7:5] == 1): selects which crystal cap policy runs.
- `eeprom_trx_path_bmp` (0xC9, `EEPROM_RF_ANTENNA_OPT_8822C`): [7:4] TX and [3:0] RX paths, accepted only in {0x33, 0x13, 0x23, 0x11, 0x22} else 0; narrows `hal_rfpath_init`'s trx_path_bmp.
- `eeprom_max_tx_cnt` (0xC1[2], `BIT_BOARD_OPTION_1TX`): a 1Tx board bounds max_tx_cnt to 1.
- `eeprom_regulatory` (0xC1[1:0]): regulatory domain, feeds `phy_is_tx_power_by_rate_needed`.
- `mac_address` (logical 0x157); `autoload_ok` (0x0A `REG_SYS_EEPROM_CTRL` bit5) and `map_valid` (logical[0:2] == 0x8129 `EEPROM_ID`) screen the fields above.
- `ant_num` and `hw_stype`, which also feed `hal_rfpath_init`, come from the C2H MAC hidden report (`firmware.read_mac_hidden_rpt`, bytes 4, 5 and 6 of one 13 byte read [hal_com.c:1378,1381,1383]), not from EFUSE.
- The PG bytes at 0x10..0x63 are walked separately by `txpower.hal_load_txpwr_info` into the 2G and 5G TX power info tables.

### Non-obvious in the port

- `driver._bringup` builds `self.txpwr` once as `txpwr_idx_state(...)` immediately after `read_mac_hidden_rpt`; nothing per tune recomputes it.
- TX power splits across `txpower.py` (PG walk), `txpwr_tables.py` (the compiled by rate array) and `txpwr_index.py` (index math); `phy._set_tx_power` is the only writer.
- `_set_tx_power` writes 0x18A0 / 0x41A0 (CCK, mask 0x007F0000), 0x18E8 / 0x41E8 (OFDM, mask 0x0001FC00) and twelve 0x3A00 diff dwords, bracketed by `_bbrstb_txagc_off`.
- `phy._fill_txagc_buff` runs the CCK section on 2.4 GHz only, so the 5 GHz CCK reference on the wire is the carried forward last 2.4 GHz CCK 11M index [rtl8822c_phy.c:662].
- `txpwr_tables.py` carries the vendor `array_mp_8822c_phy_reg_pg` [halhwimg8822c_bb.c:3639-3686], 46 rows by 6 columns; no Realtek part holds a power by rate region in EFUSE.
- `tx_pwr_by_rate[BAND_ON_5G][path][0x00..0x03]` is txgi_max because the vendor array has no 5 GHz CCK rows.
- The index is pg_txpwr_idx + (rate_target - rs_target) + rate_amends, clamped 0..127 [hal_com_phycfg.c:6295, :6341-6344]; the clamp reconciles the u8 buffer with the s8 diff table for the negative VHT MCS8/MCS9 offsets (DESC_RATE 0x34, 0x35, 0x3E, 0x3F).
- Every EFUSE diff nibble is valid (`IS_PG_TXPWR_DIFF_INVALID` is diff > 7 || diff < -8 [hal_com_phycfg.c:31]), so only bases fall through to a later source; a blank PG region yields bases 0x33 and diffs -2.
- No regulatory limit is applied, matching the vendor build [Makefile:102, os_intfs.c:758,1388]; each function that would consume a limit keeps the parameter as the plug point.
- `txpower.hal_load_txpwr_info` returns `None` outside PWR_IDX; that `None` is a screen, not an error, and `txpwr_idx_state` propagates it.
- With a `None` txpwr state, `_set_tx_power` logs once at ERROR naming EFUSE 0xC8's tpt_mode and returns without a register write.
- Bring up does not raise on EFUSE contents (policy EFUSE-1): sites log at ERROR and continue. `efuse.rfe_type` is the exception, see Known Problems.
- An unknown tpt_mode nibble (> 7, such as an unburned 0xFF) resolves to PWR_IDX, matching the vendor's zeroed post abort hal_data state [hal_com_phycfg.h:36-38, hal_intf.c:147-148].
- A blank 0xC1 on a valid map yields eeprom_max_tx_cnt 1: `Hal_EfuseParseBoardType` tests BIT2 without the 0xFF screen its InterfaceSel branch uses [rtl8822c_ops.c:296-305].
- `efuse.hal_rfpath_init`'s two probe abort fallbacks return the state before the EFUSE narrowing, keeping the C2H MAC hidden report's own edit.
- Path B's 5G BW80 and BW160 3S/4S bytes at PG offsets 0x62-0x63 fall past the 82 byte IC default segment and read 0xFF through `map_read8`'s init_value fill.
- `build_tx_desc_inject` emits DATARATE = DESC_RATE1M on both bands and RTS_DATA_RTY_LMT = 6; `band_is_2g` is threaded in and unused [tx.py, rtl8822cu_xmit.c:122-139].
- `build_tx_desc_mgmt` is band aware and emits DESC_RATE6M on 5 GHz, so the mgmt and inject descriptors disagree on the 5 GHz rate.
- `tx.pick_bulk_out_ep`'s MGMT to HIGH mapping holds only because the supported devices enumerate 3 bulk OUT; a 4 bulkout part needs `pq_map[MG] = EXTRA`.
- `_fill_txdesc_checksum48`'s fixed 24 halfword length holds only because the inject `pkt_offset` is 0.
- The five bundled `assets/*.bin` PHY tables are the vendor u32 arrays byte identical, iterated live through `selected_writes` in the C's order (BB, AGC, cal_init, RF-A, RF-B).
- 337 CFG_PARAM records per page is a derived fill threshold (4096 byte buffer, 48 byte txdesc, 12 byte record), not a capture constant [`phy._fw_offload_flush`].
- `phy.config_bb_rf` blocks on the CFG_PARAM C2H ACK, so any replay source without bulk IN completions stalls there.
- Bringup reads whose results the C itself discards must still be issued, in order, or the replay diverges.
- capture-1 never crosses the thermal LCK threshold (delta_LCK >= 8 [halphyrf_ce.c:983-998]); capture-2 crosses it at op #9777, so a watchdog change checked on capture-1 alone does not exercise that branch.
- `tests/chips/rtl8822cu/recorded_txagc.py` is the recorded oracle the TX power tests compare against, guarded by `ENTRY_COUNTS` and a sha256 `DIGEST` in `test_txagc.py::test_the_recorded_oracle_is_unedited`; deleting it removes those tests instead of failing them.

## Known Problems

- A TSSI mode part (EFUSE 0xC8 nibble 4..7) brings up and RXes with no TX power programmed: the TSSI branch is not ported, `txpwr_idx_state` returns `None` and `_set_tx_power` writes nothing, so the txagc table keeps power on defaults.
- The vendor computes a TSSI table on every tune via `halrf_get_tssi_codeword_for_txindex()` [hal_com_phycfg.c:6321-6326, rtl8822c_phy.c:675], so that divergence recurs per tune.
- Whether any 8822CU ships in TSSI mode is unknown; nobody has looked.
- `efuse.rfe_type` raises `RuntimeError` when EFUSE 0xCA reads 0xFF on a valid map, so an unburned EFUSE fails bring up there against policy EFUSE-1. Setting `constants.RFE_TYPE` is the escape hatch.
- capture-3 is unusable as a verification target: its usbmon file is truncated at 9590 packets (1.35 MB against capture-1's 14.3 MB) while its main.log shows the full 52 phase script ran to teardown.
- capture-3 holds 1 bulk IN completion against capture-1's 24628 and capture-2's 15175, so the CFG_PARAM C2H ACK the BB/RF offload interlock waits on is absent from the file.
- TX inject is the verify_pcap frontier on both usable captures, so no inject descriptor byte is checked against recorded traffic.
- The only recorded inject is on 2.4 GHz. INJ-1 is open: keep the vendor's unconditional DESC_RATE1M on a 5 GHz inject [rtl8822cu_xmit.c:139, core/rtw_xmit.c:4876], or emit DESC_RATE6M because wifit3 cannot supply a radiotap rate. `band_is_2g` is already threaded into `build_tx_desc_inject`.
- `Index5G_BW80_Base` is untestable against a BW20 only capture, so BW80 TX power is left alone.
- `rx._phy_rssi` clamps with `min(0, pwdb - 110)`, so any PWDB byte of 110 or more reports exactly 0 dBm.
- `rx._phy_rssi` decodes PHY status pages 0 and 1 only and returns `None` for any other page; the module docstring states it follows the 2T rtw88 layout used by the 8822B receiver, while the 8822C is a Jaguar3 part in the vendor phydm.
- `scripts/porting/rtw88_pcap_replay.py` has no waiver or skip mechanism: `ReplayDevice._next` raises the stored `Divergence` again on every later call, so a diverged device stays unusable.
- The registered known divergences list (libusb versus mac80211) is empty, so nothing has needed a waiver yet (INFRA-1).
- Verification rests on two captures from a single device (D-Link AC13U, 2001:3329), so the absence of a problem from this document is not evidence that none exists.

## Driver Entry Points

- Bring up: `driver.RTL8822CUDriver._bringup` (two cycles), calling `mac.mac_power_on` and HALMAC `card_enable_flow_8822c` in `power_seq.py`.
- Monitor entry: `driver._monitor_entry` with `mac.arm_monitor` (opmode set, TRX/EDCA/protocol/WMAC/USB config, MAC address program).
- Chip identity: `chipid.py` reads SYS_CFG1/CFG2/STATUS1 and WL_BT_PWR_CTRL into `ChipInfo` (chip_id, cut, rf_2t2r, rom_version) without changing device state.
- Transport: `transport.py`, vendor bRequest 0x05 control read/write over `Rtw88Transport`, the ON section 0x4E0 echo write, and bulk endpoint discovery (`endpoints`).
- Firmware upload: `firmware.download_firmware` (image from `firmware.load_firmware`), with H2C fill/send and `send_general_info` alongside.
- EFUSE read: `efuse.read_efuse` (HALMAC `dump_efuse_map_88xx` to `read_physical_map` to `decode_logical_map`).
- BB/AGC/RF table load and TRX path config: `phy.config_bb_rf`.
- Channel tune: `phy.switch_channel` (vendor `config_phydm_switch_channel_8822c` plus `switch_bandwidth`), with the per hop txagc flush in `phy._set_tx_power`.
- TX power: `txpwr_index.hal_com_get_txpwr_idx`, state from `txpwr_index.txpwr_idx_state`, PG walk in `txpower.hal_load_txpwr_info`.
- Cold boot calibrations: `cal.py` (`odm_dm_init` / `halrf_init`: DAC cal/DACK, RX DCK, X2K, thermal), then the phydm sub inits in `dm.py` (DIG, CCK-PD, env monitor, adaptivity, RA).
- Per board RF trim: `kfree.py` (thermal, power, PA bias, TSSI trims); TSSI DC offset calibration `tssi.py`; TX gap K `txgapk.py`.
- Watchdog tick: `watchdog.py`, the phydm 2 s JGR3 dynamic check (env monitor result/set, false alarm stats, DIG, CCK-PD, thermal).
- RX decode: `rx.iter_bulk_frames` (`rtw88_base.parse_rx_pkt_desc` plus the 8822C page 0/1 PHY status `_phy_rssi`).
- TX inject: `tx.build_tx_desc_inject` (vendor `dump_mgntframe` inject branch, `pattrib->inject == 0xa5`).

## Scripts

- `scripts/chips/rtl8822cu/verify_pcap.py`: the pcap replay described above, driving `scripts/porting/rtw88_pcap_replay.py` (`ReplayDevice`, `Divergence`).
- `scripts/chips/rtl8822cu/extract_phy_reg_pg.py`: regex parses `array_mp_8822c_phy_reg_pg` out of `halhwimg8822c_bb.c` and emits the literal embedded in `txpwr_tables.py`.
- `scripts/chips/rtl8822cu/verify_embed.py`: re parses the same C array and compares it element by element against the embedded tuple. Prints `identical: True` and sum `0x3b1dc83e37`.

All three need a local copy of the vendor driver source or a capture, neither of which ships with the repo.

## Debug log

(empty)
