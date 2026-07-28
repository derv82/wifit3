# RTL8922AU severe audit

Working log for the deep re-port audit. This is NOT the chip README (`RTL8922AU.md`); keep audit
findings here so the chip doc stays a short orientation. The premise of this audit: the port passes
`verify_pcap` byte-for-byte on the cold-boot capture, yet RX is non-deterministically deaf on real
hardware. verify_pcap proved to be structurally blind (it replays outbound bytes with canned read
values and zero timing, and it carries driver logic that feeds the port answers). So a PASS means
very little, and we treat every simplification in the port as suspect until re-checked against the
rtw89-7.2 C source at `/usr/src/rtw89-7.2`.

## Operating premise (the audit lens)

The dominant failure mode in this port is a skip that reads as reasonable: the code says "we don't
need X here" or "no-op on the 8922A" or "not needed for monitor," a reviewer nods, and X was
actually load-bearing. For every such claim the question is not "do we need X?" but "what IS X, and
what did the C driver spend those bytes/waits on?" Assume wrong until the source says otherwise.

## Why verify_pcap could not catch these

verify_pcap replays the captured USB control/bulk-OUT stream against the real `connect()` and each
`set_channel`, asserting byte-equality. It cannot see:

- **Inbound C2H.** RX bulk-IN is skipped entirely, and the packet-C2H channel (pkt_type=10) is where
  the firmware reports calibration completion, scan results, rate info, and more. No PASS touches it.
- **Timing.** Replay is instant with canned read values. Any "send H2C, wait for the firmware to
  finish" collapses to nothing, so a missing wait still passes.
- **Fed answers (driver logic in the harness).** `_drive` peeks the wire and hands the driver
  `mlo_1_1` (`_peek_mlo_1_1`), the RX filter (`_peek_rx_fltr`), and the decision of when to call
  `set_channel` / `configure_filter` / `config_monitor` / `dm_watchdog`. So the driver's own logic
  for those is never exercised. This is driver logic living in the test and it must be removed as we
  give the driver its own logic (see G5, G4).

## Confirmed gaps (P1: port these first, offline)

| ID | Severity | Gap | C source | Status |
|----|----------|-----|----------|--------|
| G1 | CRITICAL | Packet-C2H receive path (pkt_type=10) entirely unported; `rx.py` yielded only pkt_type==0 (WIFI) and dropped all firmware reports | `usb.c` rx_handler -> `core.c` rtw89_core_rx (pkt_type != WIFI -> process_report) -> `fw.c:8110` rtw89_fw_c2h_handle | **PORTED (offline); needs HW test** |
| G2 | CRITICAL | RFK report wait missing: `rfk_channel`/`rfk_band_changed`/`rfk_init_late` fired calibration H2Cs back-to-back with zero wait; kernel blocks on the C2H RFK report per step, `ms*=4` on USB (~1.8s/hop) | `phy.c:4055` rtw89_phy_rfk_report_wait, `rtw8922a.c:2388` rfk_channel | **PORTED (offline); needs HW test** |
| G3 | HIGH | phycap RF/antenna reply discarded: `setup_phycap` waits for the register-C2H response then throws away tx_nss/rx_nss/antenna/QAM/no_eht/no_mcs_12_13 | `mac.c:3222` setup_phycap_part0/1, rtw8922a hal extraction | not started |
| G4 | HIGH | `configure_filter` + `config_monitor` have NO live caller; the monitor RX filter + physts monitor config only ever run inside verify_pcap's `_drive` | `mac80211.c:388` ops_configure_filter, `mac80211.c:109` ops_config | not started |
| G5 | MED-HIGH | `mlo_1_1` hard-coded False live (interface never passes it); capture flips it per hop. Driver must compute the MLO mode itself (entity recalc), then remove `_peek_mlo_1_1` from verify | `chan.c:485` rtw89_entity_sel_mlo_dbcc_mode, `core.c:563` | not started |
| G6 | MEDIUM | `set_channel` ran synchronously on the event loop (UI freeze every hop); the tune must run off-loop | driver.set_channel | **DONE (off-loop executor) with G1/G2** |

## Audit backlog (P2: interrogate after P1)

Every "no-op / not needed / skipped / TODO verify untested / PCIE-only / already-done" claim, checked
against the C source. Seed list from a first grep; expand as the audit proceeds.

