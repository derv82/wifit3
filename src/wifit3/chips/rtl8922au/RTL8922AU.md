# RTL8922AU (rtw89 8922A, USB)

Realtek RTL8922AU, 802.11be (WiFi 7), USB. Retail card in hand: ASUS USB-BE93 (`0b05:1d84`).
Standalone port from the rtw89 vendor source, no shared base. This doc is the handoff: status,
where the source and captures are, how to verify, and what to port next.

## Status

Cold-boot bring-up, 10479/163814 driver ops reproduced and committed (capture-1; ~10510/10553 on
capture-2/3, poll-count variance only). `verify_pcap` walks both VENQT control ops and bulk-OUT
ops; all three captures stop at the same frontier. **`rtw89_mac_init` is complete, and so is
`rtw89_core_start` through the BB/RF register tables.** The frontier is inside `rtw89_btc_ntfy_init`
(the coex fw H2Cs / `_run_coex`). Newly ported since the mac_init handoff (see the Log):
- **`rtw89_phy_init_bb_reg`** (`phy.py`): the firmware BB register table for PHY_0 and (DBCC) PHY_1,
  with the headline (rfe_type/cv) selection and the if/elif/else conditional walk. init_txpwr_unit
  and bb_reset are no-ops on the 8922A; bb_gain is software-only (gain arrays, deferred to RFK). 396
  wire writes. `firmware.element_regs` pulls the reg2 pairs from the BB_REG fw element.
- **The efuse rfe_type extraction** (`mac.parse_efuse_map`): the RF-block physical->logical parse
  (`rtw89_eeprom_parser_be`) now runs, setting `t.rfe_type` (=1 on this card) and `t.xtal_cap`. This
  was the handoff's deferred item; tssi/gain-offset arrays are still deferred (RFK-time).
- **`rtw8922a_bb_postinit`** (`phy.py`): FEN reset + BB rate-edge/slope/magnitude block, PHY_0 then
  PHY_1; set_phy_regs writes both phys, so the block runs four times across the two invocations.
- **`rtw89_phy_init_rf_reg`** (`phy.py`): the RADIO_A/B firmware tables applied through the HWSI
  (DDIE) RF-write (`write_full_rf_v2_a`: poll idle, write addr/data word) or the ad_sel direct-RMW,
  plus the per-path OUTSRC H2C of the stored entries. The 8922A stores RADIO_A at reg2.idx slot 1
  (rf_path A) and RADIO_B at slot 0 (rf_path B), so slot 0 = path B runs first. ~9000 wire ops.
- **`rtw89_btc_ntfy_init` (partial, `coex.py`)**: `btc_set_rfe` (software: rfe_type 1 -> 2-antenna
  shared, BT-general on RF path B) + `btc_init_cfg` (per-path trx-mask LUT, PTA priority, break/ZB
  tables), then the BT scoreboard read and the WL tx-power coex disable (`set_wl_tx_power`).

Everything below is the pre-existing mac_init port; it remains ported:
- USB register-access transport (`rtw89_usb_vendorreq` + read/write ops + `read_cmac`), the
  read-modify-write helpers (`write8/16/32_set/clr`, `write16/32_mask`), and `bulk_out`.
- USB mode-switch (`rtw89_usb_switch_mode`, speed-branched) + `read_chip_ver`.
- MAC power-on: `rtw89_mac_pwr_on` -> `power_switch(on=True)` in full (boot-mode handoff,
  `reset_pwr_state_be` all three MAC-state arms, `rtw8922a_pwr_on_func`, then the first-probe
  efuse reads and the coex scoreboard notify).
- `rtw89_mac_partial_init(include_bb=False)`: HCI/DMAC pre-en, `dle_init(QTA_DLFW)`, `hfc_init`,
  `fwdl_preconfig`, then `rtw89_fw_download`.
- **Firmware download** (`firmware.py`): multi-firmware container parse, v1 header parse with the
  formatted-MSSC security sections, the H2C/fwdl packet build (24-byte TX descriptor + 8-byte
  fwcmd header + tweaked firmware header), and the section transfers over bulk-OUT ep 0x07. The
  header + all 212 section packets byte-match. Blob in `assets/rtw8922a_fw-4.bin` (see FIRMWARE.md).
