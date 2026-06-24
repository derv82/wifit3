# RTL8821CU (8821cu_dkms) — port reference

> Self-contained vendor/DKMS cleanroom port (no shared base — anti-DRY). Source of truth is
> the vendor tree in the bundle, **not** mainline rtw88. Citations are against
> `usb_dumps_new2/captures_rtl8821cu/driver-source/` (vendor `rtl8821cu-5.12.0.4`) and the
> cold-boot pcap `usb_dumps_new2/captures_rtl8821cu/capture-1.pcap`.

> **Status.** The byte-for-byte gate (`scripts/rtl8821cu_dkms/verify_pcap.py`) reproduces the entire
> cold-boot pcap — all 21409 ctrl + bulk-OUT ops, PASS — driving the driver's public interface
> (`connect` / `set_channel` / `inject_frame`), so it verifies the product path, not a parallel
> reimplementation. Registered in `wlan/manager.py`; cold init is HW-validated (FW boots); RSSI decode
> is fixed to the jgr2 format (reads sane on HW). **5 GHz monitor RX works on HW.** **2.4 GHz RX is
> gated on RF18 bit16** — the cold tune leaves it stuck SET (dead RX); `driver._relatch_2g_band` clears
> it warm (see Known issues + the RX bring-up debugging section). Open: in-app fresh-plug RX
> confirmation, the DIG-in-monitor beacon-rate sag, warm reattach, and the ZeroCD mode-switch (below).

> ## ⚠️ Bring-up blocker — ZeroCD / mode-switch (UNSOLVED, likely fleet-wide)
>
> This card enumerates as a **USB CD-ROM** (mass-storage "ZeroCD") and must be mode-switched to
> the Wi-Fi function (PID `0bda:c820`) before any driver can bind. A fresh user who plugs it in
> sees a CD-ROM, **not** a Wi-Fi card — Wifit3 currently finds nothing, so the card is unusable
> end-to-end until the discovery layer handles this. The cold-boot pcap was captured already in
> Wi-Fi mode (Linux `usb_modeswitch` flips it at plug-in via udev), so the **offline port + verify
> are unaffected** — this blocks the *product*, not the port.
>
> This is a **manager/discovery-architecture** problem, not a per-chip detail, and not unique to
> 8821cu (many Realtek USB adapters ZeroCD). Open design questions — auto-eject vs consent modal,
> per-plug behavior, and the Windows WinUSB re-bind after the switch — are under discussion; the
> canonical write-up belongs in `planning/` once decided. Tracking decisions here until then.

## Silicon

| | | |
|---|---|---|
| USB ID | `0bda:c820` | [WIRE] capture; [SRC] usb_intf.c:263 |
| Silicon | RTL8821C, 1T1R, 2.4 + 5 GHz 802.11ac | vendor tree name |
| MAC/PHY family | HALMAC + PHYDM (same infra as 8822b/8822c) | [SRC] hal/halmac/halmac_88xx/halmac_8821c/ |
| Firmware | yes — `array_mp_8821c_fw_nic[]` (~139 KB) | [SRC] hal/rtl8821c/hal8821c_fw.c (doors-map; confirm at FW milestone) |
| Chip ver / cut | `SYS_CFG1` (0xF0) = `0x00494537` on this card | [WIRE] f546 |

## Entry points (the doors → our `.py`)

