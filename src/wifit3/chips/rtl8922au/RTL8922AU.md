# RTL8922AU (rtw89 8922A, USB)

Realtek RTL8922AU, 802.11be (WiFi 7), USB. Retail card in hand: ASUS USB-BE93 (`0b05:1d84`).
Standalone port from the rtw89 vendor source, no shared base. This doc is the handoff: status,
where the source and captures are, how to verify, and what to port next.

## Status

Cold-boot bring-up, 5037/163814 driver ops reproduced and committed (capture-1; ~5080/5106 on
capture-2/3, poll-count variance only). `verify_pcap` now walks both VENQT control ops and
bulk-OUT ops; all three captures stop at the same frontier. What is ported:
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

`cmac_init_be(0)` is under way (`scheduler_init`, `addr_cam_init`, `rx_fltr_init` with the
sniffer-mode bit, `nav_ctrl_init`; `cca_ctrl_init_be` is a no-op). Frontier: op #5046,
`read 0x1144a` = `spatial_reuse_init_be`, then `tmac_init`, `trxptcl_init`, `rmac_init`,
`resp_pktctl_init`, `cmac_com_init`, `ptcl_init`, and the rest of `cmac_init_be` (mac_be.c:1756).
After cmac_init comes `dbcc_enable_be(true)` (qta is DBCC), the DMAC/CMAC IMR enables,
`err_imr_ctrl`, `set_host_rpr_be`, and the 8922A RSP_CHK_SIG clear, which finish `trx_init_be`.
Then `mac_init` does `feat_init`; then `core_start` continues into BB reg init, RF init, RFK
calibration, the channel tune, and monitor-mode RX enable. None of that is ported yet.

### Gotchas found while porting (not obvious from a single read)

- The cold-boot capture takes the **boot-mode branch** of `power_switch_boot_mode`, and
  `reset_pwr_state_be` finds the MAC already **`MAC_ON`**, so it runs the MAC-on arm.
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

## Next (from op #5046)

Inside `rtw89_mac_init` -> `trx_init_be` (mac_be.c:2302), `dmac_init_be` is done and `cmac_init_be`
is partway (through `nav_ctrl_init`). The frontier is `spatial_reuse_init_be` (`read 0x1144a`).
The remaining `cmac_init_be` sub-inits (spatial-reuse, tmac, trxptcl, rmac, resp-pktctl, com,
ptcl, ...) are mostly CMAC-window registers (0x10000+, band-1 at +0x4000). After it:
`dbcc_enable_be(true)` (qta is
DBCC), `enable_imr_be(DMAC_SEL)` + `enable_imr_be(CMAC_SEL)`, `err_imr_ctrl_be(true)`,
`set_host_rpr_be`, then the 8922A `R_BE_RSP_CHK_SIG` clear -> `trx_init_be` returns. Then
`mac_init` runs `feat_init` + `mac_post_init` (USB no-op) + `send_all_early_h2c` +
`h2c_set_ofld_cfg`.

Then `core_start` continues (core.c:6650+): `btc_ntfy_poweron`, `chip_reset_bb_rf`,
`phy_init_bb_reg` (the big BB register tables from the fw file), `chip_bb_postinit`
(`rtw8922a_bb_postinit`), `phy_init_rf_reg`, `btc_ntfy_init`, `phy_dm_init`, the EDCCA/PPDU-status
/phy-rpt band setup, `update_rts_threshold`, `hci_start`. After that the channel tune
(`rtw8922a_set_channel` + RFK) and monitor-mode RX enable. The deferred RF/board logical-efuse
extraction (`efuse->rfe_type`, xtal_cap, gain/tssi in `parse_efuse_map`, and the phy-cap parse in
`setup_phycap`) will be needed once RF init reads those values.

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
