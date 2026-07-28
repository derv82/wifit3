# RTL8922AU severe audit

Working log for the deep re-port audit. This is NOT the chip README (`RTL8922AU.md`); keep audit
findings here so the chip doc stays a short orientation. The premise of this audit: the port passes
`verify_pcap` byte-for-byte on the cold-boot capture, yet RX is non-deterministically deaf on real
hardware. verify_pcap proved to be structurally blind (it replays outbound bytes with canned read
values and zero timing, and it carries driver logic that feeds the port answers). So a PASS means
very little, and we treat every simplification in the port as suspect until re-checked against the
rtw89-7.2 C source at `/usr/src/rtw89-7.2`.

## HANDOFF: remaining P1 kernel-behavior port (start here next session)

The RX-critical work is done + hardware-validated (RFK/C2H waits, packet-C2H receive, mode-switch
re-enum handling, connect progress). What remains is "port what the C does that the port skips",
under the mandate to be correct for the **chipset** (any 8922au / any silicon revision), not for the
one bench card. None is the RX bug; all are real correctness/robustness work.

### OPEN HARDWARE ISSUE: USB-C is buggy (user-reported, uncharacterized)
On USB-C (SuperSpeed) the card is "really buggy" (user-reported; specific symptoms not yet captured).
Earlier this session it did enumerate SuperSpeed on USB-C and pass connect + RX, so this is a
regression or an instability to reproduce and characterize next. Different code path from USB-2: on
USB-C the mode switch does NOT fire (already SuperSpeed), so the re-enum handling is not involved.
Prime suspects to check: SuperSpeed RX-DMA / reader-thread behavior at 5 Gbps, concurrent
control+bulk transfer stability, and whether connect() / set_channel behave differently when
`dev.speed == SuperSpeed`. First step: get exact symptoms + a repro (beacon_watch + rfk_validate on
the USB-C plug), compare to the USB-2 numbers above.

### Independent, low-risk items
1. **h2c_fw_log comp bitmap** (`firmware.py:411`). Port the correct mask `0x14001806` = BIT(INIT 1,
   TASK 2, PS 11, ERROR 12, MLO 26, SCAN 28) [SRC fw.c:2826, fw.h:192]; the port hard-codes
   `0x0000012D` (wrong). Also add a way to actually ENABLE fw logging (a flag / a
   `scripts/rtl8922au/` helper) so it is usable to diagnose RX/TX from the firmware side.
2. **FW-download retry** (`mac.fw_download` / `firmware.download`). Kernel retries
   `__rtw89_fw_download` up to 5x [SRC fw.c:2034]; the port does one attempt and aborts on a
   transient USB glitch (seen on mt76 / flaky links). Wrap the download + its ready-poll in a 5x
   retry.
3. **Dropped efuse cv (ecv)** (`mac.mac_pwr_on`, ~line 506). `efuse_read_ecv` result is a local var
   and discarded; the kernel writes it to the GLOBAL `hal.cv` [SRC efuse_be.c:536], authoritative for
   every later cv branch (pwr_on_func aphy patch, efuse_pwr_cut_ddv, trxptcl_init). Make it update
   `transport.cv`. A different-revision 8922au (register-cut != efuse-cut) takes the wrong init
   branch today.
### mlo_1_1 (the important one) — investigation state, NOT yet fixed
WiFi 7 (802.11be) runs on 2.4/5/6 GHz (not a separate band); the 8922a is a 2x2 chip with two RF
paths (chains A + B) and two BB PHYs. mlo_1_1 is the DBCC mode: MLO_1_PLUS_1_1RF (True) vs
MLO_2_PLUS_0_1RF (False). `MLO_MODE_FOR_BB0_BB1_RF(bb0_streams, bb1_streams, rf)` [core.h:4166].

Established:
- True -> `set_syn01(ALLON)`: both RF synths on (both RX chains). False -> after both tune passes the
  net synth state is path-A OFF, path-B on (one chain). [SRC rtw8922a_rfk.c:145 set_syn01_cbv, :364
  pre_set_channel_rf].
- HARDWARE: `rfk_validate.py --mlo 1` (True) roughly DOUBLED beacons vs `--mlo 0` (False). Both
  chains materially help RX; frozen-False costs ~half our RX.