- [ ] `mac.reset_pwr_state_be` MAC_OFF and MAC_LPS arms marked untested (only MAC_ON exercised)
- [ ] `mac.pwr_on_func` / `pwr_off_func` "PCIE-only" blocks: confirm each truly never runs on USB
- [ ] `mac.setup_phycap` "hal extraction not yet done" (ties to G3)
- [ ] `mac.parse_phycap_map` thermal-trim "deferred"
- [ ] efuse MSS secure-boot parse marked untested
- [ ] `mpdu_proc_init` HDR_SHCUT double-read "source quirk" (confirm it is faithful to C, not a cover for a bug)
- [ ] `dmac_func_en_be` / `cmac_share_func_en_be` "return 0 / no-op on 8922A": confirm against C
- [ ] `preload_init` "not qta_poh on USB": confirm
- [ ] `sys_init` cmac_pwr_en(MAC_0) "already powered": confirm the tracking is right
- [ ] `phy.physts_parsing_init` monitor path: does the live driver ever call it?
- [ ] `coex` notifies: confirm the dedup logic matches C (SET_CX_POLICY resend-on-change)
- [ ] every `NotImplementedError` stub (6G, >20MHz): confirm the monitor path never reaches them
- [ ] `dm_watchdog` has no live caller: what does the kernel's periodic track_work actually do for RX (DIG/CFO), and do we need it running?
- [ ] the two `fw_check_rdy` variants "do not merge": re-confirm the FreeRTOS-done condition
- [ ] `rx.py` RSSI decode and the phy-report length handling: sanity vs C query_rxdesc_v2

## Findings log

(newest first; each entry: what, why it matters, C ref, action)

- **2026-07-27 G1+G2+G6 ported (offline).** Added the packet-C2H receive path and the per-step RFK
  report waits. Design (a lead-level call, flagged for review):
  - `rx.py iter_bulk_frames` now yields `(pkt_type, payload, rssi)` and surfaces C2H (pkt_type=10)
    packets; added `parse_c2h_hdr`. C source: fw.h:3871 (c2h_hdr w0 cat[1:0]/cls[7:2]/func[15:8]),
    fw.h:172 (cat TEST=0/MAC=1/OUTSRC=2), phy.h:206 (RFK_REPORT class 0x9), fw.h:5254
    (rfk_report: state at byte 8), core.h:5657 (STATE_OK=1).
  - `transport.RfkWait`: a `threading.Event` the tuner arms/blocks on and the RX reader signals.
    Signalled from the READER THREAD (`driver._scan_rfk_c2h` in `_rx_read_once`), NOT the asyncio
    loop, so a wait lands whether the tuner runs on the loop (connect) or an executor (set_channel).
    No deadlock: connect() can block the loop and still be signalled.
  - `rfk._h2c_wait`: send RFK offload H2C then block on the report; kernel per-step ms x4 for USB
    (phy.c:4066). Wired into rfk_channel (pre_ntfy5/txgapk54/iqk84/tssi20/dpk34/rxdck32),
    rfk_band_changed (tssi_SCAN 6), rfk_init_late (pre_ntfy5/dack58/rxdck128). ms values from
    rtw8922a.c:2353/2401/2412.
  - `driver`: RX reader started BEFORE rfk_init_late (so its C2H reports are received);
    `set_channel` runs the tune in an executor (fixes the UI-freeze/G6); `rfk_wait.enabled` set
    True only when the reader runs, so pcap replay (no reader) keeps the waits as no-ops.
  - verify_pcap still PASS 163814/163814 in 0.5s; full test suite 1897 passed; ruff clean.
  - **NEEDS HARDWARE TEST**: verify_pcap cannot exercise inbound C2H or timing. Must confirm on the
    card that (1) pkt_type=10 RFK reports actually arrive on bulk-IN during a tune, (2) the waits
    land (state OK) rather than time out, (3) RX is now deterministic on a fixed channel, (4) the
    concurrent reader-bulk-IN + tuner-control-transfer access is stable on PyUSB/libusb.

- **2026-07-27 audit kickoff.** Established the six P1 gaps above from the RX-deaf investigation.
  Root symptom is non-deterministic deaf RX; the strongest single cause is G2 (RFK no-wait), but per
  lead direction we treat it as one of many and port all known gaps before hardware retest. Reverted
  an earlier reader-pause experiment in driver.py (it was overkill: paused the reader on every hop).
