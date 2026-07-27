# RTL8922AU (rtw89 8922A, USB)

Realtek RTL8922AU, 802.11be (WiFi 7), USB. Retail card in hand: ASUS USB-BE93 (`0b05:1d84`).
Standalone port from the rtw89 vendor source, no shared base. This doc is the handoff: status,
where the source and captures are, how to verify, and what to port next.

## Status

Cold-boot bring-up, the monitor bring-up (configure_filter + monitor physts), the per-hop MLO-mode
handling, and the first three per-channel hops (2.4 GHz) are reproduced and committed (capture-1:
17753/163814 ops; capture-2/3 stop at the same frontier, poll-count variance only). `verify_pcap`
walks VENQT control ops and bulk-OUT (fw/H2C) ops. Frontier: **op #17762** = the periodic env-monitor
DM watchdog (read of `R_IFS_TOTAL_BE4` 0x20eec).

What reproduces: all of `rtw89_core_start`, the mac80211 add-interface, and per-hop the FULL
`__rtw89_set_channel` for **both PHYs**. A hop is TWO `__rtw89_set_channel` calls: PHY_0/MAC_0 then
PHY_1/MAC_1 (same monitor channel), driven by one `chan.set_channel`. PHY_0 runs the full tune
(set_channel mac/bb/rf, set_txpwr, help-leave/post_set_channel, then btc_switch_band +
rfk_band_changed + the pure-monitor rfk_channel: pre_ntfy/txgapk/iqk/tssi/dpk/rxdck). PHY_1 reuses
the PHY_0 functions with phy_idx=1 (txpwr shifts +0x4000, RF1 ref table, PHY_1 BB offset) but skips
rfk_channel (the monitor vif's link is PHY_0-only) and the coex policy H2C (deduped, unchanged).

The mac80211 op stream after the first hop is now dispatched by `verify_pcap._drive` by each op's
opening read: `R_DBCC` (0x26b48) = `set_channel`, `R_BE_RX_FLTR_OPT` (0x11420) = `configure_filter`,
`R_PLCP_HISTOGRAM` (0x20738) = `config_monitor` (monitor physts). Two per-hop runtime inputs are
peeked from the wire like the channel is: the RX-filter value (a mac80211 filter-policy input) and
`mlo_1_1` (the MLO mode the entity recalc lands on, peeked from the pre_set_channel_rf syn write:
RR_POW_SYN_V1 nibble 0xF = ALLON = MLO_1_PLUS_1). Threaded via `t.mlo_1_1`. The band-change work
(btc_switch_band + rfk_band_changed) now runs only when `!entity_active[phy]` or the band changed
(tracked in `t.entity_active`/`t.last_band`). iqk's kpath is `rtw89_phy_get_kpath` (RF_A for
MLO_1_PLUS_1 PHY_0), not RF_AB. rfk `pre_ntfy_mcc` carries `chan_to_rf18(channel)`. The 2.4 GHz TSSI
de is per-group (`_parse_tssi` stores the full cck/bw40 efuse arrays; `_tssi` selects by
`phy_tssi_get_cck_group`/`get_ofdm_group`).

