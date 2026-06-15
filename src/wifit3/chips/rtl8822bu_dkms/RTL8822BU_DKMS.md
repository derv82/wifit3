# RTL8822BU — vendor/DKMS port (playbook)

> **STATUS: the entire first init cycle is COMPLETE** — chip-ID, EFUSE (+PABias), MAC power-on
> (pre_init + `card_en_flow` + init_system_cfg), HALMAC iDDMA FW download (40 packets + `0xC078`
> ready), full `init_mac_cfg` (trx/protocol/edca/wmac-RX), the `init_mac_flow` driver tail
> (RCR-sync/RTS-full-bw/USB-rx-agg), `_send_general_info` (two FW-offload H2C packets + the
> dump_fifo H2CQ readback + the HMEBOX reg-H2C), and the `hal_read_mac_hidden_rpt` C2H report read.
> All reproduce **byte-for-byte on capture-1/2/3** (~5269 ops; the few-op spread is variable poll
> counts the loops reproduce). **Frontier: the 2nd init cycle** (`rtl8822b_hal_init` →
> `_halmac_init_hal`, op ~5269 `W 0x00AA`) — a WARM re-init (card_dis+card_en) that re-runs the
> ported power-on/FW/MAC steps then adds `_drv_enable_trx` / `init_mac_register` / `config_rx_info`
> / **BB+RF (PHYDM)**. See "Next frontier" below for the exact wire + open questions. Promote
> sections to ground truth with `[SRC]`/`[WIRE]` citations; by the end this reads like
> `rtl8812au_dkms/RTL8812AU_DKMS.md`.

## Verified facts (ground truth so far)

- **USB identity / endpoints** `[WIRE]` capture-1/2/3 + usb-topology.log: `2357:0138`
  (TP-Link Archer T3U Plus), single config. Card traffic: control ep0; **bulk-OUT 0x05**
  (FW/TX); **bulk-IN 0x84** (RX). The coverage audit shows no other channel.
- **Register IO is the standard Realtek `bRequest=0x05` vendor control transfer**
  (`READ=0xC0/WRITE=0x40`, addr in wValue) `[SRC] include/usb_ops.h:19-22` — byte-identical
  to the AU family, so the USB layer is *not* HAL-specific.
- **8822b register-page-switch mirror** `[SRC] os_dep/linux/usb_ops_linux.c:171-201`:
  every vendor access to an **ON-section** register (`addr <= 0xFF` or `0x1000..0x10FF`) is
  followed by an extra 1-byte `bRequest=0x05` write to **`0x4E0`** carrying the low byte of
  the IO buffer (read-back value for a read, written value for a write). OFF/LOCAL-section
  regs (incl. `0xFExx/0xFFxx`) get no mirror. Reproduced in `transport.py`; `[WIRE]` every
  `R 0x00fc=0x0a → W 0x4e0=0x0a` pair in the capture confirms it.
- **Re-runnability = clean reset every time, NOT skip-style warm-reattach.** The vendor's
  own power-on handles a still-powered chip: `mac_pwr_switch_usb_8822b` reads `REG_CR`(0xEA
  marker), `REG_MCUFW_CTRL`(0xC078=FW-exist), and `REG_SYS_STATUS1+1` BIT0 to detect
  power state, and returns `HALMAC_RET_PWR_UNCHANGE` if already on
  `[SRC] hal/halmac/halmac_88xx/halmac_8822b/halmac_usb_8822b.c:32-98`. `rtw_halmac_poweron`
  catches that and **forces power-OFF (`card_dis_flow`) then power-ON again** — "Work around
  for warm reboot but device not power off" `[SRC] hal/hal_halmac.c:2744-2774`. So our bring-up
  prepends this reset; no replug needed, no warm-skip path. The power-OFF table is not in any
  cold capture (cold boots return SUCCESS, so the off→on never fired), so it is ported from
  source and proven by a hardware double-run. *Open: whether the off→on cycle recovers on
  Windows+WinUSB (the AU/jaguar cycle did not; 8822b `card_dis_flow` is a different sequence).*
### M0 — chip-ID reads (CLEARED, byte-for-byte on capture-1/2/3)

The pre-power-on probe, ported from source and gate-clean (ops 0–12; de-mirrored):
1. **`get_chip_info`** `[SRC] halmac_api.c:517-521` — HALMAC chip-id/cut detection, the very
   first IO: `R 0xFC` (`REG_SYS_CFG2` → `chip_id`; `0x0A` = 8822B) and `R 0xF1`
   (`REG_SYS_CFG1+1 >> 4` → cut; `0x3` = D-cut). → `chipid.get_chip_info`.
2. **`phy_cfg_usb_8822b`** `[SRC] halmac_usb_8822b.c:107` → `parse_intf_phy_88xx`
   `[SRC] halmac_common_88xx.c:3168` over the USB2 (empty) then USB3 param tables
   `[SRC] halmac_phy_8822b.c:40-58`. The D-cut USB3 entry `{0x0001, 0xA841}` is emitted by
   `usbphy_write_88xx` `[SRC] halmac_usb_88xx.c:475` as `W 0xFF0D=0x41` / `W 0xFF0E=0xa8` /
   `W 0xFF0C=0x81` (data-lo / data-hi / `offset | BIT(7)` strobe). `usb_page_switch` is a
   no-op for USB3. → `usbphy.phy_cfg_usb`. *(This was the op0 mystery — not a debug/scratch
   write but the USB3 intf-phy param for this cut.)*