| phase | our `.py` | vendor `.c` | note |
|---|---|---|---|
| USB probe / id_table | `driver.SUPPORTED_IDS` | os_dep/linux/usb_intf.c:142 (VID), :263 (`0xC820`, `.driver_info = RTL8821C`) | verified VID; id line from doors-map |
| register transport | `transport.Rtl8821cuTransport` | include/usb_ops.h:19-22,30 ; os_dep/linux/usb_ops_linux.c:26-260 | bRequest 0x05, 0xC0/0x40 |
| ON-section mirror | `transport._mirror` | os_dep/linux/usb_ops_linux.c:171-201 (`t_reg = 0x4e0` :191) | gated on `CONFIG_RTL8821C` |
| mount chip-detect | `chipid.mount_get_chip_info` | hal/halmac/halmac_api.c:492 get_chip_info (USB :518-520) | SYS_CFG2 0xFC + SYS_CFG1+1 0xF1 — **VERIFIED** |
| chip-version read | `chipid.read_chip_version` | hal/rtl8821c/rtl8821c_ops.c:34 | SYS_CFG1/STATUS1/0x68 — **VERIFIED** |
| EFUSE dump + decode | `efuse.read_efuse` / `read_hw_efuse` / `eeprom_parser` | rtl8821c_ops.c:462 + halmac_efuse_88xx.c:1088 (dump), :1198 (decode) | 512 B via the 0x30 loop, packed→logical decode, BT-coex 0x68 read — **VERIFIED** |
| pre-power init | `init.pre_init_system_cfg` / `_enable_bb_rf` | `pre_init_system_cfg_8821c` halmac_init_8821c.c:975 ; `enable_bb_rf_88xx` halmac_cfg_wmac_88xx.c:637 | RSV_CTRL / PAD_CTRL1 / LED_CFG / GPIO_MUXCFG rmw + BB/RF disable + test-mode probe — **VERIFIED** |
| power on/off | `pwrseq.mac_pwr_switch` + `pwrseq` (CARD_EN_FLOW) | `mac_pwr_switch_usb_8821c` halmac_usb_8821c.c:31 ; halmac_pwr_seq_8821c.c:20-349 | state-sample preamble + 4 tables run 1:1 + status-clear/SW_MDIO tail — **VERIFIED** |
| pwr-seq runtime | `pwrseq.run_pwr_seq` / `_run_table` | hal/halmac/halmac_88xx/halmac_common_88xx.c:2980 / :3051 | WRITE=rmw, POLLING=read-until-match — **VERIFIED** |
| BT-coex power-on | `btc.power_on_setting` | `ex_halbtc8821c1ant_power_on_setting` halbtc8821c1ant.c:3838 | combo card: ant→BT, wifi-only coex tables — **VERIFIED** |
| chip-info readback | `bringup.read_mac_hidden_rpt` | `hal_read_mac_hidden_rpt` hal_com.c:1550 | power-on + FW dl + MAC cfg + general-info + 13-byte C2HEVT readback + power-off — **VERIFIED** |
| firmware download | `firmware.download_firmware` (+ `fw_dl`) | `download_firmware_88xx` halmac_fw_88xx.c:115 ; iDDMA `dlfw_to_mem` :567 | blob `assets/rtl8821cu_fw_nic.bin` byte-matched vs bulk-OUT — **VERIFIED** |
| MAC init | `mac.init_mac_flow` / `init_mac_cfg` | `init_mac_flow` hal_halmac.c:3452 ; `init_mac_cfg_8821c` halmac_init_8821c.c:382 | queue/page/H2C/protocol/EDCA/WMAC + RX-agg — **VERIFIED** |
| general info H2C | `firmware.send_general_info` | `_send_general_info` hal_halmac.c:3073 ; `send_general_info_88xx` halmac_fw_88xx.c:1046 | 2 H2C bulk pkts (QSEL=H2C txdesc) + h2cq dump-poll + HMEBOX reg send — **VERIFIED** |
| power off | `bringup.power_off` + `pwrseq` (CARD_DIS_FLOW) | `rtw_hal_power_off` hal_intf.c:475 ; `mac_pwr_switch_usb_8821c` OFF | btc scoreboard 0xAA + CARD_DIS_FLOW — **VERIFIED** |
| phydm trim + RFE | `efuse.read_phydm_trim` / `phy.init_hw_info_by_rfe` | `rtw_phydm_read_efuse` hal_dm.c:1832 ; `phydm_init_hw_info_by_rfe_type_8821c` phydm_hal_api8821c.c:328 | PPG kfree-trim bank reads + 0xCB4 DPDT default — **VERIFIED** |
| full TX descriptor | `tx.build_mgnt_txdesc` | `update_txdesc` rtl8821cu_xmit.c:35 (USB filler, via `dump_mgntframe`->`rtw_dump_xframe`) ; ra from `update_mgntframe_attrib_addr` hal_intf.c:885 | MGNT_FRAMETAG branch: LS/MACID/RAID/QSEL/HWSEQ/MBSSID/USE_RATE/DATARATE/RTY/SW_DEFINE/BMC + XOR cksum — **VERIFIED** (airmon FW dl) |
| MAC-reg table | `mac.init_mac_register` / `mac_reg_tbl` | `rtl8821c_init_phy_parameter_mac` rtl8821c_phy.c:97 -> `odm_config_mac_8821c` | 138 plain 1-byte writes, no cut/rfe conditionals — **VERIFIED** |
| RX drv-info cfg | `mac.config_rx_info` | `cfg_drv_info_8821c` halmac_cfg_wmac_8821c.c (PHY_STATUS) | DRVINFO_SZ=4 + APP_PHYSTS on + RCR sync — **VERIFIED** |
| BB enable + PHY_REG | `bb.init_bb_rf` / `bb.phy_bb_config` / `phy_cond` | `init_bb_rf` + `_init_phy_parameter_bb` rtl8821c_phy.c:57/113 ; `odm_config_bb_phy_8821c` phydm_regconfig8821c.c:171 | BB/RF enable + PRE-setting + 1678-row PHY_REG table (cut/rfe walker) — **VERIFIED** |
| AGC + RF radio-A | `bb.phy_agc_config` / `rf.config_radioa` | `_init_phy_parameter_bb` :172 (AGC + BTG diff) ; `_init_phy_parameter_rf` :207 (RF radio-A via LSSI 0x0C90) | AGC 1600 + BTG-diff 390 + crystal_cap + RF radio-A 2712 + POST — **VERIFIED** |
| USB interface cfg | `mac.init_interface_cfg` | `init_usb_cfg_88xx` halmac_usb_88xx.c:39 | RXDMA burst mode/size + TXDMA drop-on-overflow — **VERIFIED** |
| monitor RX-filter | `mac.hal_init_misc` | `rtl8821c_hal_init_misc` rtl8821c_halinit.c:203 | CAM clear + RXFLTMAP all-mgmt/data + RCR + mgmt-ack + MAC-sec + RX-TSF filter — **VERIFIED** |
| monitor entry (airmon) | `bringup.hal_init` | `rtl8821c_hal_init` halinit.c:264 | `_halmac_init_hal` + `hal_init_misc` (RX-enabled) **VERIFIED** |
| phydm DM init | `dm.phy_init_haldm` | `rtl8821c_phy_init_haldm` rtl8821c_dm.c:174 -> `rtw_phydm_init` hal_dm.c:1594 -> `odm_dm_init` phydm.c:1786 | the whole compiled `odm_dm_init`: common-info/dig/cck-pd/env-monitor/adaptivity/ra-info/cfo-track/rf-init/**dc-cancellation**/la-init/psd-init — **VERIFIED** |
| beamforming init | `mac.phy_bf_init` | `rtl8821c_phy_bf_init` rtl8821c_phy.c | MU-MIMO/TXBF defaults (0x14c0/0x167c/0x1680/0x42f/0x45f/0x6df/0x1c94) — **VERIFIED** |
| BT-coex HAL init | `btc.hal_init` | `halbtc8821c1ant_init_hw_config` halbtc8821c1ant.c:3739 (via `rtw_btcoex_HAL_Initialize`) | combo-card 1-ant init: PTA/3-wire enable + ltecoex 0x1700 GNT + ant-to-BT + coex table + tdma/query H2Cs — **VERIFIED**. `init_coex_dm` is empty |
| H2C-by-reg (HMEBOX) | `firmware.send_h2c_by_reg` | `rtw_halmac_send_h2c` hal_halmac.c:4103 | box index `t.last_hme_box` rotates mod 4, reset to 0 after each FW dl — **VERIFIED** |
| WL activity LED | `led.cfg_wl_led` | `rtw_halmac_led_cfg(TRUE,3)` hal_halmac.c:5094 (USB `hal_init_misc` rtl8821cu_halinit.c:41) | pinmux GPIO8->WL_LED (0x4a/0x4e[5]) + SW-control mode (0x4e=0x28) — **VERIFIED** |
| SW-LED blink (async) | `led.led_blink` / `LedBlinkState` | `SwLedBlink1` hal/led/hal_usb_led.c:112 (BlinkTimer) | no-link `LED_BLINK_SLOWLY` tick: alternate 0x4e[3] (active-low) via `pinmux_wl_led_sw_ctrl` — async producer #2 — **VERIFIED** (2 ticks) |
| phydm watchdog (async) | `watchdog.tick` / `WatchdogState` | `rtw_dynamic_chk_wk_hdl` rtw_cmd.c:2992 -> `phydm_watchdog` phydm.c:2382 | dynamic-check tick (async producer #3): sreset + USB rx-agg + FA-counters + DIG + CCK-PD + adaptivity + halrf-thermal (2-phase arm/callback) + dyn-bw + env-monitor NHM/CLM/FAHM. `rtw_phydm_set_rrsr` (0x440) is first-tick-only — **VERIFIED** (tick1 arm + tick2 CB0) |
| halrf thermal track | `watchdog._halrf_thermal` / `_halrf_thermal_callback` | `odm_txpowertracking_check_ce` halrf_powertracking_ce.c:818 ; `..._callback_thermal_meter` halphyrf_ce.c:409 ; `set_pwr8821c` halrf_8821c.c:123 | ARM (odd: RF 0x42[17:16]=3) / CALLBACK (even: meter avg vs `eeprom_thermal`+kfree-trim -> 2ga/5ga delta-swing table -> 0xc94[6:1] OFDM-AGC + 0xc1c BB-swing) — **VERIFIED** (CB0, 2G). 5G callbacks need band-aware table + the band-switch `odm_clear_txpowertracking_state` reset (see Known issues) |
| BT-coex periodical (async) | `btc.periodical` / `PeriodicalState` | `hal_btcoex_Hanlder` hal_btcoex.c:6069 -> `ex_halbtc8821c1ant_periodical` halbtc8821c1ant.c:5411 | BT-coex periodical (async producer #4): monitor_bt_ctr (0x770/0x774/0x76e) + monitor_wifi_ctr (silent) + update_wifi_link_info + read_scbd; first-tick-only run_coex (action_wifi_not_connected) + the post-periodical BT-FW-version query (BT_MP_OPER 0x67) — **VERIFIED** (1 periodical) |
| iface MAC addr | `mac.set_mac_addr` / `efuse.mac_address` | `rtw_hal_iface_init` hal_intf.c:521 -> `cfg_mac_addr_88xx` | REG_MACID 0x0610/4 + 0x0614/2 from EFUSE 0x107 (per-card, never hardcoded) — **VERIFIED** |
| iface port-enable / RX-BAR | `mac.hw_port_enable` / `mac.enable_rx_bar` | `hw_var_hw_port_cfg` / `init_hw_mlme_ext` rtw_mlme_ext.c:1279 | BCN_CTRL 0x0550 \|= 0x1c ; RXFLTMAP1 0x06a2 \|= BIT8 — **VERIFIED** |
| channel tune (RF/BB) | `chan.set_channel` / `_need_switch_band` | `rtl8821c_switch_chnl_and_set_bw` rtl8821c_phy.c:740 ; `need_switch_band` :477 | 2.4G ch1 + same-band hop ch10: band switch (coex notify + phydm band) only on band change, then switch_channel/switch_bandwidth (20 MHz) + kfree — **VERIFIED** |
| 5G band switch + tune | `chan._switch_band_5g` / `_switch_channel_5g` / `_csi_mask_disable` / `txpower` 5G | `config_phydm_switch_band_8821c` :756 ; `..._channel_8821c` :865 ; `phydm_csi_mask_setting` phydm_api.c:1190 | 2.4G->5G (ch36..64): `switchband_notify_5g`->`action_wifi_under5g` (set_ant_path PHASE_5G + table0, no tdma) + 5G band/channel (WLA rf-set, AGC idx, fc, csi-mask off) + 5G kfree (RF 0x55 gain) + 5G TXAGC (no CCK) — **VERIFIED** |
| coex run_coex | `btc.run_coex` / `btc.switchband_notify_2g` | `halbtc8821c1ant_run_coex` halbtc8821c1ant.c:3493 | band switch: update_wifi_link_info (limited_tx 4 backup reads) then early-return (run_time FALSE); media-connect (run_time TRUE): BTCQDDR + action_wifi_not_connected — **VERIFIED** |
| phydm stop-TRX | `dm.stop_ic_trx` | `phydm_stop_ic_trx` phydm_api.c:606 (11AC) | dbg-port BB-idle **poll** ((BIT17\|BIT3)==0, ≤100 reads) + TX pause + OFDM/CCK TRX off / restore; reused by DC-cancel + channel tune — **VERIFIED** |
| TX-power-by-rate | `txpower.set_tx_power_level` | `rtl8821c_set_tx_power_level` rtl8821c_phy.c:556 | 0x1d00-0x1d34 txagc = EFUSE PG base (by-rate/limit disabled in the DKMS build); BTG looks up RF_PATH_B — **VERIFIED** |
| monitor RX-enable | `mac.set_opmode_station` / `set_opmode_monitor` | `hw_var_set_opmode` rtl8821c_ops.c:1002 (STATION+MONITOR) | re-MAC + MSR + StopTxBeacon; promiscuous RCR 0x90000001 + cfg_drv_info(SNIFFER) + RXFLTMAP=0xffff — **VERIFIED** |
| media-connect coex | `btc.media_status_notify_connect_2g` / `run_coex` | `ex_halbtc8821c1ant_media_status_notify` halbtc8821c1ant.c:4851 (via setopmode_hdl rtw_mlme_ext.c:13575) | combo card: set_ant_path PHASE_2G (antenna BT->WiFi, GNT HW-PTA), CCK hi-pri 0x6cf[4], leap-AP H2C 0x69, run_coex(2GMEDIA) -> action_wifi_not_connected (coex table + PTA tdma) — **VERIFIED** |

## Hot paths

- `transport._mirror` (`transport.py`) — after every ON-section vendor access (addr ≤ 0xFF or
  0x1000–0x10FF), a 1-byte write to `0x4E0` of the IO-buffer low byte. Verified byte-for-byte
  against the wire. [SRC] usb_ops_linux.c:171-201.
- `pwrseq._run_table` (`pwrseq.py`) — HALMAC `pwr_sub_seq_parser`: WRITE = read-modify-write,
  POLLING = read-until-masked-match, DELAY/READ = no-op, the USB intf filter drops SDIO/PCI rows.

## Scripts

- `scripts/rtl8821cu_dkms/verify_pcap.py` — the byte-diff gate vs the cold-boot pcap
  (`usb_dumps_new2/captures_rtl8821cu/capture-1.pcap`, the cold-boot of the 4 captures). Run:
  `uv run python scripts/verify_pcap.py rtl8821cu_dkms`. **Single-cursor `Walk` + async-handler
  dispatch** (8814-style): the deterministic prefix is `bringup.cold_bringup`; the operational phase
  dispatches each interleaved async burst (channel hop / LED blink / phydm tick) to its real port
  handler by a unique opener op, advancing the cursor by what the handler consumes. A clean run prints
  the per-phase op counts and a `FRONTIER ->` line naming the next op to port. Do NOT edit the gate to
  pass and do NOT strip async ops — port the diverging op / register the producer as an async handler
  (PORTING.md Step 3).
- `scripts/rtl8821cu_dkms/dump_ops.py` — throwaway wire inspector (rebuilds the merged ctrl+bulk
  stream): `dump_ops.py <start> <end>` prints an op range (0x4e0 mirror filtered unless `--all`);
  `--led` tabulates the LED on/off sequence; `--rf18` tabulates the RF 0x18 channel-write (hop) order.
- `scripts/rtl8821cu_dkms/band_state_probe.py` — HW RX diagnostic: cold ch1 → 5 GHz → ch1, counting
  good/CRC-err/beacon-header frames per phase and diffing the band-state registers (RF18, 0xcb8, AGC
  idx, IGI, …). This is what isolated the RF18 bit16 gate. Passive (RX only).
- `scripts/rtl8821cu_dkms/driver_rx_diag.py` — drives the real `Rtl8821cuDkmsDriver.connect()` and
  reports RF18 bit16 + IGI + delivered-beacon rate over a dwell (optional `killwd`). Re-run after a
  fresh plug to confirm `_relatch_2g_band` revives 2.4 GHz RX in the shipped path.

## Caveats

- The 8821c power tables differ from 8822b's (no `0xFF0A/0xFF0B/0x0012` LDO rows, different PCI
  block, no cut-C `0x10A8` rows, ACT ends at `0x007C`). This is why the port is self-contained,
  not a reuse of `rtl8822bu_dkms`.
- Every 8821c card_en/dis row is `CUT_ALL`, so the chip cut does not filter the power sequence;
  the real cut (from `SYS_CFG1` 0xF0) only gates the init tables that follow.
- **ZeroCD / mode-switch:** see the ⚠️ bring-up-blocker callout at the top — it's a
  discovery-layer architecture problem, not a per-chip caveat.

## Known issues

- **2.4 GHz monitor RX — RF18 bit16 is the master gate (root cause, HW-confirmed).** bit16 (the 5 GHz
  band-select bit) = 0 ⇒ the 2.4 GHz demod works (~half of frames pass CRC, hundreds of good
  beacons/s); = 1 ⇒ 100% CRC-fail, 0 good beacons. The cold first tune doesn't latch bit16=0 (it runs
  before RX-enable + the antenna switch); `config_phydm_switch_channel_8821c` never clears bit16
  ([SRC] phydm_hal_api8821c.c:840) so a same-band hop can't fix it. `driver._relatch_2g_band` (commit
  62dcf199) re-asserts it warm. **Open:** fresh-plug end-to-end confirmation (`driver_rx_diag.py`); the
  phydm-watchdog DIG cranks IGI 0x20→0x2a in monitor mode and may sag the beacon rate (secondary). See
  the RX bring-up debugging section.