Module map for the tune: set_channel_rf = `phy.set_channel_rf` (ctl_band_ch_bw, RF 0x18/0x10018 per
path via `phy.read_rf`/`write_rf`). set_txpwr = `txpwr.py` (firmware txpwr elements
BYRATE/LMT_2GHZ/LMT_RU_2GHZ/TX_SHAPE_LMT via `firmware.txpwr_conf` -> byrate/offset/tx_shape/limit/
limit_ru + the 8922a diff/ref/sar; the card's efuse country is "00" so **regd = RTW89_WW**).
set_channel_done = `phy.set_channel_help(leave)` (hal_reset re-enable + digital_pwr_comp LTPC tables +
`ctrl_mlo` + rfk_mlo_ctrl). The per-channel RFK = `rfk.rfk_band_changed` + `rfk.rfk_channel`, coex
notifies = `coex.ntfy_switch_band`/`ntfy_wl_rfk`, efuse-TSSI = `mac._parse_tssi`
(t.tssi_cck/mcs/therm) with the 300B TSSI H2C builder `rfk._tssi` (validated byte-for-byte).
Everything is **2G/HT20-only** so far (5/6G raises in set_gain / set_rx_gain_normal / encode_chan_idx
/ ctrl_bw / set_channel_mac, the txpwr limit tables, and `rfk._tssi`). The whole capture is ~150k
ops; each hop's tune is ~750 ops for PHY_0 plus the PHY_1 mirror.

**Hardware:** the live `connect()` smoke test passed on the ASUS USB-BE93 at SuperSpeed
(`scripts/rtl8922au/test_hw.py`): PyUSB drives the device, firmware uploads, and the init poll loops
converge on real silicon. That was the one hardware check wanted for now; **further hardware testing
is deferred until the port fully verifies** against the pcap. Keep working offline via `verify_pcap`.

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
(SuperSpeed) capture would set speed 4 and skip the `PAD_CTRL2` read. `build_ops` walks both VENQT
control ops and bulk-OUT ops (firmware chunks + H2C); `_drive` runs `connect()` then dispatches each
per-hop `set_channel` (peeking the target channel from the upcoming `R_FC0` write).

## Register access

A register op is a vendor control transfer on endpoint 0, `bRequest = 0x05` (`RTW89_USB_VENQT`),
`bmRequestType = 0xC0` read / `0x40` write. The address splits across the setup packet as
`wValue = addr & 0xFFFF`, `wIndex = (addr >> 16) & 0xFF`. [SRC] usb.c:31-32.

CMAC-window reads (`0xC000..0xFFFF`) can return `0xDEADBEEF` until the CMAC clock is enabled;
`read_cmac` re-enables it and re-reads. [SRC] usb.c:83-108. Indirect crystal-SI registers go
through `read_xtal_si` (write a command to `XTAL_SI_CTRL`, poll, read the data field).

## Next (from op #17762): the periodic env-monitor DM watchdog

The next block is the periodic DM watchdog (`rtw89_track_work`, core.c:5473): stat_track,
env_monitor_track, dig, cfo_track, antdiv_track, edcca_track. It is an **async producer** (fires on a
timer at irregular ops: 17762, 20845, 29966, 39052, 45125, ...), so it must not go in
`chan.set_channel`. On the wire it is env_monitor (ifs_clm counters at 0x20ecc-0x20eec, ifs_clm_set +
ccx at 0x20c00/0x20c28) + dig (`R_SEG0R_PD_V2` 0x26a74, `R_BMODE_PDTH` 0x26708/0x26718) + edcca_track
(`R_SEG0R_EDCCA_LVL_BE` 0x269ec, `R_SEG0R_PPDU_LVL_BE` 0x269f0); stat/cfo/antdiv look like no-ops at
idle. Because the capture has no traffic, every counter reads back 0, so the adaptive algorithms
collapse to fixed idle-state field writes (read-modify-write, the non-field bits echoed from the
replayed read). The op count varies per firing (FIRE1 44 ops with edcca, FIRE2 38 without), driven by
per-function period counters / ccx racing state that must be tracked to stay byte-exact.

Port as a driver method (e.g. `dm_watchdog`) for PHY_0's active BB; add a `verify_pcap._drive` dispatch
hook keyed on the opener read of `R_IFS_TOTAL_BE4` (0x20eec), like the configure_filter / physts hooks.

After the watchdog, the rest is the **per-channel hop loop** interleaved with more watchdogs and
configure_filter bursts. The tune code is channel-agnostic (replay supplies the reads; only
channel-table WRITTEN values differ), so the 2.4 GHz hops (ch1-14) should auto-advance. The first 5 GHz
hop (ch36, ~tssi index 50 in capture order) needs the deferred 5/6G branches (the 2G/HT20-only list in
Status).

**Recurring trap (still relevant):** at set_channel the MLO mode is MLO_2_PLUS_0_1RF (single PHY_0
monitor vif), not the core_start MLO_1_PLUS_1_1RF. Any helper that branches on the mode must handle
both; thread it via `transport.mlo_1_1` (True through core_start, set False in `chan.set_channel`).
The `_get_kpath`/`_get_syn_sel` helpers in phy.py already encode the per-mode path selection.

**Reusable assets:** `firmware.h2c_command` (any H2C), `firmware.txpwr_conf` (any txpwr fw element),
`firmware.element_regs`/`element_regs_with_idx` (any reg2 fw element), `phy.read_rf`/`phy.write_rf`
(HWSI + ad_sel RF access), `mac._reg_by_idx` (MAC_1 +0x4000 shift), and the scratch dumper
`/tmp/.../scratchpad/dop.py` (recreate: import `scripts/rtl8922au/verify_pcap`, `build_ops`, print a
window with `_fmt`). The subagent workflow that worked for the RFK: dump the ground-truth op window,
hand a general-purpose subagent that file + source pointers + this doc, ask for an ordered byte-spec
reconciled to the wire, port from it, `verify_pcap`, commit.

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
