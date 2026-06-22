# RTL8821CU (8821cu_dkms) — port reference

> Self-contained vendor/DKMS cleanroom port (no shared base — anti-DRY). Source of truth is
> the vendor tree in the bundle, **not** mainline rtw88. Citations are against
> `usb_dumps_new2/captures_rtl8821cu/driver-source/` (vendor `rtl8821cu-5.12.0.4`) and the
> cold-boot pcap `usb_dumps_new2/captures_rtl8821cu/capture-1.pcap`.

> **Status — cold-init probe GREEN + airmon re-init through general-info GREEN.** The
> byte-for-byte gate (`scripts/rtl8821cu_dkms/verify_pcap.py`, replaying ctrl + the FW/TX
> bulk-OUT stream) reproduces the **whole cold-boot probe and the first ~2977 ops of the airmon
> monitor-entry phase — 6348 ops, zero divergence**. Cold init (frames 1-7672): USB transport
> (+ the `0x4E0` mirror), chip-detect/version, EFUSE dump + decode + BT-coex read, pre-power
> init + card-enable + init_system_cfg, the BT-coex power-on setting, the **iDDMA firmware
> download** (the 138 KB blob byte-matched vs bulk-OUT), `init_mac_flow` (queue/page/H2C/
> protocol/EDCA/WMAC + RX-agg), `_send_general_info` (two H2C packets + h2cq dump-poll +
> HMEBOX), the MAC-hidden-report readback (now also parsing `PackageType`), `power_off`, and the
> phydm kfree-trim + RFE-type init. **Airmon `_halmac_init_hal`** (`bringup.hal_init`) then
> re-runs power-on + FW download (this time through the **full `update_txdesc`** reserved-page
> descriptor, `tx.py`) + init_mac_flow + general-info + **`init_mac_register`** (the 138-entry
> PHYDM MAC-reg table, `mac_reg_tbl.py`) + **`config_rx_info`** (DRV_INFO_PHY_STATUS) +
> `rtw_hal_init_phy` so far (BB/RF enable + PRE-setting + the **1678-row PHYDM BB PHY_REG table**
> via the cut/rfe conditional walker `phy_cond.py`), all byte-matched. Frontier is op #6348 (frame
> 13635), the **BB AGC table**. Not registered in `wlan/manager.py` (claims nothing until complete).

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
| monitor entry (airmon) | `bringup.hal_init` | `_halmac_init_hal` hal_halmac.c:3576 | + init_mac_register + config_rx_info + BB enable/PHY_REG **VERIFIED**; **(frontier)** BB AGC table -> RF radio-A -> POST-setting + calibration -> monitor RX-filter |

## Hot paths

- `transport._mirror` (`transport.py`) — after every ON-section vendor access (addr ≤ 0xFF or
  0x1000–0x10FF), a 1-byte write to `0x4E0` of the IO-buffer low byte. Verified byte-for-byte
  against the wire. [SRC] usb_ops_linux.c:171-201.
- `pwrseq._run_table` (`pwrseq.py`) — HALMAC `pwr_sub_seq_parser`: WRITE = read-modify-write,
  POLLING = read-until-masked-match, DELAY/READ = no-op, the USB intf filter drops SDIO/PCI rows.

## Scripts

- `scripts/rtl8821cu_dkms/verify_pcap.py` — the byte-diff gate vs the cold-boot pcap
  (`usb_dumps_new2/captures_rtl8821cu/capture-1.pcap`, the cold-boot of the 4 captures). Run:
  `uv run python scripts/verify_pcap.py rtl8821cu_dkms`. It drives `bringup.cold_bringup` against
  the recorded wire; a clean run prints `reproduced N/… ops clean` and a `FRONTIER ->` line naming
  the next op to port. Add ops-dump probes inline (see this session's frontier dumps) to read a
  byte range. Do NOT edit the gate to pass — port the diverging op (PORTING.md Step 3).

## Caveats

- The 8821c power tables differ from 8822b's (no `0xFF0A/0xFF0B/0x0012` LDO rows, different PCI
  block, no cut-C `0x10A8` rows, ACT ends at `0x007C`). This is why the port is self-contained,
  not a reuse of `rtl8822bu_dkms`.
- Every 8821c card_en/dis row is `CUT_ALL`, so the chip cut does not filter the power sequence;
  the real cut (from `SYS_CFG1` 0xF0) only gates the init tables that follow.
- **ZeroCD / mode-switch:** see the ⚠️ bring-up-blocker callout at the top — it's a
  discovery-layer architecture problem, not a per-chip caveat.

## Known issues