- **RSSI decode — FIXED (was a Jaguar-1 borrow).** 8821C is `PHYSTS_2ND_TYPE_IC`; `rx.decode_rssi` now
  parses the jgr2_type0 (CCK) / type1 (OFDM) report dispatched on the page nibble — path-A `pwdb[0]`,
  the 8821C CCK LNA table, the real `cck_new_agc` latch (commit d9c93c16). [SRC] phydm_phystatus.c.
- **BT-coex HAL init (`init_hw_config`) is COMPLETE @ 7748.** `rtl8821c_hal_init` runs it after
  `phy_bf_init` because the combo card reports `EEPROMBluetoothCoexist`
  (`rtw_btcoex_HAL_Initialize(_FALSE)` -> `halbtc8821c1ant_init_hw_config(back_up, wifi_only=FALSE)`,
  [SRC] halbtc8821c1ant.c:3739). Wire order (all in `btc.hal_init`): kt_ver read 0x00f1; PTA/3-wire
  enables 0x0550/0x0790/0x0778/0x0040/0x0041/0x04c6/0x0763/0x06cf; `btc_set_rf_reg(RF_A,0x1,0x2,0x0)`;
  `set_ant_path(INIT)` = ltecoex-disable + WL/BT-vs-LTE tables + GNT_BT-hi/GNT_WL-lo via the **0x1700
  indirect protocol** (0x1703 ready-poll + 0x1700 addr-latch + 0x1704 wdata / 0x1708 rdata) + the
  ant-to-BT switch (0x004e/0x004f/0x0cb4/0x0cb7/0x0067); `write_scbd(ACTIVE|ONOFF)` -> 0x00aa=0x8003;
  `table(0)` -> 0x06c0-0x06cc (break/select = 0xf0ffffff/0x1b because `concurrent_rx_mode_on`=TRUE);
  `tdma(off,8)` + `query_bt_info` -> two H2Cs (0x60 / 0x61) through the **HMEBOX rotation**
  (`firmware.send_h2c_by_reg`, box1 then box2). `init_coex_var`/`enable_gnt_to_gpio`(dbg off) are
  wire-silent; `init_coex_dm` is an empty function. The HMEBOX box index (`t.last_hme_box`) advances
  mod 4 per send and resets to 0 after each FW dl — that is why both general-infos are box0.