3. **`read_chip_version`** `[SRC] rtl8822b_ops.c:173` — `R32 0xF0/0xF4/0x68`. → `chipid.read_chip_version`.

### Early EFUSE read (CLEARED, byte-for-byte on capture-1/2/3)

On the wire the chip-info path reads EFUSE **up front** (before power-on), not at M4 as the
milestone table guesses: `read_adapter_info` = `rtl8822b_read_efuse` `[SRC] rtl8822b_ops.c:616,3930`
→ `EFUSE_ShadowMapUpdate`. There is no FW yet, so the AUTO path falls to the driver-side
dump (`dump_efuse_drv_88xx`). Ported to `efuse.read_efuse`; the de-mirrored op stream is:

1. **`R 0x0A`** `REG_SYS_EEPROM_CTRL` `[SRC] halmac_reg_8822b.h:23` — autoload/eeprom-sel flags
   (`BIT(5)` set ⇒ autoload OK; `BIT(4)` clear ⇒ on-chip eFuse). Here `0x20` ⇒ map valid, eFuse.
2. **`switch_efuse_bank(WIFI)`** `[SRC] halmac_efuse_88xx.c:995` — `R 0x35` (`REG_LDO_EFUSE_CTRL+1`);
   the bank lives in bits[1:0] and powers up at 0 = WIFI, so the read matches and **no write fires**.
3. **`read_hw_efuse(0, 1024)`** `[SRC] halmac_efuse_88xx.c:1089`:
   - `cfg_ldo25(0)` `[SRC] halmac_common_8822b.c:159` — `R 0x37` / `W 0x37` clearing `BIT(7)` of
     `REG_LDO_EFUSE_CTRL+3` (reads need no 2.5V LDO; the bit is already clear so the value is unchanged).
   - `R 0x30` once to latch the upper command bits of `REG_EFUSE_CTRL` (`0x31600000` on this card),
     then per byte `addr 0..1023`: `W 0x30 = base | (addr<<8)` with `BIT(31)` clear, poll `R 0x30`
     until `BIT(31)` set, take the low byte as data. Address is `[17:8]` (10-bit), data `[7:0]`,
     `BIT_EF_FLAG = BIT(31)` `[SRC] halmac_bit_8822b.h:688,726-738`. `EFUSE_SIZE_8822B = 1024`
     `[SRC] halmac_8822b_cfg.h:55`, so this is exactly **4096 wire ops** (1024 × W+R, each mirrored).
4. **`eeprom_parser`** `[SRC] halmac_efuse_88xx.c:1198` — physical→logical (768 B) PG-header walk
   (1-byte header, or 2-byte extended when `hdr[4:0]==0x0f`; per enabled word copy 2 bytes; `0xFF`
   ends the walk). No wire IO. Decoded fields validated on capture-1: rfe_type `0x03`, crystal_cap
   `0x2f`, channel_plan `0xa5`, valid unicast MAC, 312/1024 physical bytes non-blank.

5. **`Hal_EfuseParsePABias`** `[SRC] rtl8822b_ops.c:553` — the tail of `read_efuse`. It reads
   physical efuse `0x3D7/0x3D8` (PA bias) via `rtw_efuse_access`; the physical map is already
   **cached** (valid from the dump), so HALMAC's `dump_efuse_map_88xx` `[SRC] halmac_efuse_88xx.c:132`
   serves it from memory and the only wire op is the WIFI bank-switch `R 0x35` (no `0x30` loop).
   This `R 0x35` is therefore the *last* EFUSE op, not the start of power-on.

The PG **tx-power** block (`hal_load_pg_txpwr_info`) is deliberately **not** decoded yet — it is
done at the tx-power milestone where each value is checked against the channel/power writes it
drives (decoding it now, with no consumer, would be unverifiable against the wire).

### MAC power-on (CLEARED, byte-for-byte on capture-1/2/3)

`rtw_halmac_poweron` `[SRC] hal/hal_halmac.c:2705`, ported to `mac.power_on(t, chip_ver)`:

1. **`pre_init_system_cfg_8822b`** `[SRC] halmac_init_8822b.c:945`: `W 0x1c`(`REG_RSV_CTRL`=0),
   `R 0xFF`(`REG_SYS_CFG2+3`; `0x80≠0x20` so the USB3-only `0xFE5B|BIT(4)` is **skipped here** —
   see the USB2/USB3 coverage note), PIN-mux RMWs `PAD_CTRL1`(0x64, set BIT28/29 → `0x36242000`) /
   `LED_CFG`(0x4c, clear BIT25/26) / `GPIO_MUXCFG`(0x40, set BIT2), `enable_bb_rf(0)` (clear bits
   on `0x02`/`0x1f`/`0xec`), and the `REG_SYS_CFG1+2 & BIT(4)` test-mode read.