- The capture ends every channel in True (1+1): wrap `driver.set_channel`, run verify's real `_drive`
  -> strict per-channel `(ch,False),(ch,True)` pairs (50 True / 51 False on capture-1).
- Mode is chosen by `rtw89_entity_recalc` from the vif's chanctx-assigned link count:
  `entity_force_hw==PHY_0 -> 2+0`; else `active_hws` BIT(0) -> 2+0, BIT(0)|BIT(1) -> 1+1
  [chan.c:484 sel_mlo_dbcc_mode]. The port models neither links nor entity_recalc; it freezes False.

UNRESOLVED (finish before implementing):
- WHERE the capture's two-call-per-channel comes from. It is NOT the `prehdl_link` dance in
  `rtw89_chip_rfk_channel` (core.c:489): that needs `!WITH_RFK_PRE_NOTIFY` (old fw) AND `!mon`; the
  8922a fw HAS pre-notify and our vif is `pure_monitor_mode_vif` (so `mon=true`). Prime suspect: the
  SCAN path (airmon-ng + airodump hw-scan hops the channel). Read `rtw89_hw_scan*` on BE and decide
  whether the SECOND call (1+1/True) is the monitor's steady mode or a scan artifact.
- WHY 2+0 ("2 streams on PHY_0", which sounds like both chains) empirically yields ONE chain while
  1+1 yields both. Gap between the enum's stream semantics and set_syn01's synth mechanism I could
  not fully reconcile. Empirical answer (True = 2 chains, ~2x RX) is solid; the RF-arch "why" is not.
- Whether the wifite monitor vif SHOULD carry 1 or 2 chanctx-assigned links (that decides 2+0 vs 1+1
  via entity_recalc).

Recommended fix: do what the kernel does. Model the pure-monitor vif's steady mode via entity_recalc
(after the scan-path analysis), confirm it lands on 1+1 for our single monitor, and tune 1+1 so both
chains come up. The blunt "tune mlo_1_1=True" matches the observed steady state and doubles RX, but
confirm entity_recalc actually yields it so it is principled, not a magic constant. THEN strip
`_peek_mlo_1_1` (driver logic) out of verify_pcap once the driver computes the mode itself. Validate:
soak both chains, confirm ~2x beacons is stable and RFK still lands 26/26.

