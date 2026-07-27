# RTL8922AU (rtw89 8922A, USB)

Realtek RTL8922AU, 802.11be (WiFi 7), USB. Retail card in hand: ASUS USB-BE93 (`0b05:1d84`).
Standalone port from the rtw89 vendor source, no shared base. This doc is the handoff: status,
where the source and captures are, how to verify, and what to port next.

## Status

Cold-boot bring-up, 13399/163814 driver ops reproduced and committed (capture-1; ~13415/13456 on
capture-2/3, poll-count variance only). `verify_pcap` walks both VENQT control ops and bulk-OUT
ops; all three captures stop at the same frontier. **All of `rtw89_core_start`, the entire mac80211
add-interface, and `rtw8922a_set_channel_mac` + `rtw8922a_set_channel_bb` (the full BB half of the
first per-channel tune) are reproduced.** The frontier is **`rtw8922a_set_channel_rf`** (op #13408,
the masked-HWSI RF writes on 0x22adc/0x22c24/0x22ae0 path A and 0x22bdc/0x22d24/0x22be0 path B).

set_channel_bb is complete: pre_set_channel bb/rf, set_channel_help(enter)/hal_reset, set_channel_mac,
then all of set_channel_bb (ctrl_sco_cck, ctrl_ch [set_gain, band_sel, set_rx_gain_normal, freq/sco/
cck-params/chan-idx], ctrl_bw, ctrl_cck_en, spur_elimination, tssi_reset). Two data subsystems were
un-deferred: the **BB-gain FW element** (`phy._decode_bb_gain`, 804 reg2 pairs -> the be gain arrays,
cached on the transport; applied by set_gain via the reg-def tables) and the **efuse rx-gain offset**
(`mac._parse_gain_offset` at parse_efuse_map -> `transport.gain_offset`, applied by set_rx_gain_normal).
Both are 2G-only so far (5/6G raises in set_gain / set_rx_gain_normal / encode_chan_idx / ctrl_bw /
set_channel_mac -- monitor hops are HT20 2.4/5 GHz, and 5G will need the gain_a tables + 5G bands).
The per-channel loop is ~150k ops, one unit ~750 ops.

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

## Next (from op #13408)

**The per-channel channel-tune + RFK loop, ~150k ops, the bulk of what remains.** `core_start`, the
whole add-interface, and set_channel_mac + set_channel_bb are done. `chan.set_channel` has TODO
slots for what's left of the unit:

1. **`rtw8922a_set_channel_rf` (op #13408, the frontier).** Masked-HWSI RF writes: path A on
   0x22adc(write)/0x22c24(read)/0x22ae0, path B on 0x22bdc/0x22d24/0x22be0 (uses the existing
   `phy.write_rf` masked-HWSI path). Read `rtw8922a_set_channel_rf` in rtw8922a_rfk.c; the RF-register
   values are channel-dependent (freq/band tables).
2. **`set_txpwr`** (chip op) for the channel, then **`set_channel_help(leave)`** (`phy.set_channel_help`
   already has the leave arm stubbed with a NotImplementedError: hal_reset re-enable + post_set_channel
   bb/rf -> digital_pwr_comp + ctrl_mlo), then **`rtw8922a_rfk`** (`_rfk_by_channel`: TXGAPK/RX-DCK/
   IQK/DPK/TSSI, mostly fw-offload H2C cat=2 cls 0x8/0x9/0x10 + masked HWSI RF writes). Delegate
   per-calibration subagent specs. `rtw8922a_rfk_init` is software-only.

The subagent workflow (proven for the BB-gain element): dump the ground-truth op window with the
scratch `dop.py`, hand a subagent the source + our infra (`firmware.element_regs`, `phy.write_rf`)
+ the ground truth, get a byte-spec reconciled to the wire, port, `verify_pcap`, commit.

**One recurring trap:** the MLO mode is MLO_2_PLUS_0 at set_channel (single PHY_0 monitor vif),
not the core_start MLO_1_PLUS_1. Any rtw8922a helper that branches on `mlo_dbcc_mode == MLO_1_PLUS_1`
(tssi_cont_en, adc_en, tssi_reset so far, and likely some RFK helpers) must do BOTH RF paths at
set_channel. Thread it via `transport.mlo_1_1` (True through core_start, set False in chan.set_channel).

The same code runs per channel; only channel-table written values differ (replay supplies reads).
A pragmatic sub-goal: get ONE channel's set_channel+RFK correct; the rest are replays. The subagent
workflow that worked all session: dump the ground-truth op window (`dop.py`), hand a subagent that +
source pointers + this cheatsheet, ask for an ordered byte-spec reconciled to ground truth, port,
`verify_pcap`, commit.

**Structure is in place** (`chan.py` + `Driver.set_channel()`): `connect()` ends after the
add-interface; each channel tune runs through `chan.set_channel(t, channel)`, which mirrors
`__rtw89_set_channel` (help-enter -> set_channel mac/bb/rf -> set_txpwr -> help-exit -> rfk) and
so far calls only `phy.pre_set_channel_bb` (the rest are TODO in `chan.set_channel`). Keep adding
the sub-functions there. `verify_pcap` drives the hops the way the other RTL ports do
(rtl8814au/rtl8821cu): after `connect()`, `_drive` peeks the `pre_set_channel_bb` opener (read
`R_DBCC` 0x26b48), decodes the target channel from the upcoming `R_FC0` (0x26b4c) center-freq write
via `chan.freq_to_channel` (the single source of truth for channel<->freq), and dispatches
`driver.set_channel(ch)`. The capture has 202 R_FC0 writes (the hop count). As each set_channel
sub-function lands, one channel's unit grows until it fully reproduces, then the walk auto-advances
to the next hop.

Reusable assets: `firmware.h2c_command` (any H2C), `firmware.element_regs`/`element_regs_with_idx`
(any reg2 fw element), `phy.write_rf` (masked/full HWSI + ad_sel RF write), the reg.h resolver
(`/tmp/.../scratchpad/imr.py`), and the scratch dumpers (`dump_*.py`: import
`scripts/rtl8922au/verify_pcap`, `build_ops`, print a decoded op window; recreate as needed). The
subagent workflow that worked all session: dump the ground-truth op window to a scratch file, hand a
subagent that file + the source pointers + the config/helper cheatsheet, ask for an ordered byte-spec
reconciled to the ground truth, port from it, `verify_pcap`, commit.

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