- **The channel tune's RF/BB is GREEN @ 7930** (`chan.set_channel`, ch 1 / 20 MHz / 2.4 GHz). Order
  inside `rtl8821c_switch_chnl_and_set_bw` ([SRC] rtl8821c_phy.c:740), all ported: (1)
  `phy_switch_wireless_band` = `btc.switchband_notify_2g` -> `run_coex` (the 4 `limited_tx` backup
  reads then early-return, `run_time_state` FALSE) + `config_phydm_switch_band_8821c` (2.4G: RF 0x18,
  CCK gates, `switch_rf_set(BTG)`, RF 0xdf/0x64, RF 0x18 under stopped TRX) + `phy_set_bb_swing`
  (0xc1c=0x200); (2) `config_phydm_switch_channel_8821c` (AGC idx, clock-offset 0x860=0x96a, cached
  CCK-TX-filter 0xa24/0xa28/0xaac); (3) `phydm_config_kfree` -> `set_kfree_to_rf_8821c` (2G PPG gain,
  here 0); (4) `mac_switch_bandwidth` (halmac cfg_ch_bw 20 MHz) + `config_phydm_switch_bandwidth_8821c`
  (0x8ac BW word, RF 0x18 |= BIT11|BIT10, RX-DFIR, bw-fixed). **`default_rf_set` is BTG (0)** for this
  rfe-0x22 card — the band switch takes the BTG arm (0xa84=0xe / 0xa80=0xfc84). `phydm_rfe_8821c`,
  `ccapar_by_bw`, `ccapar_8821c` are all `#if 0` (silent); (5) `txpower.set_tx_power_level` writes the
  **0x1d00-0x1d34 TXAGC table** = the EFUSE PG base (`by_rate`/`limit` disabled in the DKMS build,
  CONFIG_TXPWR_*_EN=n; BTG looks up RF_PATH_B's PG @ 0x10+42). The radio is tuned and the first
  channel set is COMPLETE @ 7938. Then **`bringup.set_monitor_mode`** (`setopmode` STATION then
  MONITOR) opens the monitor RX path: promiscuous RCR 0x90000001, `cfg_drv_info(SNIFFER)`,
  RXFLTMAP0/1/2 = 0xffff. GREEN @ 7969.
- **The second BT-coex pass (the media-connect antenna switch to WiFi) is GREEN @ 8033**
  (`btc.media_status_notify_connect_2g` + `run_coex`). The trigger is **not** scan_notify (as the
  pre-port note guessed) but `ex_halbtc8821c1ant_media_status_notify(BTC_MEDIA_CONNECT)` ([SRC]
  halbtc8821c1ant.c:4851), fired by `setopmode_hdl` for a monitor vif ([SRC] rtw_mlme_ext.c:13575) —
  which is why there is no leading scoreboard write (its `write_scbd(ACTIVE|ONOFF)` is already set, a
  no-op). Order, all ported: (1) `set_ant_path(AUTO, FC, PHASE_2G)` ([SRC] :2678) — BT-cal-check
  (0x49c[0]/[1]), `coex_ctrl_owner(WLSIDE)` (0x73), the 0x1700 ltecoex `set_gnt_bt(HW_PTA)` /
  `set_gnt_wl(HW_PTA)` block (7974-7997, GNT field value 0x0), `set_ant_switch(BBSW, TO_WLG)` routing
  the DPDT to WiFi (0x4e/0x4f/0xcb4/0xcb7/0x67, 7998-8016), **`run_time_state` -> TRUE**; (2)
  `0x6cf[4]=1` CCK Tx/Rx hi-pri (not 11b); (3) the inline leap-AP-protection H2C `0x69 {0xc,0}`
  (box3 @ 8019-8021 — **not** `set_tdma_timer_base`, which would send `{0xb,..}` and here early-returns
  silent); (4) `run_coex(2GMEDIA)`: `update_wifi_link_info` (`limited_tx` 4 reads @ 8022-8025), then —
  now that `run_time_state` is TRUE — `set_ant_path(NM, PHASE_2G)` (early-return, no wire via the
  `cur_ant_pos_type` guard), `write_scbd(BTCQDDR)` -> 0x00aa=0x8403, and `action_wifi_not_connected`
  (`table(NM,0)` = a no-op read of 0x6c0/0x6c4 + `tdma(FC, off, 8)` = the PTA-control set_tdma 0x60
  box0 @ 8030-8032). The HMEBOX box chain validates end-to-end (media-connect H2C box3 -> tdma box0).
  **This + the RXFLTMAP=0xffff is the full RX-enable** (the cold HW test saw no beacons because the
  antenna was still parked at BT — this pass routes it to WiFi).
- **The operational phase runs via a single-cursor Walk + async-handler dispatch** (8814-style, [SRC]
  scripts/rtl8814au_dkms/verify_pcap.py). After the deterministic prefix (`cold_bringup`), the gate
  dispatches each interleaved async burst to its real port handler by a unique opener op: a channel
  hop (`chan.set_channel`, opener `IN 0x2860` = `read_rf 0x18`), an LED blink (`led.led_blink`, opener
  `IN 0x004e`), and the phydm dynamic-check tick (`watchdog.tick`, opener `IN 0x0210`). The SW-LED is
  **ported, not stripped** (PORTING.md Step 3) — `SwLedBlink1`'s no-link `LED_BLINK_SLOWLY` tick is a
  strict-alternation BlinkTimer producer.
  - **LED-double treatment — DECIDED (lead-approved 2026-06-23).** The occasional **no-change** LED
    write in the wire (first ~op 9975) is a traffic-driven `LED_CTL_TX/RX/site-survey` re-assert — a
    separate producer from the BlinkTimer, whose interleaving the time-less replay can't predict.
    Source-confirmed the LED is a **single cosmetic bit** `0x4e[3]` (`pinmux_wl_led_sw_ctrl_88xx`,
    active-low: ON clears bit3, OFF sets it) with no RX effect. So when the strict-alternation
    `led_blink` handler first diverges on a re-assert, the agreed fix is to **bypass the LED-bit
    value-check** for that op (advance the cursor, treat ON==OFF) — NOT to model the recorded RX
    stream (overkill for one cosmetic bit). This is the one sanctioned value-bypass: it is scoped to
    the `0x4e[3]` LED bit only; every other op stays strict. (Cleanest impl: when `led_blink` /the
    dispatch hits a `0x4e` op whose write value differs from the strict-alternation prediction by
    only bit3, accept it / write the recorded value. Keep the BlinkTimer alternation handler for the
    majority of LED ticks; the bypass covers only the re-assert doubles.)
- **The phydm watchdog tick (`watchdog.tick`) front is GREEN @ 8356** (members 1-14): sreset
  xmit/linked (0x210/0x288/0x1118) + the 8821CU USB rx-agg reconfig (`cfg_usb_rx_agg_88xx`:
  0x283/0x10c/0x280) + an interleaved RCR read (0x608) + `phydm_false_alarm_counter_statistics`
  (the 16 FA/CCA/CRC32 reads 0xfcc-0xf54 + 0x808 cck-enable; `phydm_get_dbg_port_info` dbg ports
  0x0 then 0x209; the FA-reset toggles 0x9a4[17]/0xa2c[15]/0xb58[0]; first-tick crc32-cnt2-rate
  0xb04=6M) + `phydm_dig` (silent: cur_ig_value=0x20, FA in the [2000,4000] hold band -> no change;
  IGI lives at **0xc50**, not 0x9a4) + `phydm_cck_pd_th` type2 (tick1 LV_1->LV_0: 0xa08 pd_th
  0x7->0x3, 0xaa8 cs_ratio 0x11->0xf; `set_cckpd_lv` reads cck_n_rx `0xa2c` (BIT18&&BIT22, one read
  on 1R) **before** its lv-unchanged early-return, so a same-level update still reads 0xa2c and
  writes nothing — that read was the tick6 frontier) + `phydm_adaptivity` EDCCA NORMAL
  (L2H=max(igi+8,48)=0x30, H2L=0x28 ->
  0x8a4) + interleaved `rtw_phydm_set_rrsr` (0x440=0x15d) + `halrf` thermal arm (tm_trigger 0->1: RF
  0x42[17:16]=3 -> 0x2908 read + 0x0c90 LSSI) + `phydm_dyn_bw_indication` (0x840 bw-fixed). Silent
  members (#if'd out / software / no-link): noisy-detection, ra-info, cfo-tracking, primary-cca,
  enhance-mntr, hwsetting_8821c (empty), receiver_blocking. Carried state lives in `WatchdogState`.
- **The env-monitor (`watchdog._env_mntr`) is GREEN @ 8392 — the whole watchdog tick is complete.**
  `phydm_env_mntr_watchdog`: NHM get/set, CLM get/set, NHM trigger, CLM trigger, then FAHM (get/set/
  trigger). At monitor idle with IGI steady (0x20) the threshold curves are suppressed (carried
  `nhm_igi`/`fahm_igi` already equal the live IGI) and CLM period is suppressed (init set 0xffff); the
  only real changes are the NHM period (0x990 -> 0xfffe) + FAHM period (0x1cf8 -> 0xfffe, first tick)
  and the trigger-bit chain on 0x994 (`...18 -> 1a NHM -> 1b CLM -> 3b FAHM-incl -> 3f FAHM-trig`).
  NHM/CLM are not-ready (one ready poll each, no report read); FAHM is ready (denom + 6 report dwords).
  Period/include writes are first-tick-only (carried in `WatchdogState`), so later ticks suppress them.
- **The BT-coex periodical (`btc.periodical`) is GREEN @ 9284** (1 periodical, op 8392-8412). The
  driver thread's `hal_btcoex_Hanlder` ([SRC] hal_btcoex.c:6069) runs `ex_halbtc8821c1ant_periodical`
  ([SRC] halbtc8821c1ant.c:5411) then a one-shot BT-FW-version query. Wire order: `monitor_bt_ctr`
  (read 0x770/0x774, reset 0x76e=0xc) + `monitor_wifi_ctr` (silent — its 0x69{0x8} H2C is gated on
  `cur_ps_tdma_on`, off in monitor) + `update_wifi_link_info` (the 4 `limited_tx` backup reads) +
  `monitor_bt_enable`->`read_scbd` (0x00aa). Then **first-tick-only** `run_coex(RSN_PERIODICAL)`
  (the trigger `moniter_wifibt_status` sees the monitor port-count go 0->1 once → action_wifi_not_
  connected: table(0) no-op read + tdma 0x60 box1) and the **once** BT-FW-version query
  (`halbtcoutsrc_GetBtPatchVer`->`_btmpoper_cmd(BT_OP_GET_BT_VERSION)` → H2C **BT_MP_OPER 0x67** box2,
  buf {(seq<<4)|0, 0}=0..0 → main box 0x00000067; the real C2H caches `bt_get_fw_ver` so later ticks
  skip it). The 38 capture periodicals confirm: only #1 runs run_coex + the query; #2+ are the
  prefix only. Carried state in `PeriodicalState`. Live TX stays the user's.
- **The halrf thermal 2-phase + rrsr-gate is GREEN @ 9941** (`watchdog._halrf_thermal`). `rtw_phydm_
  set_rrsr` (0x440) is **first-tick-only** (it is NOT a watchdog member — a one-shot RRSR/rate update
  that interleaved into tick1; gated on `WatchdogState.first_tick`). The thermal meter is a 2-phase
  toggle on `tm_trigger`: ODD ticks ARM (RF 0x42[17:16]=3 -> 0x2908 read + 0x0c90 LSSI), EVEN ticks
  CALLBACK (`odm_txpowertracking_callback_thermal_meter`): read the settled meter [15:10], average
  it (4-deep ring), and when the average moved re-derive the OFDM swing index from the 2.4G
  delta-swing table (`-2ga_n[delta]` at/below the PG base) and apply `0xc94[6:1]=idx&0x3f` +
  `0xc1c[31:21]` BB-swing. `eeprom_thermal` (EFUSE 0xBA = 30) + kfree thermal trim (EFUSE 0x1EF =
  +4) seed `WatchdogState` from the gate. Tick2/CB0: meter 21 -> avg 25, |25-30|=5, -2ga_n[5]=-1 ->
  0xc94=0x7e; bb-swing stays at default_ofdm_index (24) so 0xc1c is identity. **VERIFIED** (CB0).
- **RESOLVED — the "CB1 anomaly" is the 2.4G->5G band switch, NOT an interleaved producer.** The
  earlier impossibility proof assumed (a) the 2G delta-swing table and (b) `thermal_value` carried =
  25 — both wrong. CB1 (tick4, op ~10791) is the **first thermal callback AFTER the 2.4G->5G band
  switch** (ch12 @9890 -> ch36 @10043; CB1 is on ch52 = 5G). Two things the band switch does, both
  byte-confirmed against the wire (0x0c1c write @10044-10045):
  1. `rtl8821c_switch_chnl_and_set_bw` -> `phy_switch_wireless_band` -> `phy_set_bb_swing_8821c`
     ([SRC] rtl8821c_phy.c:682-695) calls **`odm_clear_txpowertracking_state`** ([SRC]
     halphyrf_ce.c:134), which **resets `cali_info->thermal_value = rf->eeprom_thermal` (=30)** (line
     166) and `delta_power_index[A]/_last/absolute_ofdm_swing_idx = 0`. So at CB1 the "last" thermal
     is 30, not 25.
  2. The callback now indexes the **5G** table (`get_delta_swing_table_8821c`: ch36-64 ->
     `delta_swing_table_idx_5ga_n[0]` = `{0,1,1,2,3,3,3,4,...}` [SRC] halhwimg8821c_rf.c:2785), not 2ga_n.
  CB1 then: avg=25 (ring [25,26] is NOT reset), outer-delta=|25-30|=5>0 (because last was reset to 30)
  -> d=5 -> `5ga_n[0][5]=3` -> idx **-3** -> `0xc94=0x7a`; offset = -3 - 0(reset last) != 0 -> applies.
  Exactly the wire. **DONE + gate-VERIFIED @ 10809:** the thermal callback is band-aware
  (`watchdog._delta_swing_tables`, 2ga / 5ga[sub-band] by `t.current_channel`), and the band switch
  flags `t.thermal_reset_pending` (`chan.set_channel`) which the next callback applies
  (thermal_value=eeprom, ofdm_swing_idx=0). CB1 (`0xc94=0x7a`) reproduces exactly. No external/IQK
  writer (`do_iqk` needs `is_linked`, FALSE in monitor).
- **The op-9941 run_coex pass was the 2.4G->5G band switch — DONE + gate-VERIFIED @ 10809.** Not
  scan_notify: it is `set_channel(ch36)`'s `phy_switch_wireless_band` -> `switchband_notify_5g` ->
  `run_coex(5GSWITCHBAND)` -> **`action_wifi_under5g`** ([SRC] :3257): `set_ant_path(NM, PHASE_5G)`
  (no 0x49c poll / no ltecoex; GNT_BT=HW_PTA, GNT_WL=SW_HIGH -> 0x38=0x3303; `set_ant_switch(BBSW,
  TO_WLA)`) + `table(0)` (non-force read, no write) + `tdma(NM, off, 8)` (**no-op H2C** — already
  off/type-8). No scbd write (BTCQDDR already set). The run_coex's `update_wifi_link_info` runs first
  (the leading `limited_tx`), which is why `limited_tx` precedes set_ant_path. Dispatched as a
  band-switch hop (opener `IN 0x0430`). Then the full 5G tune (`_switch_band_5g` WLA / `_switch_
  channel_5g` AGC-idx+fc+csi-mask / 5G kfree RF 0x55 / 5G TXAGC no-CCK) reproduces ch36-64.
- `_drv_enable_trx` (between init_mac_flow and general-info) is RX/thread-side only — a gate no-op.
- **PHYDM discriminators are transformed, not the hal->* values** (the AGC walker forced this out):
  `dm->rfe_type = rfe_type_expand >> 3` (0x22 -> 4) and `dm->package_type = 1` for the 0x2x combo
  range ([SRC] phydm_hal_api8821c.c:336/349) — both differ from `hal->rfe_type`=0x22 /
  `hal->PackageType`=7 that the general-info H2C uses. `init_hw_info_by_rfe` now also sets
  `default_rf_set_8821c` (BTG for 0x22), which selects the BTG AGC-diff table ([SRC]
  phydm_hwconfig.c:1225 applies it after the main AGC table only for BTG cards).
- **DONE — the full TX-descriptor builder (`tx.build_mgnt_txdesc`).** The airmon FW download takes
  the full reserved-page descriptor: cold-init set `not_xmitframe_fw_dl=1` ([SRC] hal_com.c:1578)
  so its rsvd-page write took the minimal `usb_write_data_not_xmitframe` path; airmon leaves the
  flag 0 so the chunks go `usb_write_data_rsvd_page_normal` -> `dump_mgntframe` ->
  `rtw_dump_xframe` -> **`update_txdesc`** ([SRC] rtl8821cu_xmit.c:35 — the USB filler, NOT
  `fill_default_txdesc`; the recorded LS bit is the tell). Three non-obvious facts the gate
  forced out: (1) `BMC = IS_MCAST(addr1) = payload[4] & 1` because `rtw_hal_mgnt_xmit` runs
  `update_mgntframe_attrib_addr` ([SRC] hal_intf.c:885) to copy `ra`=addr1 from the frame first —
  true even for FW chunks, whose byte 4 lands in the addr1 slot. (2) The rsvd-page **restore**
  writes `txff_alloc.rsvd_boundary | BIT(15)` = `0x81cc` in airmon (0x8000 cold) — the boundary
  is set by the *cold* init_mac_flow and persists. (3) The phydm general-info H2C carries
  `PACKAGE_TYPE = hal->PackageType` (content byte 5) — 0 in cold (the MAC-hidden report that sets
  it is read *after* the cold general-info), `0x07` in airmon. The H2C descriptor itself is
  byte-identical in both modes (`update_txdesc_h2c_pkt` == the minimal H2C path). Same builder is
  what `inject_frame`/deauth TX will use.
- `transport` bulk-OUT EP defaults to `0x04`, but the coverage audit shows FW/TX bulk-OUT is on
  **ep `0x05`** — the offline gate's `ReplayDevice.write` ignores the endpoint so it passes, but
  real-HW FW download/TX must target `0x05`: set `Rtl8821cuTransport(bulk_out_ep=0x05)` (or probe)
  before HW testing. Interrupt-IN ep `0x81` (360 pkts) is still a C2H blind spot.
- **FW blob provenance (housekeeping):** `assets/rtl8821cu_fw_nic.bin` is byte-verified against the
  recorded bulk-OUT (so it matches what the kernel shipped), but still needs the linux-firmware
  cross-check + WHENCE provenance recorded per PORTING.md before release.

## RX bring-up debugging — 2026-06-23

The cold-boot pcap reproduces byte-for-byte (gate: 21409/21409), but on hardware 2.4 GHz
monitor RX was dead while 5 GHz worked. Root cause (HW, `scripts/rtl8821cu_dkms/band_state_probe.py`):
**RF18 bit16 — the 5 GHz band-select bit — is the master 2.4 GHz RX gate.** bit16=0 ⇒ the demod
works (~half of frames pass CRC, hundreds of good beacons/s); bit16=1 ⇒ 100% CRC-fail, 0 good
beacons (perfect correlation across every run). The cold first channel tune (inside `cold_bringup`,
before monitor RX-enable + the media-connect antenna switch to WiFi) does not latch bit16=0 — it
reads back stuck SET. `config_phydm_switch_channel_8821c` clears only BIT18/17/byte0, never bit16
([SRC] phydm_hal_api8821c.c:840), so a same-band hop can't fix it; only a band switch or an explicit
warm re-write does. The vendor stack hides this because airmon→airodump hops to 5 GHz immediately and
the first 5 GHz→2.4 GHz transition re-latches bit16 warm. Fix: `driver._relatch_2g_band` (commit
62dcf199) — a deterministic, read-back-verified warm RF18 bit16 clear after cold init, replacing the
flaky `_prime_2g_rx` 5 GHz-bounce. RSSI decode was a Jaguar-1 borrow on this Jaguar-2 chip, now the
jgr2 format (commit d9c93c16; HW reads sane −60..−84 dBm).

Not re-verified this session (the test card degraded after ~8 cold-boots with no replug — RF18 writes
began intermittently failing to land): in-app 2.4 GHz revival on a fresh plug (re-run
`driver_rx_diag.py` after replug — expect bit16=0 + beacons flowing); whether the phydm watchdog DIG
cranking IGI (0x20→0x2a observed in monitor mode) sags the beacon rate; reader-vs-init USB ordering.
The kernel's own over-air fixed-ch1 captures show only 13–21 beacons/15 s (mostly short control
frames), so this card's 2.4 GHz is genuinely marginal even under the vendor driver — judge against a
strong near reference AP, not the capture environment.