- **Frontier (next milestone): op #6348 (frame 13635): `OUT 0x081c/4=0xfb000003`** — the BB **AGC
  table** (`odm_config_bb_agc_8821c` [SRC] phydm_regconfig8821c.c:137, walked like PHY_REG;
  `odm_update_agc_big_jump_lmt` is software-only). After AGC: `rtw_phydm_set_crystal_cap` +
  `phy_set_bb_reg(rCCK0_FalseAlarmReport, BIT18|BIT22, 0)`, then **`init_rf_reg`** (the RF radio-A
  table via SIPI write_rf — needs the LSSI/3-wire RF write path, the one genuinely new mechanism
  left), the **POST-setting** (0x808 enable + 0xa24/0xa28/0xaac caches), then phydm **calibration**
  (IQK etc.), `halmac_init_interface_cfg`, the monitor-mode RX-filter/RCR setup, and the channel
  hops (airodump). `_drv_enable_trx` is RX/thread-side only — a gate no-op. The 8822bu_dkms sibling
  (`bb.py`/`rf.py`/`cal.py` + `*_tbl.py` + `phy_cond.py`) is the structural template; `phy_cond.py`
  + `bb.py` are now ported and reusable for AGC.
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

## Port log — 2026-06-22 (scaffold + M1 power tables)

- Built the self-contained dir: `transport.py` (+0x4E0 mirror), `pwrseq.py` (4 × 8821c tables
  verbatim), `bringup.py`, `driver.py` (WIP skeleton), `constants.py`. Added
  `scripts/rtl8821cu_dkms/verify_pcap.py` on the shared Realtek `rtw88_pcap_replay` relay and
  registered the chip in the top-level `scripts/verify_pcap.py`.
- Gate result: transport + `0x4E0` mirror reproduce the wire byte-for-byte (the recorded
  `IN <reg>` / `OUT 0x4e0=<lowbyte>` pairs match). Divergence at op #0 identified the real M1 as
  the chip-id/pre-init prologue above — power-on is M2. Doors-map (Explore subagent) had guessed
  power-on as first; the pcap corrected it (PORTING.md: the pcap is the map).
- Next session: port the chip-id/pre-init prologue, then re-run the gate — power tables should
  fall in behind it cleanly.

## Port log — 2026-06-22 (M1 chip-id + EFUSE prologue GREEN)

- Ported the prologue the gate had pointed at, all from source: `chipid.mount_get_chip_info`
  (halmac_api.c:492 get_chip_info — SYS_CFG2/SYS_CFG1+1), `chipid.read_chip_version`
  (rtl8821c_ops.c:34), and `efuse.read_efuse` (rtl8821c_ops.c:462 + halmac_efuse_88xx.c:1088
  read_hw_efuse — the 512-byte `0x30` indirect loop; bank-switch + cfg_ldo25 setup).
- Gate now reproduces **2068/20833 control ops with zero divergence** — the whole chip-id +
  EFUSE prologue is byte-for-byte. Discovery loop worked exactly as PORTING.md describes: each
  gate divergence named the next register block, traced to its C function, ported, re-ran.
- New frontier at op #2068: the pre-power-on init block (see Known issues), then `0x4A`
  power-on at op #2106. `power_on` removed from `cold_bringup` for now (not wire-adjacent to
  EFUSE); the power tables stay in `pwrseq`, ready to verify once the pre-power block lands.

## Port log — 2026-06-22 (EFUSE decode + parse, pre-power init, power-on GREEN)

- Op #2068 (`IN 0x68`) was **not** the head of pre-init as the prior doors-map note guessed —
  it is the tail of `rtl8821c_read_efuse`: `Hal_EfuseParseBTCoexistInfo` (rtl8821c_ops.c:134)
  reads `REG_WL_BT_PWR_CTRL` iff the EFUSE map is valid (logical[0:2]==0x8129) and carries a
  board option (logical[0xC1]!=0xFF). Reaching that condition required the packed→logical
  decode, so ported `eeprom_parser` (`eeprom_parser_88xx` halmac_efuse_88xx.c:1198); the BT-coex
  read is the only register touch in the whole parse chain (rest is pure map decode). `read_efuse`
  now returns the logical map. → 2070 ops.
- Then the two pre-power doors: `init.pre_init_system_cfg` (pre_init_system_cfg_8821c
  halmac_init_8821c.c:975) with its BB/RF-disable helper `_enable_bb_rf` (enable_bb_rf_88xx
  halmac_cfg_wmac_88xx.c:637), and `pwrseq.mac_pwr_switch` (mac_pwr_switch_usb_8821c
  halmac_usb_8821c.c:31) — the state-sample preamble (RPWM/MCUFW/CR/SYS_STATUS1+1) wrapping the
  already-transcribed `CARD_EN_FLOW`, plus the post-seq SYS_STATUS1+1 clear and SW_MDIO+3 probe.
  Untaken arms (USB SYS_CFG2+3==0x20, MCUFW==0xC078 FW-present) and the power-off branch ported
  behind their real checks. → **2170 ops, zero divergence.**
