# RTL8821CU (8821cu_dkms) — port reference

> Self-contained vendor/DKMS cleanroom port (no shared base — anti-DRY). Source of truth is
> the vendor tree in the bundle, **not** mainline rtw88. Citations are against
> `usb_dumps_new2/captures_rtl8821cu/driver-source/` (vendor `rtl8821cu-5.12.0.4`) and the
> cold-boot pcap `usb_dumps_new2/captures_rtl8821cu/capture-1.pcap`.

> **Status — power-on GREEN.** The byte-for-byte gate
> (`scripts/rtl8821cu_dkms/verify_pcap.py`) reproduces the cold-boot wire for **2170 control
> ops, zero divergence**: USB transport (+ the 8821c `0x4E0` mirror), the halmac mount
> chip-detect, the chip-version read, the full 512-byte EFUSE dump + physical→logical decode +
> BT-coex parse read, the pre-power-on system config, and the HALMAC card-enable power
> sequence. Frontier is now the post-power-on MAC init block (op #2170, a read of `0x1080`).
> Not registered in `wlan/manager.py` (claims nothing until complete).

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
| firmware download | — **(frontier)** | hal/hal_halmac.c:3350 `download_fw` ; hal/rtl8821c/rtl8821c_halinit.c:149 ; post-power MAC init rtl8821c_halinit.c:264 (op #2170 reads `0x1080`) | doors-map; next milestone |
| MAC/BB/RF init | — | hal/rtl8821c/usb/rtl8821cu_halinit.c:55 → rtl8821c_halinit.c:264 | doors-map; later milestone |

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

- **Frontier (next milestone): BT-coex power-on setting, then firmware download — op #2186
  (frame 5078): `IN 0x0002/2`.** The whole region #2186-~3360 is driven by **one function**:
  `hal_read_mac_hidden_rpt` ([SRC] hal_com.c:1550), called from `rtl8821c_read_efuse:525` — it
  reads the FW MAC-hidden capability bits, and to do so powers the chip and downloads firmware:
  1. `rtw_hal_power_on` (hal_com.c:1571) — our `bringup.power_on`, **already ported** (#2070-2185).
     Its tail calls `rtw_btcoex_PowerOnSetting` (hal_intf.c:470, gated on `EEPROMBluetoothCoexist`,
     which is TRUE on this combo-silicon card) → `ex_halbtc8821c1ant_power_on_setting`
     ([SRC] halbtc8821c1ant.c:3838). That is the **btc block #2186-2217** (decoded below).
  2. `rtw_write8(REG_C2HEVT_MSG_NORMAL=0x1A0, C2H_DEFEATURE_RSVD=0xfd)` (hal_com.c:1575) — op #2218.
  3. `rtw_hal_fw_dl` (hal_com.c:1579) → the chip `fw_dl` op → a 2nd btc power-on pass (#2219-2250,
     identical, no C2HEVT) then `download_firmware_88xx` ([SRC] halmac_fw_88xx.c:115) at #2271:
     ltecoex backup (0x1700/0x1708 indirect, `ltecoex_reg_read_88xx` common_88xx.c:3338) → MCUFW/CR/
     FIFOPAGE/RQPN/BCN_CTRL backups+writes → `pltfm_reset_88xx` → `start_dlfw_88xx` (`dlfw_to_mem_88xx`
     pushes the FW blob over **bulk-OUT ep 0x05**, first packet frame 5346, 4144 B each) → checksum/
     MCU-boot → `dlfw_end_flow_88xx` → ltecoex restore. Then poll C2HEVT for `C2H_MAC_HIDDEN_RPT`,
     read the rpt, write C2HEVT=`C2H_DBG` (hal_com.c:1588-1601).
- **btc power-on register sequence (decoded, matches #2186-2217 byte-for-byte)** —
  `ex_halbtc8821c1ant_power_on_setting`: W16 0x02 |= BIT0|BIT1 (BB enable, :3855); `set_ant_path`
  POWERON (:3884) → `coex_ctrl_owner(BTSIDE)` clears 0x73 BIT2 (:1604) + `set_ant_switch(BBSW,TO_BT)`
  (:2394-2470): 0x4e &= ~BIT7, 0x4f |= BIT0, 0xcb4 = 0x77, 0xcb7[5:4] = polarity (0x1 here), then
  0x67 |= BIT5 (PAPE) and 0x67 |= BIT4 (LNA_ON); `table` (:1664) writes 0x6c0/0x6c4 = 0x55555555,
  0x6c8 = 0x00ffffff, 0x6cc = 0x13; W8 0xfe08 = 0 (USB ant-path-for-FW local reg, :3895);
  `enable_gnt_to_gpio(TRUE)` is a no-op here (early return). The cb7 polarity + ant path come from
  `set_rfe_type` (:2474, pure logic) keyed on `board_info->rfe_type` + `single_ant_path` (efuse
  0xc3[6]) — **port those EFUSE reads (`Hal_ReadRFEType`, single_ant_path) so a sibling card's
  polarity is correct; do NOT hardcode 0x1**. The 2× repetition = two power-on passes (caller #1 =
  `rtw_hal_power_on`; caller #2 lives inside the chip `fw_dl` path — confirm exact site when porting).
- **To verify FW download the gate must replay bulk-OUT:** switch `scripts/rtl8821cu_dkms/verify_pcap.py`
  from `extract_ctrl_ops` alone to `merge_ops_by_frame(extract_ctrl_ops(...), extract_bulk_out_ops(...))`
  (both already in `rtw88_pcap_replay`; `ReplayDevice.write` byte-checks bulk). The FW blob is
  `array_mp_8821c_fw_nic[]` ([SRC] hal/rtl8821c/hal8821c_fw.c) — extract + byte-verify vs the bulk-OUT
  packets and vs linux-firmware, record provenance (PORTING.md Housekeeping).
- `transport` bulk-OUT EP defaulted to `0x04` but the **coverage audit shows FW/TX bulk-OUT is on
  ep `0x05`** (1152 pkts) — fix `Rtl8821cuTransport(bulk_out_ep=0x05)` (or probe it) at the FW milestone.
  The audit also flags interrupt-IN ep `0x81` (360 pkts) as a C2H blind spot.

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