- `parse_efuse_map` (physical 0x1300 dump + USB MAC read from 0x4078) + `parse_phycap_map`
  (0x38 dump at 0x1700). The RF/board logical extraction is deferred (software, no wire ops).
- `setup_phycap` via the **register H2C/C2H mailbox** (`firmware.msg_reg`: H2CREG/C2HREG at
  0x7140-0x7164, nibble counters at 0x1F5), reading phy-cap part0 + part1 from the running fw.
- `mac_pwr_off` (`rtw8922a_pwr_off_func` USB arm + `power_switch(on=False)`): the `out:` tail of
  `chip_info_setup`. Then `rfkill_polling_init` (GPIO9 pinmux/mode + initial poll) closes probe.
- **Interface-up path** `rtw89_core_start`: `mac_preinit` (second `mac_pwr_on`, this time
  `probe_done` so no efuse tail, and `reset_pwr_state_be` takes the **MAC_OFF** arm; then
  `mac_func_en` powers+enables CMAC0 **and** CMAC1). `phy_init_bb_afe` is a no-op (no afe elm).
- `rtw89_mac_init` so far: `partial_init(include_bb=True)` = `chip_bb_preinit` (bb_preinit PHY_0
  and PHY_1, `bbmcu_write32` +0x30000 tables) then the **NORMAL firmware re-download** + the
  **BB-MCU suit** (`firmware.load_bbmcu_suit`: first BBMCU0 element after the mfw region, contents
  at hdr+32; skips the malloc write, ends on a BB0-FWDL-DONE check). Then `enable_bb_rf`,
  `sys_init` (cmac_func_en only, cmac_pwr already done), and **all of `dmac_init_be`**: `dle_init`
  (DBCC qta), full `hfc_init(en=true)`, `sta_sch_init`, `mpdu_proc_init`, `sec_eng_init`,
  `txpktctrl_init`, `mlo_init`.

- **All of `rtw89_mac_init`**: `trx_init_be` = `dmac_init_be` + `cmac_init_be(MAC_0)` (scheduler,
  addr-cam, rx-filter with the sniffer-mode bit, nav, spatial-reuse, tmac, trxptcl, rmac,
  resp-pktctl, cmac-com, ptcl, cmac-dma) + `dbcc_enable_be(true)` (band-1: TX-idle poll, DBCC
  quota change, band-1 TX preload, CMAC1 func-en, a second full `cmac_init` at +0x4000, BB1 enable,
  CMAC1 IMR, then the notify-dbcc H2C) + the MAC_0 DMAC/CMAC IMR tables + `err_imr_ctrl` +
  `set_host_rpr` + RSP_CHK_SIG clear. Then `feat_init` (2 init-ba-cam-users H2C), `mac_post_init`
  (USB `rx_agg_cfg_v3`), and `set_ofld_cfg` (H2C).
- **General H2C-command infrastructure** (`firmware.h2c_command`): the 8-byte fwcmd header
  (type/cat/class/func/seq, total-len, rec/done-ack) + payload behind an H2C TX descriptor, on the
  H2C bulk-OUT, with the `fw.h2c_seq` counter (reset after the fw download). Reusable for every
  later H2C. Built so far: `notify_dbcc`, `init_ba_cam_users`, `set_ofld_cfg`.
- The IMR tables were extracted from reg.h with a resolver script (`/tmp/.../scratchpad/imr.py`:
  BIT/GENMASK/OR eval) into `constants.py` (`IMR_DMAC_REGS`, `IMR_CMAC_REGS`). Reuse that resolver
  for the next big data tables.

Frontier: op #10488, a coex fw H2C (bulk-OUT ep 0x07, cat=2/class=0x10 OUTSRC-BTC), inside
`rtw89_btc_ntfy_init` after `set_wl_tx_power`. The remaining `ntfy_init` (coex.c:7746) is: the coex
fw H2Cs (`btc_fw_set_monreg` func 2, `rtw89_btc_fw_set_slots` func 1, `_fw_set_drv_info` INIT/CTRL
func 5, plus func 0/3), each a fixed-ish payload behind `firmware.h2c_command` (seq flows from
`t.h2c_seq`, now 6), the scoreboard register write (RMW at R_BE_SCOREBOARD 0xac, op #10496-97, from
`_run_coex` syncing `wl->scbd`), then **`_run_coex`** proper: the coex action engine, a large
stateful policy body (it emits the 0x2047c/0x11a40/0x26b48/0x20044/0x20c00... register writes plus
more H2Cs). `_run_coex` is the hard part of what remains before `phy_dm_init`.