- New frontier op #2170 (`IN 0x1080`): the post-power-on MAC/DMA init (rtl8821c_halinit.c:264)
  and firmware download — next milestone.

## Port log — 2026-06-22 (init_system_cfg GREEN @ 2186; FW-download region mapped)

- Ported `init.init_system_cfg` (init_system_cfg_8821c halmac_init_8821c.c:721) — the third leg of
  `rtw_hal_power_on` after the power switch: CPU_DMEM_CON BIT16 platform reset, SYS_FUNC_EN+1 |= 0xD8,
  boot-from-flash disable. → **2186 ops, zero divergence.**
- Then reverse-engineered the whole #2186-~3360 region (see Known issues for the decoded detail). The
  surprise: the cold-boot **firmware download is not in `_halmac_init_hal`** — it is driven by
  `hal_read_mac_hidden_rpt` (hal_com.c:1550) called from `rtl8821c_read_efuse:525`, which powers the
  chip (→ btc power-on setting, the BT-coex block) then `rtw_hal_fw_dl`. Anchored empirically:
  pcap_slicer puts frame 5078 in `<hardware_plugin_and_initialization>`; `extract_bulk_out_ops` shows
  the FW blob on **bulk-OUT ep 0x05** (first packet frame 5346, 4144 B); `ltecoex_reg_read_88xx`
  (0x1700/0x1708) confirms `download_firmware_88xx` starts at op #2271; an Explore subagent + manual
  read of halbtc8821c1ant.c decoded every btc register write.
- **Stopped here (not a failure — a milestone boundary).** The next milestone (btc power-on setting +
  `hal_read_mac_hidden_rpt` orchestration + `download_firmware_88xx` + gate bulk-merge + 139 KB blob
  extraction/provenance) is large and multi-function; the full map is recorded in Known issues so it
  can be ported faithfully rather than rushed. `bulk_out_ep` must move 0x04 → 0x05 at that milestone.

## Port log — 2026-06-22 (BT-coex power-on, FW download, MAC init GREEN @ 3257)

- Drove the discovery loop autonomously through the hardest stretch of the cold path.
- **BT-coex power-on** (`btc.py`): the combo card reports BT, so `rtw_hal_power_on` runs
  `ex_halbtc8821c1ant_power_on_setting` at its tail. Decoded every register write (ant-switch
  BBSW→BT, PAPE/LNA, wifi-only coex tables) and computed the cb7 polarity from EFUSE `rfe_type`
  (`efuse.read_efuse` now returns `EfuseInfo`: bt_coexist / rfe_type / single_ant_path / ant_num).
  The block runs twice (two `rtw_hal_power_on` calls, the 2nd via `rtw_halmac_dlfw`; the chip
  power-on is idempotent through the `APFM_ON_MAC` software flag, so only the btc setting repeats).
  → 2218 ops.
- **Firmware download** (`firmware.py`): reached from `hal_read_mac_hidden_rpt` (the chip-info read
  powers the chip + pulls FW caps — the cold FW dl lives here, not `_halmac_init_hal`). Ported the
  full iDDMA path: ltecoex/MAC-reg backups, `start_dlfw`, per-4096-B-chunk rsvd-page bulk-OUT (48-B
  TX desc + chunk, XOR checksum) → iDDMA TXBUF→IMEM/DMEM/EMEM copy + rolling checksum, restore,
  `dlfw_end_flow` (FW-ready, CPU boot, poll 0x80==0xC078). The 138 KB `array_mp_8821c_fw_nic[]`
  blob was extracted to `assets/` and byte-matches the recorded bulk-OUT; the gate now merges the
  ctrl + bulk-OUT (ep 0x05) streams. → 3156 ops.
- **MAC init** (`mac.py`): `init_mac_flow` → `init_mac_cfg_8821c` (TX-DMA queue map, reserved-page
  boundary math, H2C queue, protocol/EDCA/WMAC blocks) + RCR sync + RTS-full-BW + USB RX-agg. → 3257 ops.
- Two intricate sub-protocols (iDDMA FW dl, btc power-on) were de-risked with Explore subagents that
  dumped the wire and traced the source; every value was then re-verified byte-for-byte by the gate.
