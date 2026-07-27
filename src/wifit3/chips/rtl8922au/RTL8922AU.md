# RTL8922AU (rtw89 8922A, USB)

Realtek RTL8922AU, 802.11be (WiFi 7), USB. Retail card in hand: ASUS USB-BE93 (`0b05:1d84`).
Standalone port from the rtw89 vendor source, no shared base. This doc is the handoff: status,
where the source and captures are, how to verify, and what to port next.

## Status

Cold-boot bring-up, 13820/163814 driver ops reproduced and committed (capture-1; capture-2/3 stop at
the same frontier, poll-count variance only). `verify_pcap` walks both VENQT control ops and bulk-OUT
ops. **All of `rtw89_core_start`, the entire mac80211 add-interface, and the first per-channel tune
through `set_channel_help(leave)` are reproduced: set_channel_mac, set_channel_bb, set_channel_rf,
the full `set_txpwr`, and `set_channel_done` (post_set_channel bb/rf).** The frontier is now the
**per-channel RFK** (op #13829, `rfk_band_changed` -> `rtw89_phy_rfk_tssi_and_wait`, an H2C bulk-OUT
on ep 0x07), followed by `rfk_channel_for_pure_mon_vif` (pre_ntfy / txgapk / iqk / tssi / dpk / rxdck
offload H2Cs with interleaved BTC-notify and HWSI RF writes; report_wait is completion-based, so it
emits no register ops).

**Live hardware smoke test PASSED** (ASUS USB-BE93 `0b05:1d84`, SuperSpeed): `driver.connect()` runs
firmware upload + MAC/BB init + add-interface on real silicon with the poll loops converging (see
`scripts/rtl8922au/test_hw.py`). At SuperSpeed the rtw89 mode switch is skipped, so there is no
re-enumeration. RX is not yet testable (the channel does not fully tune until the RFK lands).

set_channel_bb: pre_set_channel bb/rf, set_channel_help(enter)/hal_reset, set_channel_mac, then all of
set_channel_bb (ctrl_sco_cck, ctrl_ch, ctrl_bw, ctrl_cck_en, spur_elimination, tssi_reset). set_channel_rf
is `ctl_band_ch_bw` (RF 0x18/0x10018 per path via `phy.read_rf`/`write_rf`). set_txpwr is a new
`txpwr.py`: the firmware txpwr elements (BYRATE/LMT_2GHZ/LMT_RU_2GHZ/TX_SHAPE_LMT via
`firmware.txpwr_conf`) drive byrate/offset/tx_shape/limit/limit_ru, then the 8922a diff/ref/sar. The
card's efuse country is "00" so **regd resolves to RTW89_WW**. set_channel_done is hal_reset(leave) +
digital_pwr_comp LTPC tables + `ctrl_mlo(MLO_2_PLUS_0_1RF)` + rfk_mlo_ctrl (set_syn01 + chlk_reload).
Two earlier data subsystems: the **BB-gain FW element** and the **efuse rx-gain offset**, both cached
on the transport. Everything is 2G/HT20-only so far (5/6G raises in set_gain / set_rx_gain_normal /
encode_chan_idx / ctrl_bw / set_channel_mac and the txpwr limit tables). The per-channel loop is
~150k ops, one unit ~750 ops.

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

## Next (from op #13829): the per-channel RFK

The whole channel tune through `set_channel_help(leave)` is done; only the RFK remains before one
channel fully reproduces (then the walk auto-advances to the next of the 202 hops). Two chip ops in
`__rtw89_set_channel` order:

1. **`rtw8922a_rfk_band_changed` (op #13829, the frontier)** = `rtw89_phy_rfk_tssi_and_wait(SCAN, 6)`:
   one TSSI-offload H2C (`rtw89_fw_h2c_rf_tssi`, class `H2C_CL_OUTSRC_RF_FW_RFK` 0xb, func
   `RFK_TSSI_OFFLOAD` 0x0) then a completion wait (no wire ops). The payload pulls efuse-TSSI de +
   thermal-meter tables (`fill_fwcmd_efuse_to_de` / `_tmeter_tbl`), so the efuse-TSSI parse may need
   un-deferring first (see `rtw8922a_efuse_parsing_tssi`, rtw8922a.c:744).
2. **`rfk_channel_for_pure_mon_vif` -> `rtw8922a_rfk_channel`**: btc_ntfy_wl_rfk(START), stop_sch_tx,
   `_wait_rx_mode(RF_AB)`, then pre_ntfy / txgapk / iqk / tssi(NORMAL) / dpk / rxdck offload H2Cs
   (each an H2C + completion wait), with interleaved BTC-notify writes and masked-HWSI RF writes,
   then resume_sch_tx + btc_ntfy_wl_rfk(STOP). Reuse `rfk.py`'s H2C helpers and `phy.write_rf`.

Report_wait is completion-based (waits on a C2H that the replay never sends), so it emits no register
ops: verify sees H2C, then the next real op. Dump each H2C's exact bytes with `dop.py` and reconstruct
the payload (the "simulate before you port" workflow used for byrate/limit) before writing the builder.

**The RFK H2C sequence, decoded from the wire (cat is always 2 = OUTSRC; class 0xb =
`H2C_CL_OUTSRC_RF_FW_RFK`, 0xa = `..._FW_NOTIFY`, 0x10 = the BTC/coex class):**

    op 13829  class 0x10 func 0x3  (11B)   btc_ntfy_switch_band     <- FRONTIER, coex, not in coex.py yet
    op 13830  class 0xb  func 0x0  (300B)  rfk_band_changed: TSSI(SCAN)   efuse-TSSI de + tmeter tables
    13831-13858  register/RF ops              btc_ntfy_wl_rfk(START) writes + stop_sch_tx + _wait_rx_mode
    op 13859  class 0xb  func 0x8  (84B)   pre_ntfy      (rfk.py already has _pre_ntfy for init_late)
    op 13860  class 0xa  func 0xf  (36B)   mcc notify    (rfk.py already has _pre_ntfy_mcc)
    op 13861  class 0xb  func 0x4  (8B)    txgapk
    op 13862  class 0xb  func 0x1  (8B)    iqk
    op 13863  class 0xb  func 0x0  (300B)  tssi(NORMAL)
    op 13864  class 0xb  func 0x3  (8B)    dpk
    op 13865  class 0xb  func 0x6  (9B)    rxdck

Blockers/notes for the RFK: (1) the very first op is `btc_ntfy_switch_band` (coex class 0x10) and
`btc_ntfy_wl_rfk` (START/STOP around the calibrations) -- neither is in `coex.py` yet, and the wire
can't advance past 13829 until switch_band reproduces. (2) The two 300B TSSI payloads need the
efuse-TSSI parse un-deferred (`rtw8922a_efuse_parsing_tssi`, rtw8922a.c:744) plus
`rtw89_phy_rfk_tssi_fill_fwcmd_efuse_to_de` / `_tmeter_tbl` (phy.c). (3) txgapk/iqk/dpk/rxdck are tiny
fixed payloads (like rfk.py's `_dack`/`_rxdck`); dump their 8-9B bytes and mirror. Header framing is
`firmware.h2c_command(cat=2, cls, func, payload)` (already verified); only payloads are new.

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
