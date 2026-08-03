# RTL8922AU (rtw89 8922A, USB)

Realtek RTL8922AU, 802.11be (WiFi 7), USB. Retail card in hand: ASUS USB-BE93 (`0b05:1d84`).
Standalone port from the rtw89 vendor source, no shared base. This doc is the handoff: status,
where the source and captures are, how to verify, and what to port next.

## Status

**`verify_pcap` reports `RESULT: PASS` on all three cold-boot captures.** Every register (VENQT
control) and bulk-OUT (fw/H2C) op of the whole recorded conversation is reproduced by real driver
code: the cold-boot bring-up, the monitor bring-up (configure_filter + monitor physts), the per-hop
MLO-mode handling, the periodic env-monitor DM watchdog, and all 101/103 channel hops across **both
2.4 GHz (ch1-14) and 5 GHz (ch36-165, HT20)**. On live hardware the card is a working monitor +
injection interface end to end: cold `connect()` + `set_channel()` (2.4 + 5 GHz) + RX (real beacons
parsed) + TX (`inject_frame` validated: a broadcast probe request drew 38 probe responses to the
forged SA). RX-ACK admission (`_enable/_disable_rx_acks`) is a documented no-op (monitor mode
already forwards all control subtypes to host, so the AP's ACKs reach RX), and the driver reads +
exposes its efuse MAC. **Forged-MAC auto-ACK (`enter_active_monitor`) works: `FAKE_MAC = SPOOFABLE`**
(bench-confirmed, notes below). What remains is the WEP / WPS labs (see the project task list and
`docs/planning/`).

What reproduces: all of `rtw89_core_start`, the mac80211 add-interface, and per-hop the FULL
`__rtw89_set_channel` for **both PHYs**. A hop is TWO `__rtw89_set_channel` calls: PHY_0/MAC_0 then
PHY_1/MAC_1 (same monitor channel), driven by one `chan.set_channel`. PHY_0 runs the full tune
(set_channel mac/bb/rf, set_txpwr, help-leave/post_set_channel, then btc_switch_band +
rfk_band_changed + the pure-monitor rfk_channel: pre_ntfy/txgapk/iqk/tssi/dpk/rxdck). PHY_1 reuses
the PHY_0 functions with phy_idx=1 (txpwr shifts +0x4000, RF1 ref table, PHY_1 BB offset) but skips
rfk_channel (the monitor vif's link is PHY_0-only) and the coex policy H2C (deduped, unchanged).

The mac80211 op stream after the first hop is now dispatched by `verify_pcap._drive` by each op's
opening read: `R_DBCC` (0x26b48) = `set_channel`, `R_BE_RX_FLTR_OPT` (0x11420) = `configure_filter`,
`R_PLCP_HISTOGRAM` (0x20738) = `config_monitor` (monitor physts). The channel is the only per-hop
runtime input still read from the wire (airodump picks it). `mlo_1_1` and the RX-filter value are now
DERIVED by the driver, not peeked (the peeks that fed them are gone): `configure_filter` computes the
monitor filter (`DEFAULT_MON_RX_FLTR` = 0x0F004431), and `set_channel` runs the prehdl double-tune
(rtw89_chip_rfk_channel, core.c:489-513) deriving each pass's MLO mode from a modelled entity force
(`_prehdl_force_phy0`): forced PHY_0 -> 2+0, cleared -> 1+1. The band-change work
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

**Hardware:** `scripts/chips/rtl8922au/test_hw.py` passes on the ASUS USB-BE93: `connect()` completes in
~3s and `set_channel()` succeeds for ch 1/6/36 (2.4 GHz + 5 GHz), so the whole bring-up + tune runs
on real silicon, not just against the pcap. It matches the device by VID:PID, so the co-plugged
rtl8812au is untouched. USB-2 quirk: the mode switch re-enumerates the card to SuperSpeed and the
driver doesn't re-acquire the handle, so the first `connect()` on a fresh USB-2 plug hangs (re-run,
or use USB-3). **RX works**: `connect()` starts the shared `RxReaderThread` on the vendor-interface
bulk-IN, `rx.iter_bulk_frames` strips the BE v2 rx descriptor (16-byte aligned), and real beacons
parse through `WlanFrameParser` with correct BSSID/SSID/RSSI (validated live: `mud2g` ch1 -67 dBm,
`dd-wrt` ch6, etc.). So the card is a working monitor interface end to end (boot + tune + receive).

### USB speed and RX

RX yield tracks the negotiated USB link speed. Same spot, 15-20s hop:
- USB 2.0 (`dev.speed == 3`): ~70-90 unique APs.
- USB 3.0 (`dev.speed == 4`): ~15; 2.4 GHz hit hardest.

Cause not pinned down (port / cable / negotiation, not chased). The check is `dev.speed`: if it
reads 4, move the card to a path that comes up 3. `PAD_CTRL2` force writes land but do not survive
the re-enum, so software cannot pick the speed. The kernel corroborates: `rtw89_usb_switch_mode`
logs "2.4 GHz performance may be better in a USB 2 port" on SuperSpeed.

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

`driver_captures/captures_rtw89_8922au_git/` (capture-1/2/3, cold boot). Taken on a USB-2 path:
`rtw89_usb_switch_mode` early-returns on SuperSpeed, so `switch_mode_be` reads `R_BE_PAD_CTRL2`
and the pcap opens with that read. Verify against all three per the methodology's step 6.

## Verify

    uv run python scripts/porting/verify_pcap.py rtl8922au [capture]

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

## Active monitor (auto-ACK): solved

`enter_active_monitor` makes the card HW-ACK a forged/chosen MAC while still monitoring, and it works.
`FAKE_MAC = SPOOFABLE`.

**The trigger is one thing: program the addr-cam SMA** (`firmware.h2c_addr_cam`, entry 0,
net_type NO_LINK). The RX responder auto-ACKs a received frame whose addr1 matches that SMA. Nothing
else is needed. Everything I first tried on top of it (a `net_type` INFRA/AD_HOC change, `join_info`
connected, `role_maintain` STATION, port-config `rx_sw`/`TSF_UDT`, clearing `B_BE_SNIFFER_MODE`, the
responder CCA check) was a **red herring**, chased because of a flaky-RX confound (below).

Bench-confirmed (ASUS USB-BE93 DUT + RTL8812AU prober, ch1, `scripts/chips/rtl8922au/`):
- `driver_check.py` / `confirm.py` case A: program the SMA, nothing else -> `ACKed 100/100`.
- `a1match.py`: with the SMA programmed the rx-desc `BE_RXD_A1_MATCH` bit is 1 on 100% of to-SMA
  frames (forged and silicon), 0 on broadcast. The hardware reads the CAM SMA as "me".
- `monitor_check.py`: while armed, the DUT still receives foreign toDS traffic (addr1 != SMA) and
  ACKs **only** the armed MAC (foreign frames get 0 ACKs). So it stays a real promiscuous monitor.

**The flaky-RX confound (why this took so long).** The 8922A's bulk-IN RX DMA wedges after repeated
cold-boot-of-a-warm-chip cycles: `connect()` always cold-boots, and `dev.reset()` / sysfs re-authorize
re-enumerate without cutting power, so a wedged pipe survives them. Only a physical replug recovers
it. When RX is wedged the DUT receives nothing, so it auto-ACKs nothing, which read as a long string
of false `0/100` negatives across every approach. The tells: a run's `total_wifi`/`DUT_recv` is 0
when wedged, hundreds when healthy; auto-ACK tracks that perfectly. Diagnostics print `DUT_recv` and
retry across connects to catch a healthy window. The rtl8xxxu `REG_MACID` note still holds (rtw89 has
no self-MAC register; the addr-cam SMA is the analog), but no port/role/sniffer change was ever
needed.

The `scripts/chips/rtl8922au/` diagnostics (`_amlib.py` harness + `confirm.py`/`driver_check.py`/
`monitor_check.py`/`a1match.py`/`ack_*.py`) are kept for the next chip or a regression check; leave
the 8812 sniffer plugged and re-run any of them after a physical replug.

## Next

- **WEP / WPS labs** (see the project task list and `docs/planning/`). Now unblocked: active monitor
  supplies the client-impersonation ACKs.
- `inject_frame` has no No-ACK flag path; add a test-only no-ACK path to verify TX bytes (cf. commit
  d58e6f252) if needed.
- **Slow set_channel (~0.5-1 s/hop):** each hop runs the full two-pass RFK (iqk/dpk/tssi/rxdck) for
  both PHYs, ~1500 USB control ops. The kernel skips most of that RFK unless the band/context
  changed; we redo it every hop. Deferred (works fine, just slow); skip the redundant RFK to speed
  hopping if it ever matters.

Only the 5/6 GHz **20 MHz** paths are exercised by the capture; the 40/80/160/320 MHz branches in
`_ctrl_bw`, `_set_txpwr_limit`, and `_fill_limit_*` still raise (not needed for the monitor hops, but
required if wider bandwidths are ever tuned).

**Recurring trap (still relevant):** each hop is a TWO-pass prehdl double-tune, not one tune. Pass 1
is MLO_2_PLUS_0_1RF (entity force = PHY_0) so the per-channel RFK calibrates the active path; pass 2
is MLO_1_PLUS_1_1RF (force cleared) and is the operating state the hop ENDS in (both RX chains up).
Freezing the mode at 2+0 (the old behavior) left PHY_1's RX chain off, ~half the beacons. Any helper
that branches on the mode must handle both; the mode is threaded via `transport.mlo_1_1`, set per pass
by `chan.set_channel` from the value the driver derives (`driver._tune_pass`). The
`_get_kpath`/`_get_syn_sel` helpers in phy.py already encode the per-mode path selection.

**Reusable assets:** `firmware.h2c_command` (any H2C), `firmware.txpwr_conf` (any txpwr fw element),
`firmware.element_regs`/`element_regs_with_idx` (any reg2 fw element), `phy.read_rf`/`phy.write_rf`
(HWSI + ad_sel RF access), `mac._reg_by_idx` (MAC_1 +0x4000 shift), and the scratch dumper
`/tmp/.../scratchpad/dop.py` (recreate: import `scripts/chips/rtl8922au/verify_pcap`, `build_ops`, print a
window with `_fmt`). The subagent workflow that worked for the RFK: dump the ground-truth op window,
hand a general-purpose subagent that file + source pointers + this doc, ask for an ordered byte-spec
reconciled to the wire, port from it, `verify_pcap`, commit.

## Working efficiently (notes for a fresh-context agent)

The port loop that has worked: read the source function -> grep the exact register/bit values
(`grep -m1 "define SYM " reg.h`) -> port it citing `file:line` -> `uv run python
scripts/porting/verify_pcap.py rtl8922au` -> the FRONTIER/DIVERGENCE trace names the next op or the exact
mismatch -> fix -> commit each milestone. Keep milestones small; commit when all three captures
advance to the same frontier.

To keep context low:
- **Delegate large source-mapping to a subagent** (general-purpose). It worked well for the H2C
  mailbox: ask for a tight spec (function logic in order + a register/value table with `file:line`
  + how payload words are built), and port from the returned spec instead of reading five files
  yourself. Good candidates ahead: `rtw89_mac_init` (huge), the BB/RF init + RFK tables.
- Use a tiny scratch dumper (recreate `/tmp/dop.py`: import `scripts/chips/rtl8922au/verify_pcap`,
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