2. **`mac_pwr_switch_usb_8822b(POWER_ON)`** `[SRC] halmac_usb_8822b.c:32`: `R 0xFE58`(RPWM),
   `R16 0x80`(`REG_MCUFW_CTRL`; `0x0001≠0xC078` so no 32K-leave toggle), `R 0x100`(`REG_CR`=`0xEA`
   ⇒ chip OFF, so the `SYS_STATUS1+1` probe is skipped), then the `card_en_flow` power sequence
   (`CARDDIS→CARDEMU` then `CARDEMU→ACT`) via `pwr_seq_parser_88xx`, then `W8_CLR 0xF5 BIT(0)` and
   `R 0x10C3`(`REG_SW_MDIO+3`). Cold chip ⇒ returns SUCCESS, so the warm-reboot off→on workaround
   `[SRC] hal_halmac.c:2768-2772` does **not** fire (it is ported in `power_on` for warm chips but
   has no cold capture — HW-double-run only).
3. **`init_system_cfg_8822b`** `[SRC] halmac_init_8822b.c:715`: `REG_CPU_DMEM_CON |= WL_PLATFORM_RST`,
   `REG_SYS_FUNC_EN+1 |= 0xDC`, and the boot-from-flash disable (`BIT_BOOT_FSPI_EN` is clear on
   cold boot, so the GPIO_MUXCFG follow-up is skipped).