After `ntfy_init`, `core_start` continues (core.c:6666+): `rtw89_phy_dm_init` (stat/dig/cfo/edcca/
bb_wrap init, then `rtw89_chip_rfk_hw_init` + `rtw89_phy_init_rf_nctl` (the RF_NCTL fw element) +
`rtw89_chip_rfk_init` = **RFK calibration, the bulk of the remaining ~150k ops**), the EDCCA/PPDU/
phy-rpt band setup, `update_rts_threshold`, `hci_start`, then the channel tune (`rtw8922a_set_channel`
+ RFK) and monitor-mode RX enable. Still-deferred efuse extraction: the RF gain/tssi arrays and the
phy-cap parse (RFK-time); rfe_type/xtal_cap are now extracted.

Note: the coex subsystem (`_run_coex` especially) is large and stateful. The handoff's "delegate
large source-mapping to a subagent" note applies squarely here; a subagent spec of `_run_coex`'s
cold-init action path (which action functions fire, in order, with their register/H2C footprint)
would make the port far cheaper than reading coex.c end to end.

### Gotchas found while porting (not obvious from a single read)

- The cold-boot capture takes the **boot-mode branch** of `power_switch_boot_mode`, and
  `reset_pwr_state_be` finds the MAC already **`MAC_ON`**, so it runs the MAC-on arm.
- **RF radio element idx-slot vs rf_path are swapped.** `rtw89_phy_init_rf_reg` iterates
  `rf_radio[slot]` for slot 0,1, but `build_phy_tbl_from_elm` stores each RADIO element at
  `rf_radio[elm.reg2.idx]`, and idx != arg.rf_path here: RADIO_A (id 4, arg.rf_path A) has idx=1,
  RADIO_B (id 5, arg.rf_path B) has idx=0. So slot 0 = RADIO_B = **path B runs first** (capture:
  0x22d24/0x22be0 before 0x22c24/0x22ae0). Use `element_regs_with_idx` and iterate by slot.
- **Displayed write hex in the verify trace is little-endian bytes**, not the u32. `write ... =
  1e000000` is the u32 `0x0000001e`; an HWSI RF write `= ef000002` is `0x020000ef` (addr 0xef, data
  BIT(17)). The port's `write32` packs LE, so this only matters when hand-decoding the trace.
- `dle_init(DLFW)` calls `get_dle_mem_cfg(ext_mode=SCC)` last, which sets `dle_info.qta_mode = SCC`.
  So `hfc_reset_param` reads back **SCC** and the H2C page precedence is `hfc_prec_cfg_c5` (32),
  not DLFW's c2. State-order matters.
- **The operating qta_mode is `RTW89_QTA_DBCC`, not SCC.** `rtw89_core_init` sets `dbcc_en=true`
  and `qta_mode=DBCC` for every BE chip (`core.c:6992`). So `chip_bb_preinit` runs both PHY_0 and
  PHY_1, and the operating `dle_init` uses the USB-2 **DBCC** `dle_mem` config (`wde_size8_v1` /
  `ple_size7_v1`, `wde_qt8_v1`, `ple_qt14/15_v1`), the `hfc` USB-2 DBCC param (`chcfg_ch8`,
  `pubcfg_p8`, `prec_cfg_c6`). The earlier handoff's SCC guess was wrong. `dle_mem`/`hfc_param_ini`
  are selected by `hci.dle_type` = USB2. Config tables are in `constants.py` (`_DLE_CFG`, `HFC_*`).
- `mpdu_proc_init_be`'s `HDR_SHCUT_SETTING` uses `write32_set(reg, val32)`, which re-reads the
  register and ORs `val32` -- so the local `&= ~TX_ADDR_MLD_TO_LIK` is overridden. Reproduce the
  double read, don't "fix" it.
