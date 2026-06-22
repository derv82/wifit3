# RTL8821CU (8821cu_dkms) — port reference

> Self-contained vendor/DKMS cleanroom port (no shared base — anti-DRY). Source of truth is
> the vendor tree in the bundle, **not** mainline rtw88. Citations are against
> `usb_dumps_new2/captures_rtl8821cu/driver-source/` (vendor `rtl8821cu-5.12.0.4`) and the
> cold-boot pcap `usb_dumps_new2/captures_rtl8821cu/capture-1.pcap`.

> **Status — MAC init GREEN.** The byte-for-byte gate
> (`scripts/rtl8821cu_dkms/verify_pcap.py`, now replaying ctrl + the FW/TX bulk-OUT stream)
> reproduces the cold-boot wire for **3257 ops, zero divergence**: USB transport (+ the 8821c
> `0x4E0` mirror), chip-detect/version, the EFUSE dump + physical→logical decode + BT-coex read,
> pre-power init + card-enable + init_system_cfg, the BT-coex power-on setting, the **iDDMA
> firmware download** (the 138 KB blob, byte-matched against the bulk-OUT), and
> **init_mac_flow** (queue/page/H2C/protocol/EDCA/WMAC cfg + RX-aggregation). Frontier is
> `_send_general_info` (op #3257, the first H2C bulk packet). Not registered in
> `wlan/manager.py` (claims nothing until complete).

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
| chip-info readback | `bringup.read_mac_hidden_rpt` | `hal_read_mac_hidden_rpt` hal_com.c:1550 | drives power-on + FW dl + C2HEVT readback — **partial** (FW dl + MAC cfg done; H2C/rpt tail pending) |
| firmware download | `firmware.download_firmware` (+ `fw_dl`) | `download_firmware_88xx` halmac_fw_88xx.c:115 ; iDDMA `dlfw_to_mem` :567 | blob `assets/rtl8821cu_fw_nic.bin` byte-matched vs bulk-OUT — **VERIFIED** |
| MAC init | `mac.init_mac_flow` / `init_mac_cfg` | `init_mac_flow` hal_halmac.c:3452 ; `init_mac_cfg_8821c` halmac_init_8821c.c:382 | queue/page/H2C/protocol/EDCA/WMAC + RX-agg — **VERIFIED** |
| general info H2C | — **(frontier)** | `_send_general_info` hal_halmac.c:3073 ; `send_general_info_88xx` halmac_fw_88xx.c:1046 | op #3257: 2 H2C bulk pkts (gen-info + phydm-info) + h2cq dump-poll + HMEBOX reg send |
| MAC-hidden rpt tail | — | `hal_read_mac_hidden_rpt` hal_com.c:1586-1605 | op ~#3270: poll C2HEVT==0x19, read 13-byte rpt, write C2H_DBG |
| power off | `pwrseq` (CARD_DIS_FLOW) | `rtw_hal_power_off` hal_com.c ; `mac_pwr_switch_usb_8821c` OFF | op ~#3285: the probe powers the chip back off (CARD_DIS_FLOW, already transcribed) |
| monitor entry (airmon) | — | rtl8821cu MAC/BB/RF + monitor; frame 7673+ | next phase after cold init (op #3371+) |

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

- **Frontier (next): `_send_general_info` — op #3257 (frame 7220), the first H2C bulk packet.**
  After `init_mac_flow`, `rtw_halmac_dlfw` calls `_send_general_info` ([SRC] hal_halmac.c:3073):
  `send_general_info_88xx` ([SRC] halmac_fw_88xx.c:1046) sends two H2C bulk packets — gen-info
  (sub-cmd 0x0D, content = FW_TX_BOUNDARY = rsvd_fw_txbuf−rsvd_boundary = 0x30) and phydm-info
  (sub-cmd 0x11, content = rfe_type 0x22 / rf_type 1T1R / cut / ant) — each an 80-B bulk = the same
  48-B TX desc as FW-DL (QSEL=H2C_CMD 0x13, OFFSET unset, XOR checksum) + 32-B H2C
  (`set_h2c_pkt_hdr_88xx` 8-B header: cat 0x01, cmd 0xFF, sub-cmd, len, seq). Then the h2cq
  dump-poll (`dump_fifo_88xx`: RX-clk-gate 0x060A, PKTBUF_DBG_CTRL 0x0140, read FIFO word at
  0x8000+residue == {0x01,0xFF}), then `_send_general_info_by_reg` (HMEBOX: read 0x1CC ready,
  W 0x1F0 ext, W 0x1D0 box0). The H2C txdesc reuses `firmware._build_txdesc_pkt` (qsel param).
- **Then the cold-init tail (#3270-3370):** the `hal_read_mac_hidden_rpt` readback (poll 0x1A0 ==
  C2H_MAC_HIDDEN_RPT 0x19, read the 13-byte report at 0x1A2.., write 0x1A0 = C2H_DBG 0x00), then
  `rtw_hal_power_off` (the probe powers the chip back off — `CARD_DIS_FLOW`, already transcribed in
  `pwrseq`, via `mac_pwr_switch(power_on=False)`). Cold init ends at op #3370 (frame 7672).
- **After cold init: the monitor-entry (airmon) phase, op #3371+ (frame 7673+)** — a fresh large
  region (MAC/BB/RF init, RF calibration, channel tune). This is where RX actually comes up; it is
  the next major milestone after the cold path closes.
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