The pwr-seq runtime (`pwrseq.py`) filters every row by interface (USB) + cut (`BIT(chip_ver+1)`,
D⇒BIT4, so the `CUT_C`-only `10A8/9/A` rows are skipped) and runs WRITE as a RMW, POLLING as
read-until-masked-match, DELAY/READ as no-ops. **Capture-3 polls `0x0005 BIT(0)==0` one extra
time** (reads `01,01,01,00` vs cap-1/2's `01,01,00`); the poll loop consumes exactly the recorded
reads on each boot — direct proof the polling is dynamic, not a single hardcoded read.

### Firmware download (CLEARED, byte-for-byte on capture-1/2/3)

`hal_read_mac_hidden_rpt` `[SRC] rtl8822b_ops.c:681, hal_com.c:1529` writes `W 0x1A0`
(`REG_C2HEVT_MSG_NORMAL = C2H_DEFEATURE_RSVD` 0xFD), then `rtl8822b_fw_dl → rtw_halmac_dlfw →
download_fw → halmac_download_firmware`, ported to `firmware.download(t, blob)`:

- **FW blob** = morrownr `array_mp_8822b_fw_nic` (v30.20, 161240 B), shipped in
  `assets/rtl8822bu_fw.bin`. It is **not** the linux-firmware rtw88 blob (161176 B, a different
  version) — the captures use the morrownr FW, so that is the wire ground truth and the gate
  byte-verifies it. Header: dmem 11216 B @0x200000, imem 149960 B @0x0, no emem (sizes incl. the
  8-byte per-segment checksum).
- **download_firmware_88xx** `[SRC] halmac_fw_88xx.c:115`: TX-FIFO-empty gate
  (`txfifo_is_empty(chk=10)` — a fixed 10× check of `0x41A==0xFF && (0x41B&0x06)==0x06`),
  ltecoex-0x38 save, `wlan_cpu_en(0)`, the **interleaved** reg save+set (R,W,R,W… on
  `TXDMA_PQ_MAP+1=0xC0` / `CR=0x05` / `H2CQ_CSR=BIT31` / `FIFOPAGE_INFO_1=0x200` /
  `RQPN_CTRL_2|BIT31` / `BCN_CTRL`), `pltfm_reset` (incl. the 8822b `SYS_CLK_CTRL+1 BIT6`
  clock-sync), `start_dlfw` (MCUFW FWDL bit + dmem then imem), `restore_mac_reg`, `dlfw_end_flow`,
  ltecoex restore.
- **The chunk loop** `dlfw_to_mem → send_fwpkt → dl_rsvd_page → iddma_dlfw`: each ≤4096 B block
  becomes a BEACON-qsel rsvd-page TX (48-byte TX descriptor `TXPKTSIZE/OFFSET=48/QSEL=0x10` +
  XOR-16 checksum, verified byte-exact) sent on **bulk-OUT 0x05**, then DDMA-copied from
  `TXBUF+0x30` to MCU mem with a running checksum (`CHKSUM_CONT` after block 0). USB pads one
  dummy byte when `(chunk+48)%512==0` (only the 3024 B dmem chunk). **40 packets** total (3 dmem,
  37 imem). `rsvd_boundary` is still 0 at this point (txff not yet allocated). `dlfw_end_flow`
  sets `FW_DW_RDY`, re-enables the CPU, and polls `REG_MCUFW_CTRL == 0xC078` (FW ready).

The gate replays a **merged ctrl+bulk** stream (`extract_bulk_out_ops` + `merge_ops_by_frame`)
against one `ReplayDevice` whose new `write()` byte-checks each FW packet.

### MAC init for RX (CLEARED) + the FW-info H2C (CLEARED)

`init_mac_flow` `[SRC] hal_halmac.c:3452` → `init_mac_cfg(trx_mode=NORMAL)` `[SRC] halmac_init_88xx.c:504`
is ported in `mac.py` as four sub-functions, all gate-clean: `init_trx_cfg` (queue map `0xF5A0`,
the `txff_allocation` page math — `rsvd_boundary=1996`/`pub=1803`/`h2cq_addr=0x3FA00` all
wire-verified, LLT auto-init), `init_protocol_cfg`, `init_edca_cfg`, `init_wmac_cfg` (RCR
`0xE400220E`). Then the driver tail `init_mac_flow_tail` (HW_VAR_RCR sync, `rts_full_bw(on)`,
USB `rx_agg` mode), and `firmware.send_general_info` (two 32-byte FW-offload H2C packets:
general-info `FW_TX_BOUNDARY=48`, PHYDM-info rfe/cut/rf/ant/package). `bulkout_num=3`,
`rxagg_mode=USB`, and the `get_trx_path` general-info fields (`rf_type=4`, ant `1/1`, package `0`)
are `[WIRE]`-pinned — re-derive from `get_trx_path`/`PackageType` if a different card is targeted.

### read-chip-info tail (CLEARED) — dump_fifo readback + reg-H2C + C2H report

The tail of `hal_read_mac_hidden_rpt` / `rtl8822b_read_efuse`, all gate-clean:
- **`dump_fifo` H2CQ readback** (`firmware._dump_h2cq_fifo`, the tail of `send_general_info_88xx`):
  reads 4 B from the H2C ring via the packet-buffer debug window (`RCR+2` rx-clk-gate, `0x140`
  start-page `0x7BF`, `R32 0x8A00 = 0x000DFF01` = the general-info header), confirms it landed.
- **`_send_general_info_by_reg`** (`firmware._send_general_info_by_reg`): an 8-byte reg-H2C
  (`0x4C` class/cmd, rfe/rf/cut/ant) over HMEBOX 0 (`HMETFR` `0x1CC`, `HMEBOX_E0` `0x1F0`,
  `HMEBOX0` `0x1D0` = `0x0300034C`).
- **C2H report read** (`firmware.read_mac_hidden_rpt`): poll `R 0x1A0`==`C2H_MAC_HIDDEN_RPT`(0x19),
  read 13 report bytes (`0x1A2..0x1AE`), ack `W 0x1A0=C2H_DBG`. Report is read-and-discarded
  (decodes wl-func/bw/proto caps into hal_spec, which wifit3 doesn't consume).

### Next frontier — the 2nd init cycle (op ~5269, `W 0x00AA=0x8000` ...)

`rtl8822b_read_efuse` returns ⇒ `rtw_hal_read_chip_info` done ⇒ `rtw_hal_init` → `rtl8822b_hal_init`
→ `_halmac_init_hal` `[SRC] hal_halmac.c:3576` — the **real** init, which RE-RUNS the already-ported
steps then adds the new ones:
0. **A power-OFF first** (f10769-f10935 — `rtw_hal_power_off`, NOT yet ported): `W16 0x00AA=0x8000`
   (`REG_PMC_DBG_CTRL1+2`, byte `0xAB` BIT7), then `mac_pwr_switch(OFF)` — probe (`R 0xFE58` /
   `R16 0x80=0xC078` ⇒ `W 0xFE58=0x80` rpwm-toggle / `R 0x100=0xFF`≠0xEA ⇒ chip ON ⇒ `R 0xF5`) ⇒
   **`card_dis_flow`** (f10787+ matches `pwrseq.CARD_DIS_FLOW` exactly: `0x93=0xC4`, `0xFF1A=0x30`,
   …, `0x90=0x00`), then a tail `R 0x35×3` + `W 0xFE58`. **This leaves the chip OFF** (next CR read
   is `0xEA`). The core (`mac_pwr_switch(OFF)`) is just `mac._mac_pwr_switch(power_on=False)` — port
   a thin `power_off(t, chip_ver)` = `W 0x00AA` + that + the tail. (Open: exact origin of `W 0x00AA`
   and the `R 0x35×3`/`W 0xFE58` tail — `mac_pwr_switch`'s init_adapter_dynamic_param? Identify, but
   they're few ops and wire-pinned.)
1. `rtw_hal_power_on` (f10953+) is then the **COLD path** — `pre_init` (`W 0x1C` + PIN-mux) +
   `mac_pwr_switch(ON)` (CR=`0xEA` ⇒ cold ⇒ `card_en_flow`) + `init_system_cfg` — i.e. **exactly
   `mac.power_on` reused unchanged** (it takes the no-reset cold branch).
   (The read-chip-info tail — `power_off`/card_dis + `read_phydm_trim` (3 cached PG reads = `R 0x35×3`,
   thermal+2G+5G all blank) + the cold `mac.power_on` — is now **CLEARED**, frontier at the 2nd
   download. `W 0x00AA`/`W 0xFE58` around it are 2 stray ops [WIRE]-pinned inline, source still TBD.)
2. `download_fw` again — **NOT a simple reuse**: this cycle `not_xmitframe_fw_dl=0`, so the FW packets
   go through the full xmit path (`usb_write_data_rsvd_page_normal` → `dump_mgntframe` →
   `rtl8822b fill_default_txdesc`) — a **full TX descriptor** (1st-pkt byte3 `0x84` vs the simple
   path's `0x00`, FS/LS/MACID/rate fields), not `build_fw_txdesc`. **Port the general rtl8822b TX
   descriptor builder** (`rtl8822bu_xmit.c` `fill_default_txdesc` for a BEACON rsvd-page) — it is
   shared with M8 frame injection, so this is the natural place to do it. Then the 40 packets +
   iDDMA reuse `firmware.download`'s loop.
3. `init_mac_flow` again (reuse `mac.init_mac_cfg` + `init_mac_flow_tail`).
4. `_drv_enable_trx` (new, driver-side TRX enable).
5. `_send_general_info` again (reuse — but **no** `mac_hidden_rpt` this cycle).
6. `rtw_hal_init_mac_register` (new — more MAC regs).
7. `rtw_halmac_config_rx_info(PHY_STATUS)` (new — DRVINFO/PHYSTS).
8. **`rtw_hal_init_phy` = BB + RF init (PHYDM)** — **CLEARED through the deterministic table init**
   (BB phy-reg + AGC + crystal-cap + RF-A/RF-B radio tables; see below). What remains on the wire
   (op ~9410 → end, ~20K ops) is the RF **calibration scan** — see the decode below.

### BB + RF tables (CLEARED, byte-for-byte on capture-1/2/3)

`rtl8822b_phy_init` `[SRC] rtl8822b_phy.c:278` brackets the BB+RF tables with two PHYDM parameter
passes and runs `init_bb_reg` then `init_rf_reg` between them:
- **PRE/POST** (`config_phydm_parameter_init_8822b`, `bb.phy_parameter_init`) — an RMW of `0x808`
  bits 28/29 (OFDM/CCK block enable): PRE clears them before the tables, POST sets them after RF.
  On the wire each reads-back the same `0x0E028233` (bits already in the wanted state), so the
  pair is a no-op-equivalent here, but ported faithfully (the earlier "byte0 rx-path pre-amble"
  guess was a misattribution — the 2T2R `0x808` byte0 = `0x33` is baked into the phy-reg table
  value, not a separate write).
- **phy-reg** (`array_mp_8822b_phy_reg`, `bb_phy_reg_tbl.py`) — 1492 plain `(addr, value)` W32
  rows via `odm_config_bb_phy_8822b`. No conditionals on this card.
- **AGC** (`array_mp_8822b_agc_tab`, `bb_agc_tbl.py`) — 10684 rows **with 328 cut/rfe
  conditionals**, run through `phy_cond.walk` + `check_positive` (ported 1:1 from
  `halhwimg8822b_bb.c` / `odm_read_and_config_mp_8822b_agc_tab`). check_positive matches
  cut[27:24]/package[15:12]/interface[11:8] as value-or-don't-care and rfe[7:0] exactly; the AGC
  conditions only constrain **rfe**. On this card (rfe 3) the walker selects **521 W32 rows**
  (addrs `0x81C`/`0xC50`/`0xE50`) == the single wire AGC run (frame 16503+), byte-for-byte.
  `odm_config_bb_agc_8822b` also feeds each `0x81C` row to `odm_update_agc_big_jump_lmt` (software
  DIG `big_jump_lmt[]` state, **no** register write — reconstruct when DIG is ported).
- **crystal cap** (`bb.set_crystal_cap`, tail of `init_bb_reg`) — `[SRC] phydm_set_crystal_cap_reg`
  8822b branch writes the EFUSE xtal-K (`0x2F` on this card, `EEPROM_XTAL=0xB9`) into
  `0x24[30:25]` and `0x28[6:1]` (masked RMW).
- **RF-A / RF-B** (`array_mp_8822b_radioa/radiob`, `rf_radioa_tbl.py`/`rf_radiob_tbl.py`,
  `rf.phy_rf_config`) — `init_rf_reg` configures path A then B from PHYDM radio tables, each with
  cut/rfe conditionals via the same `phy_cond.walk`. Each in-branch row is a single masked RF
  write: `[SRC] config_phydm_write_rf_reg_8822b` packs `((addr & 0xFF) << 20 | data[19:0])` into a
  W32 to `0xC90` (path A) / `0xE90` (path B); `addr 0xFE/0xFFE` rows are delays (no write). Walker
  selects **402** path-A + **353** path-B W32 rows == the wire `0xC90`/`0xE90` runs, byte-for-byte.
  The tx-power-track table (`odm_config_rf_with_tx_pwr_track_header_file`) that follows is
  software-only (stores deltas, no register writes) — nothing on the wire between RF-B and POST.

**The deterministic cold init ends at op ~9410.** Everything after is RF calibration, NOT more
init.

### RF calibration — the rest of the capture is an all-channel scan (decoded)

Decoding the RF channel-register writes (`0xC90`/`0xE90`, `addr 0x18`, `data[7:0]` = channel
number) across op 9410 → 29542 shows the vendor driver is **pre-calibrating every channel in both
bands, twice** — not tuning one channel:
- op ~9700–9740: IQK setup probing band-representative channels (36 / 100 / 149)
- op ~9900–14200: per-channel **DPK** on 2.4 GHz ch **1–11**
- op ~14600–16700: per-channel DPK on 5 GHz ch **36–64, 100–116**
- op ~16800–22400: 2.4 GHz ch **1–11 again** (TSSI / power-tracking pass)
- op ~22700–28500: 5 GHz ch **36–140, 149–165 again**
- op ~28780 → end: settles back on **ch 1** (final `switch_channel` + spur-cal/DIG cluster)

So the genuinely one-time work (IQK + LCK + first-channel DPK) is only ~2–3K ops; the other ~17K
is a full-spectrum DPK/TSSI scan over ~25 channels × 2 bands × 2 passes.

**Decision (Lead, 2026-06-15): per-channel on-demand cal — do NOT replay the all-channel scan.**
The kernel itself cal's a channel only when it tunes to it; our userland driver does the same.
Port the per-channel unit once and run it from `set_channel`, gating it against a single-channel
**slice** of the capture (not one monotonic cursor to the end). DPK is TX-only pre-distortion —
**deferred to TX (M8)**; it is not needed for RX. So the path to first beacon is small:
- **`set_channel`** = `config_phydm_switch_band/channel/bandwidth_8822b` `[SRC] rtl8822b_phy.c:822`,
  20 MHz only. The captures contain an airodump `--band abg` hop sweep — **38 `iw set channel N`
  commands** in `<cap>_logs/iw.log` (2.4 GHz 1-12, 5 GHz 36-165, back to 1), each one a vendor
  set_channel. `scripts/rtl8822bu_dkms/verify_channels.py` slices each hop window (iw epoch → pcap
  frame) and byte-diffs the port against it.

  **`switch_channel` (CLEARED — 27/27/26 hops byte-for-byte on capture-1/2/3).** `chan.switch_channel`
  + `sipi.py` (the BB-masked-RMW + SIPI RF read/write primitives: RF read = direct BB read at
  `{0x2800,0x2c00}[path]+(addr<<2)`; RF write packs `((addr&0xFF)<<20|data[19:0])` into `0xC90`/
  `0xE90`). Per channel: read RF_A 0x18 (clear bits 18/17/byte0, `|= ch`); AGC-tab `0x958[4:0]`;
  clock-offset `0x860[28:17]`; CCK TX filter `0xA24`/`0xA28` (2.4G); RF_A `0xBE[17:15]` phase-noise
  (5G low/mid/high tables); RF_A `0xDF[18]`; write RF_A/RF_B `0x18`; RF_A `0xB8[19]` toggle;
  `phydm_igi_toggle` (`0xC50`/`0xE50[6:0]`); `phydm_ccapar_by_rfe` (rfe-3 iFEM CCA table, col by
  band/Nrx → `0x82C/0x830/0x838`); `phydm_spur_calibration` → `phydm_dsde_init` (reset NBI/CSI
  `0x880-0x89C` + `0x874[0]`). `phydm_rfe` is NOT called per same-band hop (only on a band change).
  - **PSD spur sweep — deferred.** `phydm_dynamic_spur_det_eliminate` runs its read-dependent PSD
    sweep only on spur channels (`dsde_ch_idx ≤ 13`: 2.4G ch 5-8, 5G 153/161 at 20 MHz). Those 6
    hops `raise NotImplementedError` (verify skips them) — a bounded, flagged gap (missing NBI/CSI
    notch on 6 channels = slightly worse RX there, not a break), not a silent partial.
  - **Bandwidth re-apply (CLEARED — full `set_channel_bw`, 27/27/26 hops byte-for-byte).**
    `chan.set_channel_bw` = `switch_channel` + `mac_switch_bandwidth` (HALMAC `cfg_ch_bw_88xx`:
    `cfg_pri_ch_idx` `0x483`, `cfg_bw` clear `0x668[8:7]`, `cfg_mac_clk` `0x024[21:20]`+`0x55c`/
    `0x638`=`0x50`, `cfg_ch` `0x454[7]` band marker) + `config_phydm_switch_bandwidth_8822b` (20 MHz:
    `0x8ac &= 0xFFCFFC00`, `0x8c4[30]`, RF18 `|= BIT11|BIT10`, both paths; then `phydm_rxdfirpar`
    `0x948/0x94c[29:28]=2`+`0xc20/0xe20[31]=1`, re-run `ccapar`+`spur_reset`, `phydm_bw_fixed_setting`
    `0x840[3:0]=0`/`[4]=1`, `0x808` RX-path toggle to `0x33`, re-run `igi_toggle`). Each hop lands
    **exactly on the deferred per-channel DPK** (`0x1Dxx` LUT) — that's the boundary, verified.
  - **Band switch (CLEARED — both 2.4↔5 crossings byte-for-byte).** `chan.switch_band`
    (`config_phydm_switch_band_8822b`): 2.4G sets `0x808[28]=1`/`0x454[7]=0`/`0xa80[18]=0`/
    `0x814[15:10]=15`; 5G inverts those + `0x814=34`; both read the SoML marker `0x19a8[31]` and
    branch (rfe-3: `0xc04/0xe04[18,21]=0`, `0x8cc`=`0x08108492`/`0x8d8[27]=1`, except 5G SoML-on →
    `0x08108000`/`0x8d8[27]=0`), write RF_A/B `0x18` band bits, then `phydm_rfe_ifem`
    (`0xcb0/0xcb4/0xcbc/0xca0`, both paths) + `spur_reset`. `set_channel_bw(prev_ch=…)` runs it
    when `prev_ch` is on the other side of ch 14. The 2 crossing hops (ch 36, final ch 1) reproduce
    the full PHYDM tune byte-for-byte and stop at the lone `0xCBC` BT-coex band-notify
    (`rtw_btcoex_wifionly_switchband_notify`, a separate subsystem — wifit3 is BT-coex-less) which
    precedes the deferred DPK.

  **Net: `set_channel` is complete** — `verify_channels.py` clears **29/29/28 hops byte-for-byte**
  on capture-1/2/3 (every same-band 2.4 + 5 GHz hop + both band crossings). Skips: the 6 PSD spur
  channels (above) + a few slicing artifacts (windows whose iw-epoch→frame head lands mid-cal).
- **RX enable + monitor RX tail** → first beacons (RCR/monitor config is wifit3-side, like the
  other drivers — not a capture replay).
- **IQK + LCK** (RX-relevant image rejection / LO cal) — port if RX is deaf without them; one-time.
- **DPK + TSSI per-channel** — deferred; rides along with TX (M8).

### Coverage gap — USB2-link branches untested (all captures are USB3)

capture-1/2/3 are all **SuperSpeed (USB3)** links, so every link-speed conditional is only
verified on its USB3 side. The discriminator is `REG_SYS_CFG2+3 == 0x20` (== USB3, else USB2)
`[SRC] halmac_usb_88xx.c:48`. A USB2-link capture (plug the card behind a USB2-only
adapter/hub so it negotiates High-Speed) would let `verify_pcap` confirm the USB2 sides of:
- `pre_init_system_cfg`'s `0xFE5B |= BIT(4)` — **USB3-only**, skipped on USB2 `[SRC] halmac_init_8822b.c:962`
- USB RXDMA / aggregation mode `[SRC] halmac_usb_88xx.c:48,123,432`
- bulk-OUT size + EP/queue layout `[SRC] rtl8822bu_halinit.c:410-437` — may change the TX bulk-OUT EP (M8)

NOT covered by a USB2 capture: the USB2 intf-phy writes (`0xFE40-42`) are gated by the empty
`usb2_phy_param_8822b` table, not link speed — dead on 8822b regardless. When porting the
branches above, the USB3 side is gate-verified; the USB2 side stays source-ported-but-uncaptured
until a USB2 capture exists.

## Cleanroom rules

- Do **not** open `chips/rtl8822bu/`, `chips/rtw88_base/`, or `scripts/rtl8822bu/` — the
  mainline rtw88-derived driver, its shared base, and its tooling. Reading them produces a
  hybrid; no `_dkms` port imports `rtw88_base` (the AU ports use `rtl88xxau_base` instead).
  (The shared gate *engine* `scripts/rtw88_pcap_replay.py` is fine — family tooling used by
  every `_dkms` recipe, not driver code.)
- This is the **HALMAC + PHYDM (ODM)** vendor stack — port it from the vendor source.
- Sanctioned references: the vendor source (below), the sibling `chips/rtl8812au_dkms/`
  (closest: 2T2R 11ac), `rtl8821au_dkms`, `rtl8814au_dkms`, and their `scripts/*_dkms/`
  recipes. The AU family shares `chips/rtl88xxau_base/`; 8822b is a different HAL, so expect
  new chip-local modules (and possibly a new shared base), not reuse of that one.

## Why this port

Biggest win of the four Realtek 11ac DKMS re-ports — A/B baseline **8 → 29 APs (3.6×)**
(`planning/PORTING.md` § "Cleanroom DKMS re-ports"); the last one pending (8812/8814/8821
`_dkms` done). The gain is the full HALMAC/PHYDM calibration (IQK/DPK/CCK-PD/power-by-rate)
the vendor stack runs.

## Hardware / provenance

- Card: TP-Link Archer T3U Plus v1, `2357:0138`, CUT_D, MP, 2T2R, dual-band (in the DKMS
  `supported-device-IDs`). Windows: Zadig→WinUSB. Linux: unbind kernel driver.
- Vendor: morrownr `rtl88x2bu` 5.13.1 (Realtek 20210702 + community).
  - source: `usb_dumps_new/captures_rtl88x2bu/driver-source/` — HALMAC `hal/halmac/`, 8822b HAL
    `hal/rtl8822b/`, PHYDM/ODM `hal/phydm/`, efuse `hal/efuse/`.
  - tarball: `usb_dumps_new/driver-sources/rtl88x2bu-5.13.1.tar.xz`.
- DKMS cold-boot captures (byte-diff ground truth): `usb_dumps_new/captures_rtl88x2bu/`
  `capture-1/2/3.pcap` + `_logs/` (driver.log, `iw.log` with per-channel `set channel`
  windows, airodump, usb-topology). Driver `rtl88x2bu`, monitor, hops 2.4 then 5 GHz.
- Mainline A/B baseline (tie-or-beat target, not a port reference): `usb_dumps/
  captures_rtw88_8822bu/` + `usb_dumps_new/captures_rtw88_8822bu/`.

## verify_pcap — the faithfulness gate

- `scripts/rtl8822bu_dkms/verify_pcap.py` — capture parse + coverage audit done; bring-up call
  sequence is a TODO. Registered: `uv run python scripts/verify_pcap.py rtl8822bu_dkms`.
- Template: `scripts/rtl8812au_dkms/verify_pcap.py` (gate) + that dir's `verify_channels.py`
  (per-hop `iw set channel` byte-diff). Build a sibling `verify_channels.py`.
- The shared engine `scripts/rtw88_pcap_replay.py` feeds recorded reads back so RMWs / EFUSE /
  any live search reproduce; every write/bulk packet is checked. If the port is dev-centric
  (register IO via `dev.ctrl_transfer`, FW via `dev.write(ep)`), wrap `ReplayTransport` in a
  thin `ReplayDev` shim.
- Run against capture-1/2/3 (Post-Port Checklist #3).

## Milestones (subject to change — the vendor source + capture set the real order)

Port tiny-first, gate every milestone. The agent completes through *wiring* TX; the human
fires live TX (Hand-off).

| M | Scope | HW-verifiable signal |
|---|-------|----------------------|
| M0 | Enumerate + claim + transport + chip-ID reads. Confirm USB topology from the capture. | control plumbing works; chip-id matches; gate green |
| M1 | MAC power-on (HALMAC power sequence + LLT/autoload). | power-on regs byte-for-byte |
| M2 | Firmware upload (HALMAC iDDMA) + FW-ready ACK. Extract FW from pcap, byte-verify vs `linux-firmware/rtw88/`, ship in `assets/`. | FW-ready bit; byte-for-byte |
| M3 | MAC init for RX (TRX enable, RX filters, DRVINFO/PHYSTS). | `REG_CR` TRX bits; byte-for-byte |
| M4 | BB + RF init (PHYDM config tables) + EFUSE read (rfe/board/xtal/tx-power). | tables byte-for-byte; EFUSE decode matches |
| M5 | RF calibration as the vendor runs it on this boot (IQK/DPK/LCK/TSSI — only what's in the capture, reproduced). | byte-for-byte incl. any live search |
| M6 | Channel tune 2.4 + 5 GHz (per-hop). Build `verify_channels.py`. | every `iw set channel` window byte-diffs (incl. band crossings + DFS) |
| M7 | PHYDM dynamics (DIG + CCK-PD) + monitor-mode RX filter. | clean 2.4 GHz beacons, ≥8–10/s, ~29 APs across a hop (beat the A/B 3.6×) |
| M8 | TX descriptor + `inject_frame` wiring. Build `deauth_hw.py`. Agent does not live-inject. | TX byte-diff vs any captured injector, else behavioural via `deauth_hw.py` (human) |
| M9 | Warm-reattach: detect warm (FW-ready + TRX up), skip bring-up, light reattach + bulk-IN smoke test; surface "please replug" if the pipe is wedged. | relaunch resumes RX; wedged pipe → clear message |
| M10 | `driver.py` (WlanDriver protocol) + `manager.py` under `WIFIT3_RTL8822` + RxReaderThread (start before RX-enable) + DIG/CCK-PD ticked off the loop. | clean RX through the app; gate-faithful |
| M11 | A/B vs the mainline baseline (run via env var) + endurance soak (≥30 min, both bands). | tie/beat it on breadth+stability → flip default |

Open questions to resolve from the source (don't guess): does the 8822b HAL reuse
`rtl88xxau_base` or need a new shared base; HALMAC vs legacy efuse path; the morrownr
`CONFIG_*` build flags (deduce from the bytes); the monitor RCR value (a vendor/airmon tail
may not deliver beacons into our RX pipeline — sibling ports re-open RCR after the tail).

## Acceptance: Post-Port Checklist

Run `planning/PORTING.md` § "Post-Port Checklist" before declaring done — gate-green is
necessary, not sufficient. Agent runs 1–6 and reports; 7 is the human's:

1. Waiver review — init + airmon reproduce single-cursor, zero waived ops (only a different
   program's traffic is a legitimate waiver).
2. Skip audit — every kernel/vendor branch we don't emit is a marked `# TODO untestable: <why>`
   genuine no-hardware skip; classify every leaf faithful/hardcoded/omitted/N-A.
3. Capture coverage — gate PASSES on capture-1/2/3 (same silicon confirmed).
4. TX byte-diff vs the captured injector (if any; else `deauth_hw.py` is the only TX check).
5. Async producers — enumerate the vendor's periodic threads (DIG/CCK-PD/link-tuner/thermal);
   dispatch the ones that fire in monitor.
6. Recalibration cadence — per-hop recal matches the vendor's `config_channel`; per-hop HW lock
   holds so a cancelled tune can't strand the chip on a stale channel.

## Hand-off to the user

- TX is the human's trigger; the agent never live-injects. Build `scripts/rtl8822bu_dkms/
  deauth_hw.py` modelled on `scripts/rtl8812au_dkms/deauth_hw.py`: targeted unicast `--client`
  only, `--dry-run` default, watches RX for the target's EAPOL. Target MACs stay on the
  terminal — never commit them.
- Post-Port Checklist #7: hammer it in-app — alternate targets, hop hard, switch channels,
  replug mid-run, soak, fire the live attacks. Hunt the stale-channel hop and any wedge.

## Working style

- Do it inline in the main session. Don't over-decompose into subagents — past ports show it
  slows things down and a port usually leaves 50–70 % context free. Use agents only for
  genuinely parallel independent search.
- Gate each milestone before any hardware/behavioural debugging.
- Commit each milestone (stage only your files; no AI-authorship trailer).
- Keep this doc current with `[SRC]`/`[WIRE]` citations.
- Reference: `planning/PORTING.md` (recipe, hardware-verifiable milestones, Post-Port
  Checklist, the cleanroom-re-port workflow).