- Several MAC-init sub-functions no-op because of software flags/config: `preload_init` (not
  qta_poh on USB), `dmac_func_en_be` / `cmac_share_func_en_be` (RTL8922A returns 0), and
  `sys_init`'s `cmac_pwr_en(MAC_0)` (CMAC0 already powered in `mac_func_en`, tracked by the
  transport's `cmac_pwr` set). `dle_input` is NULL on the 8922A (8922D+ only).
- `rtw89_mac_partial_init` ends with `rtw89_fw_download` inside it; firmware is downloaded during
  `chip_efuse_info_setup`, before `parse_efuse_map`. `wait_firmware_completion` / `fw_recognize`
  are file-side (no wire ops).
- The firmware header packet's only tweak vs the raw file is `w6` SEC_NUM 4->3: two security
  sections exist and the second (last) is marked `ignore`, compacted out, and the header trimmed
  16 bytes to 96. The `.bin` is a multi-firmware container; the NORMAL sub-firmware for cut 1 (at
  `hal.cv`=2) starts at shift 64.
- The two `fw_check_rdy` calls differ: WCPU-FWDL-DONE stops when `B_BE_WLANCPU_FWDL_EN` clears;
  FREERTOS-DONE stops when the status field (bits 26-29) reads raw 3. Do not merge them (the
  merged OR condition ends the FreeRTOS poll early on some captures).
- pcap_slicer maps frames 1-178 to enumeration (the 9 waived ops); the whole register bring-up
  runs under the first `airmon-ng start` phase. Bulk-OUT ep 0x07 = `out_pipe[bulkout_id[H2C]=2]`.
- The register H2C/C2H mailbox counters live in nibbles of 0x1F5 (h2c=low, c2h=high) and are
  monotonic per session (reset to 0 by fw download). `transport` holds `h2c_counter`/`c2h_counter`.
  `rtw89_fw_msg_reg` is data-table glue (not a `_be` pointer); only `cnv_efuse_state` is.

## Source

`/usr/src/rtw89-7.2` (morrownr rtw89 v7.2, installed via DKMS, persists across sessions). Port
from THIS, not from the mt7921au sibling in this tree (methodology forbids porting from a
sibling driver). Key files:
- `usb.c` the USB probe (`rtw89_usb_probe`), register access (`rtw89_usb_vendorreq`), mode
  switch (`rtw89_usb_switch_mode_be`).
- `core.c` `rtw89_read_chip_ver`, `rtw89_core_init` (the post-switch bring-up).
- `mac.c` `rtw89_mac_read_xtal_si_ax`, the power-on sequence.
- `reg.h` / `mac.h` register addresses and bitfields (paste verbatim, cite `file:line`).

`0b05:1d84` was added to `rtw8922au.c`'s id table so the kernel driver binds; the card runs as
a `wlan` interface under `rtw89_8922au_git` for hardware testing and re-captures later.

## Capture

`usb_dumps_new2/captures_rtw89_8922au_git/` (capture-1/2/3, cold boot). Taken on a USB-2 path:
`rtw89_usb_switch_mode` early-returns on SuperSpeed, so `switch_mode_be` reads `R_BE_PAD_CTRL2`
and the pcap opens with that read. Verify against all three per the methodology's step 6.

## Verify

    uv run python scripts/verify_pcap.py rtl8922au [capture]

One forward cursor over the device's VENQT control ops, driving the real `connect()`. Ops the
driver never emits (USB enumeration) are waived by name and logged, never dropped. It cannot
report PASS until every register op reproduces; on a mismatch it prints the frontier with a
10-before/after trace. `ReplayDev.speed = 3` (USB-2) so the mode-switch runs; a USB-C
(SuperSpeed) capture would set speed 4 and skip the `PAD_CTRL2` read. Only VENQT control ops
are walked so far. Extend `build_ops` for bulk-OUT (firmware chunks, TX) when the port reaches
them.

## Register access