Note for whoever finishes this: the mode also feeds IQK kpath, ADC enable, TSSI, digital-pwr-comp,
and ctrl_mlo (the DBCC RX-path split), so a mode change is not just the synth write.

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
| G3 | HIGH cap / COND for RX | phycap RF/antenna reply discarded: `setup_phycap` waits for the register-C2H response then throws away tx_nss/rx_nss/antenna/QAM/no_eht/no_mcs_12_13. Audit A: the ONLY RX-wire consumer is `bb_cfg_txrx_path` (hard-codes RF_PATH_AB + rx_nss=2 at phy.py:888); on a 2x2 card the C also lands on RF_PATH_AB so the discard is BENIGN. Bites only if THIS dongle's fw reports 1-antenna/asymmetric. Verify the fw antenna report on hardware | `mac.c:3222` setup_phycap_part0/1, `rtw8922a.c:2626` bb_cfg_txrx_path | not started; likely benign (2x2), verify on HW |
| G7 | MEDIUM | `mac_pwr_on` reads the efuse chip-version (`efuse_read_ecv`) into a LOCAL var and discards it; the kernel writes it to `hal.cv` GLOBALLY (efuse_be.c:536), making the efuse cut authoritative for every later cv branch (pwr_on_func aphy, efuse_pwr_cut_ddv, trxptcl_init). The port keeps using the R_AX_SYS_CFG1 register cut. Latent: wrong branch if register-cut != efuse-cut | efuse_be.c:516, mac.c:1557 | audit-found (A#2) |
| G4 | LOW (was HIGH) | `configure_filter` + `config_monitor` have no live caller, BUT `rx_fltr_init` (mac.py:1214, called at 1396 in the connect path) already sets B_BE_SNIFFER_MODE + accept mgnt/ctrl/data to host, so the base monitor filter IS established at connect (beacons were received). configure_filter is a mac80211 re-apply, not required for static monitor. Audit item: confirm B_BE_A_A1_MATCH + sniffer actually passes ALL frames (not just addr1==self) | `mac_be.c:1315` rx_fltr_init_be, `mac80211.c:388` ops_configure_filter | downgraded; audit A1_MATCH |
| G5 | HIGH (2nd RX factor?) | `mlo_1_1` frozen False live -> net path-A synth OFF (only path B RX on 2x2). Capture ends every channel in True (1+1, both synths ALLON) via a False-then-True two-call sequence. Fix once G1/G2 HW-validated: tune True (both paths) or replicate the two-call. Then remove `_peek_mlo_1_1` from verify. See findings log | `rtw8922a_rfk.c:145` set_syn01_cbv, `chan.c:484` entity table | RESOLVED (analysis); HW-test True vs False |
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

- **2026-07-27 mlo_1_1 CONTRADICTION RESOLVED (Audit B#5 vs Audit C#1) + likely 2nd RX factor.**
  Audit B said False is correct (entity_recalc -> 2+0 for 1 link); Audit C said False strands the
  path-A synth (RX killer) and the capture alternates. I got the ground truth by wrapping
  driver.set_channel and running verify's real `_drive`: **the capture dispatches set_channel TWICE
  per channel, (ch, False) then (ch, True), ALWAYS ending on True** (capture-1: 50 True / 51 False,
  strict F,T pairs). So:
  - The steady-state mode the kernel leaves per channel is **MLO_1_PLUS_1_1RF (True)**: both passes
    write RF_SYN_ALLON, so **path A synth = f AND path B synth = f (both RX paths ON)**.
  - Our live driver calls set_channel ONCE per hop with mlo_1_1=False -> PHY_0 writes ON_OFF (A=f,
    B=0), PHY_1 writes OFF_ON (A=0, B=f); net end-state **A=0 (OFF), B=f**. On a 2x2 card only path B
    receives -> degraded/one-chain RX. Mechanism confirmed at rtw8922a_rfk.c:145-160
    (set_syn01_cbv) + :364-374 (pre_set_channel_rf), entity table chan.c:484-506.
  - entity_recalc gives True only when the monitor vif has 2 chanctx-assigned links (BIT(0)|BIT(1));
    the capture's kernel monitor vif clearly ends with 2 links. Our port models neither the link
    count nor the two-call-per-channel sequence.
  - **This is a plausible SECOND RX root cause** (path-A dead) on top of G1/G2 (RFK no-wait).
    Non-determinism could come from RFK (fixed) while the one-chain loss is this.
  - **DO NOT ship an mlo change blind.** Options once G1/G2 is HW-validated: (a) tune with
    mlo_1_1=True so both synths stay ALLON, or (b) replicate the kernel's False-then-True per hop
    (2x tune cost). Discriminate on hardware: tune False vs True and compare beacons/RSSI on both
    RX chains. If G1/G2 alone makes RX solid, path-B-only may be sufficient and this is cosmetic.
  - Also revisit the PHY_0-only rfk_channel guard (Audit C#3): under a correct 1+1 mode PHY_1 also
    has a link and would run per-hop RFK; the port skips it.

- **2026-07-27 Audits B (phy.py) + D (firmware.py/coex.py) complete.** Both corroborate A: the port
  is faithful; the RX bug is not in these files; the evidence points to the now-fixed C2H/RFK gap.
  - **G5 RESOLVED-ish (Audit B#5): `mlo_1_1=False` live is CORRECT** for a single monitor vif.
    core_init sets MLO_1_PLUS_1_1RF; entity_recalc for one active link (BIT(0)) yields
    MLO_2_PLUS_0_1RF (chan.c:491), so False at hop time / True at init matches the real driver. NOT
    an RX bug. OPEN QUESTION: my earlier `_peek_mlo_1_1` found True on ~62% of capture hop-openers,
    which contradicts "capture is 2+0". Reconcile the peek (scan-window artifact?) before removing
    it from verify; the hard-coding is also fragile for any non-single-monitor scenario
    (`MLO_DBCC_NOT_SUPPORT` is unreachable, `_ctrl_mlo`/`_get_kpath` would mishandle it).
  - **Dropped settle waits in phy.py (Audit B#1-4)** = things the C does that we skip. Likely masked
    by USB control-transfer latency (>=125us) but genuinely omitted; restore for faithfulness:
    - B#1 [MED] `_read_full_rf_v2_a` (phy.py:281): missing `udelay(2)` between RD-trigger and RDONE
      poll -> a stale RDONE could return a wrong RF word (channel/band RMW corruption). rtw8922a_rfk.c
      read path / phy.c:1076.
    - B#2 [MED] `_ctl_band_ch_bw` (phy.py:1324): missing `fsleep(100)` after EACH CFGCH RF write
      (up to 4/hop), the actual synth channel/band/BW writes. rtw8922a_rfk.c:81.
    - B#3 [LOW-MED] `_hal_reset` enter (phy.py:842): missing `fsleep(40)` between adc_en(false) and
      bb_reset_en(false). rtw8922a.c:2309.
    - B#4 [LOW] `_bb_reset_en` disable (phy.py:833): missing `fsleep(1)` before RSTB_ASYNC_ALL clear.
      rtw8922a.c:1863.
  - **Audit D real bug (D#1) [LOW/latent]: `h2c_fw_log` comp bitmap wrong**: port sends 0x0000012D,
    C is BIT(1,2,11,12,26,28)=0x14001806. Latent because connect calls it with enable=False (comp=0).
    Fix the constant. fw.c:2826, fw.h:192.
  - **Audit D robustness (D#5) [LOW]: FW download single attempt** vs kernel's 5 retries
    (fw.c:2034); a transient failure aborts connect (could read as non-deterministic bring-up).
  - **Audit D (D#6) [LOW]: `_write_h2c_reg`/`_read_c2h_reg`** spin 3000x with no timeout abort and
    read stale C2HREG on exhaustion (msg_reg used once, off RX path; low impact).
  - VERIFIED-OK (do NOT re-suspect): phy RX gain (CCK/OFDM, efuse per-path offsets), LNA/TIA/op1db/
    RPL gains (real values, not zeros), DIG/PD-threshold (support_igi false for 8922A is a genuine
    no-op), HWSI RF write path, channel programming values (_chan_to_rf18/_encode_chan_idx/_ctrl_ch/
    _ctrl_bw/spur), physts, set_channel_help ordering, both-PHY tune. firmware: H2C header packing,
    rack/dack (all connect H2C are fire-and-forget in C too, none dropped a wait), msg_reg wait,
    addr_cam v0 / default dmac v2 / cmac g7 layouts, the full FW-download flow + completion waits,
    all class/cat/func constants. coex: _cfg_sb, ntfy_radio_state early-return, slot TLV format
    (per-hop coex simplifications diverge from _run_coex/_action_common but are no-ops for BT-absent
    monitor).

- **2026-07-27 Audit A (mac.py power + phycap) complete.** Headline: the register-level init is
  FAITHFUL to the C (verified branch guards, poll masks, read orderings, RMW quirks, efuse struct
  offsets). All genuine gaps are in software/firmware-response data replay cannot see; none is a
  certain RX root cause for a 2x2 8922AU. Strongest RX suspects remain the now-fixed C2H/RFK gaps.
  - G3 refined (above): phycap discard benign on 2x2; conditional otherwise.
  - G7 (new, MED): efuse cv (ecv) dropped, above.
  - LOW/latent to port for robustness (not RX-critical):
    - `read_phycap` drops the C2H `id` check (mac.c:3213 -EINVAL on mismatch); a stale C2H is
      silently accepted.
    - `parse_efuse_map` drops `country_code[0/1]` (regd input; wifite drives its own channels).
    - `parse_phycap_map` drops `thermal_trim` (0x1706/0x1733) entirely (docstring "deferred" is
      unbacked); feeds thermal protection + TSSI thermal comp, not RX.
    - 6G gain-offset + tssi sub-bands unparsed (6G only).
    - no zero-MAC fallback (`is_zero_ether_addr` -> random) after efuse read.
    - `mac_pwr_off` does not clear `t.cmac_pwr`; a second cold cycle in one process would skip
      CMAC re-power (ties to the "don't cold-boot a warm chip" theme).
  - VERIFIED-OK (do NOT re-suspect): power_switch/reset_pwr_state/pwr_on_func/pwr_off_func,
    mac_func_en + the 8922A no-op func-ens, the whole partial_init chain, the firmware-download
    completion waits (present), efuse plumbing + struct offsets, read_chip_ver, and the RX-relevant
    CMAC/DMAC init (rx_fltr_init sniffer+UID15, rmac_init, mpdu_proc HDR_SHCUT quirk is real in C,
    cfg_phy_rpt_bands, set_channel_mac 20MHz arm).

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