- Frontier #3257 = `_send_general_info` (H2C). Remaining cold-init tail + the monitor phase are
  mapped in Known issues.

## Port log — 2026-06-22 (general-info + readback + power-off + phydm: cold-init COMPLETE @ 3371)

- Closed out the cold-init probe, all gate-verified byte-for-byte:
  `_send_general_info` (two H2C packets via the TX-desc builder with QSEL=H2C + the h2cq dump-poll
  + the HMEBOX by-reg copy), the `hal_read_mac_hidden_rpt` 13-byte readback, `power_off`
  (`hal_btcoex_PowerOffSetting` 0xAA + `mac_pwr_switch(OFF)` CARD_DIS_FLOW — FW now resident so the
  rpwm leave-32K toggle fires), `rtw_phydm_read_efuse` (7 PPG kfree-trim bank reads), and
  `phydm_init_hw_info_by_rfe_type_8821c` (the 0xCB4 DPDT default). → **3371 ops, zero divergence —
  the entire frames-1-7672 probe.**
- Began the airmon phase: a `hal_init` reusing power_on + download_fw + init_mac_flow reproduced to
  ~#3578, confirming the monitor-entry re-init reuses the cold path. It diverges at the FW-DL bulk
  TX descriptor because airmon takes the full xmit path (`not_xmitframe_fw_dl` clear ->
  `fill_default_txdesc`) instead of the minimal one. Reverted that scaffold to keep the gate green
  at the cold-init boundary; the TX-descriptor builder (see Known issues) is the next sub-milestone.

## Port log — 2026-06-22 (airmon re-init through general-info GREEN @ 4513)

- New `tx.py` ports the full USB reserved-page/management TX descriptor (`update_txdesc`
  rtl8821cu_xmit.c:35 MGNT_FRAMETAG branch), and `bringup.hal_init` ports `_halmac_init_hal`
  (power-on + FW dl + init_mac_flow + general-info). `firmware.download_fw` gained a `full` flag
  (minimal vs full rsvd-page descriptor) and a `rsvd_boundary` (the window restore value). → 3371
  -> **4513 ops, zero divergence** (the cold probe + the first 1142 ops of the airmon phase).
- The doc's prior guess that the builder was `fill_default_txdesc` was wrong — the recorded LS bit
  proves it's the USB `update_txdesc`. The gate forced out three non-obvious facts (BMC from the
  frame's addr1, the persisted rsvd_boundary, the PackageType general-info byte) — all in Known
  issues. The H2C descriptor needed no change (identical in both modes).
- `read_mac_hidden_rpt` now parses + stores `PackageType` (report byte 4 bits 4..6); `EfuseInfo`
  gained `package_type` (default 0 so the cold general-info, which runs before that read, stays 0).
- Frontier #4513 = `rtw_hal_init_mac_register` — next milestone.

## Port log — 2026-06-22 (init_mac_register + config_rx_info GREEN @ 4664)

- `mac.init_mac_register` applies `mac_reg_tbl.MAC_REG_TBL` — the 138-entry PHYDM MAC-reg table
  (`array_mp_8821c_mac_reg`, generated 1:1 from the vendor header; all flat 1-byte writes, zero
  conditional rows). `mac.config_rx_info` ports `cfg_drv_info_8821c(PHY_STATUS)` (DRVINFO_SZ=4 +
  APP_PHYSTS + the TRXFF_BNDY rxdesc-len workaround + RCR sync). → 4513 -> **4664 ops**.
- Followed the rtl8822bu_dkms `mac_reg_tbl.py` convention. Frontier #4664 = `rtw_hal_init_phy`
  (BB + RF, the big PHYDM-table milestone; needs the `check_positive` conditional walker).

## Port log — 2026-06-22 (BB enable + PHY_REG table GREEN @ 6348)

- Started `rtw_hal_init_phy`. New `phy_cond.py` (the PHYDM cut/rfe/package `check_positive` +
  IF/ELSE/ENDIF walker — identical to the 8822b family, reused for AGC/RF next) and `bb.py`
  (`init_bb_rf` BB/RF enable, `set_bb_reg` masked write [SRC] rtl8821c_phy.c:347, PRE-setting
  0x808, `phy_bb_config`). `bb_phy_reg_tbl.py` is the 1678-row table generated 1:1 from
  `array_mp_8821c_phy_reg`. → 4664 -> **6348 ops, zero divergence**.
- Discriminators (cut=chip_ver=4, rfe=info.rfe_type, package=7): both PHY_REG conditional groups
  fall to ELSE (cut 2≠4, rfe 5≠ours), reproduced exactly. Frontier #6348 = the BB AGC table.