A register op is a vendor control transfer on endpoint 0, `bRequest = 0x05` (`RTW89_USB_VENQT`),
`bmRequestType = 0xC0` read / `0x40` write. The address splits across the setup packet as
`wValue = addr & 0xFFFF`, `wIndex = (addr >> 16) & 0xFF`. [SRC] usb.c:31-32.

CMAC-window reads (`0xC000..0xFFFF`) can return `0xDEADBEEF` until the CMAC clock is enabled;
`read_cmac` re-enables it and re-reads. [SRC] usb.c:83-108. Indirect crystal-SI registers go
through `read_xtal_si` (write a command to `XTAL_SI_CTRL`, poll, read the data field).

## Next (from op #10488)

The frontier is inside `rtw89_btc_ntfy_init` (coex.c:7746), which `coex.py` ports partway (through
`set_wl_tx_power`). What remains in `ntfy_init`:
1. The coex fw H2Cs, in capture order: `btc_fw_set_monreg` (class 0x10 func 2, seq 6), then
   `rtw89_btc_fw_set_slots` (func 1), `_fw_set_drv_info(INIT)` / `(CTRL)` (func 5), and the func 0/3
   commands. Each is a fixed-or-computed payload behind `firmware.h2c_command` with cat=`H2C_CAT_OUTSRC`
   (2), class=`H2C_CL_OUTSRC_BTC` (0x10). Read `btc_fw_set_monreg`, `rtw89_btc_fw_set_slots`,
   `_fw_set_drv_info` (coex.c) and reconstruct the payload structs. Note `_send_fw_cmd` uses
   `rtw89_fw_h2c_raw_with_hdr(..., rack=false, dack=true)`.
2. The scoreboard register write (RMW at R_BE_SCOREBOARD 0xac, op #10496-97) and the `_run_coex`
   action engine (op #10499+: 0x2047c/0x11a40/0x26b48/0x20044/0x20c00 register writes + more H2Cs).
   `_run_coex(BTC_RSN_NTFY_INIT)` on a cold path with no BT is a bounded-but-large policy body; a
   subagent spec of which action functions fire in order (with their register/H2C footprint) is the
   efficient way in. This is the hardest chunk before `phy_dm_init`.

After `ntfy_init`: `rtw89_phy_dm_init` (BB stat/dig/cfo/edcca/bb_wrap init, then the RF_NCTL fw
element via `rtw89_phy_init_rf_nctl` and **RFK calibration** = the bulk of the remaining ~150k ops),
the EDCCA/PPDU/phy-rpt band setup, `update_rts_threshold`, `hci_start`, then the channel tune
(`rtw8922a_set_channel` + RFK) and monitor-mode RX enable. Deferred efuse: RF gain/tssi arrays and
the phy-cap parse (RFK-time). `firmware.element_regs`/`element_regs_with_idx` already pull any reg2
fw element (RF_NCTL is id 8); reuse for the NCTL table.

Two reusable assets are in place: `firmware.h2c_command` (any H2C) and the reg.h resolver at
`/tmp/.../scratchpad/imr.py` (recreate: parse reg.h, eval BIT/GENMASK/OR, emit tuples) for large
data tables. The scratch RF/coex dumpers used this session live under the scratchpad (`rf3.py`,
`h2c.py`: import `scripts/rtl8922au/verify_pcap`, `build_ops`, decode a window; `h2c.py` decodes an
H2C's cat/class/func/seq from the bulk-OUT bytes) -- recreate as needed.

The port loop from here is the same: read the sub-function, grep register/bit values, port citing
`file:line`, `verify_pcap`, fix at the FRONTIER/DIVERGENCE trace, commit each milestone. The
scratch dumper at `/tmp/.../scratchpad/dop.py` (recreate: import `scripts/rtl8922au/verify_pcap`,
`build_ops`, print a window with `_fmt`) shows the exact wire ops with decoded write values around
a frontier; decode a couple of writes to confirm masks before porting a table-heavy function.

## Working efficiently (notes for a fresh-context agent)

The port loop that has worked: read the source function -> grep the exact register/bit values
(`grep -m1 "define SYM " reg.h`) -> port it citing `file:line` -> `uv run python
scripts/verify_pcap.py rtl8922au` -> the FRONTIER/DIVERGENCE trace names the next op or the exact
mismatch -> fix -> commit each milestone. Keep milestones small; commit when all three captures
advance to the same frontier.

To keep context low:
- **Delegate large source-mapping to a subagent** (general-purpose). It worked well for the H2C
  mailbox: ask for a tight spec (function logic in order + a register/value table with `file:line`
  + how payload words are built), and port from the returned spec instead of reading five files
  yourself. Good candidates ahead: `rtw89_mac_init` (huge), the BB/RF init + RFK tables.
- Use a tiny scratch dumper (recreate `/tmp/dop.py`: import `scripts/rtl8922au/verify_pcap`,
  `build_ops`, print a mixed ctrl+bulk op window) to see the exact wire ops around a frontier
  without re-reading the pcap by hand. Run it with `uv run` from the repo root (a leading `cd`
  elsewhere breaks the venv).
- Simulate before you port when bytes are involved (the fw-download packet build was validated in
  a throwaway script against the captured frames before writing `firmware.py`). Cheaper than
  iterating the real port.
- Don't re-read files you just wrote; the verify tool is the source of truth for correctness.

## Style

Port from source, cite `file:line`. No milestone labels or status text in code (those live in
the commit message and this doc). Docstrings two lines or fewer, name things instead of
describing them, no jargon. No em-dashes and none of the banned words anywhere; see
`~/.claude/CLAUDE.md` and `docs/porting/CODE-STYLE.md`.

## Log

- 2026-07-26 M1: register-access transport + `Driver` subclass, full `rtw_8922au_id_table`.
- 2026-07-26 M2: USB mode-switch (USB-C/USB-2 speed branch) + `read_chip_ver`. 9 ops reproduced.
- 2026-07-26 M3: MAC power-on (boot-mode, `reset_pwr_state_be`, `pwr_on_func`). 121 ops.
- 2026-07-26 M4: power-on tail (efuse ecv/secure reads, coex scoreboard). 178 ops.
- 2026-07-26 M5a: DMAC pre-init (`hci_func_en`, `dmac_func_pre_en`). 188 ops.
- 2026-07-26 M5b: `dle_init` (QTA_DLFW quota subsystem). 218 ops.
- 2026-07-26 M5c: `hfc_init` + `fwdl_preconfig` finish `mac_partial_init`. 227 ops.
- 2026-07-26 M6a: WLAN-CPU disable + firmware-download enable (`disable_cpu`, `fwdl_enable_wcpu`).
  271 ops. Frontier at `fw_download_suit` (bulk-OUT firmware transfer next).
- 2026-07-26 M6-pre: `verify_pcap` bulk-OUT support + `fw_download_suit` pre-transfer control ops
  (secure-boot malloc, H2C path-ready). 274 ops.
- 2026-07-26 M6b: firmware download (mfw parse, v1 header + security sections, TX-desc/H2C-fwdl
  packet build, section bulk-OUT, ready polls). 521 ops. Blob `assets/rtw8922a_fw-4.bin`.
- 2026-07-26 M7: `parse_efuse_map` (0x1300 dump + USB MAC read) + `parse_phycap_map` (0x38 dump).
  4265 ops. Frontier at `setup_phycap` (`rtw89_mac_read_phycap` H2C next).
- 2026-07-26 M8: register H2C/C2H mailbox (`rtw89_fw_msg_reg`) + `setup_phycap` (phy-cap part0/1
  query to the running fw). 4307 ops. Frontier at chip_info_setup tail (XTAL_SI / data-setup).
- 2026-07-26 M9: `mac_pwr_off` (`rtw8922a_pwr_off_func` USB arm + `power_switch(off)`), the `out:`
  tail of chip_info_setup. 4358 ops. The XTAL_SI 0x270 run belongs to pwr_off, not data-setup.
- 2026-07-26 M10: `rfkill_polling_init` (GPIO9 pinmux/mode + forced + wiphy-work poll). 4364 ops.
- 2026-07-26 M11: second `mac_pwr_on` (`probe_done`, reset_pwr_state MAC_OFF arm) + `mac_func_en`
  (CMAC0+CMAC1). 4485 ops. Frontier `phy_init_bb_afe` (no-op) -> `mac_init`.
- 2026-07-26 M12: `chip_bb_preinit` (bb_preinit PHY_0+1, bbmcu tables) + `partial_init(include_bb)`
  NORMAL fw re-download. 4871 ops. Frontier at the BB-MCU suit.
- 2026-07-26 M13: BB-MCU firmware suit download (`load_bbmcu_suit`, BB0-FWDL-DONE check). 4902 ops.
- 2026-07-26 M14: `enable_bb_rf` + `sys_init`, then all of `dmac_init_be`: `dle_init(DBCC)`, full
  `hfc_init(en=true)`, `sta_sch_init`, `mpdu_proc_init`, `sec_eng_init`, `txpktctrl_init`,
  `mlo_init`. 5014 ops. Frontier at `cmac_init_be(0)`.
- 2026-07-26 M15: `cmac_init_be` start: `scheduler_init` (5023), `addr_cam_init` + `rx_fltr_init`
  (sniffer mode, 5031), `nav_ctrl_init` (`cca_ctrl_init_be` no-op, 5037). Frontier
  `spatial_reuse_init_be`.
- 2026-07-26 M16: cmac_init `spatial_reuse` + `tmac` + `trxptcl` (cv threaded for the CAV-only
  RSC write; this card is not A-cut). 5069 ops.
- 2026-07-26 M17: cmac_init `rmac_init` (+ `rst_bacam`; RX max len from the DBCC c0 quota). 5082.
- 2026-07-26 M18: cmac_init `resp_pktctl` + `cmac_com` + `ptcl` + `cmac_dma`. cmac_init_be done. 5094.
- 2026-07-26 M19: `dbcc_enable` band-1 bring-up: tx-idle poll, dle_quota_change, band-1 preload
  (chip op always runs on 8922A), CMAC1 func-en, a 2nd full `cmac_init` at +0x4000, dbcc_bb_ctrl.
  5210 ops (resp_pktctl/cmac_com now mac_idx-aware).
- 2026-07-26 M20: `enable_imr` (DMAC + CMAC IMR tables, resolver-extracted). CMAC1 IMR + MAC0
  DMAC/CMAC IMRs. 5234, then 5307 with the H2C below.
- 2026-07-26 M21: general H2C-command infra (`firmware.h2c_command`, `fw.h2c_seq`) + `notify_dbcc`.
  5307 ops.
- 2026-07-26 M22: `trx_init` tail: `err_imr_ctrl` + `set_host_rpr` + RSP_CHK_SIG clear. 5318.
- 2026-07-26 M23: `mac_init` tail: `feat_init` (2 init-ba-cam-users H2C), `mac_post_init`
  (rx_agg_cfg_v3), `set_ofld_cfg` H2C. **rtw89_mac_init complete.** 5323 ops. Frontier at
  `core_start` BB register init (`phy_init_bb_reg`).
- 2026-07-26 M24: `phy_init_bb_reg` (`phy.py`): BB register table PHY_0+PHY_1 (headline rfe/cv
  select + if/elif/else walk) + the efuse RF-block logical parse for rfe_type (=1)/xtal_cap. 5719.
- 2026-07-26 M25: `rtw8922a_bb_postinit` PHY_0+PHY_1 (FEN reset + rate-edge/slope/magnitude block;
  set_phy_regs writes both phys, so 4x across the two calls). 5893.
- 2026-07-26 M26: `phy_init_rf_reg` (RADIO_A/B tables via HWSI/ad_sel RF writes + per-path OUTSRC
  H2C; slot 0=RADIO_B=path B first). `element_regs_with_idx` exposes the reg2 slot. 10431.
- 2026-07-26 M27: `btc_ntfy_init` part 1 (`coex.py`): `btc_set_rfe` (sw, 2-ant shared, BTG=path B)
  + `btc_init_cfg` (per-path trx-mask LUT, PTA priority, break/ZB tables). 10470.
- 2026-07-26 M28: `btc_ntfy_init` part 2: BT scoreboard read (`_update_bt_scbd`) + WL tx-power
  coex disable (`set_wl_tx_power`, both disable arms). 10479. Frontier at the coex fw H2Cs.
