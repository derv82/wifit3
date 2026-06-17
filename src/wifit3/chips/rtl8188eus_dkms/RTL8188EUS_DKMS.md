# RTL8188EUS — DKMS (vendor) port

Sibling vendor port of `chips/rtl8188eus/` (mainline). Goal: hotter, **stable** 2.4 GHz
monitor RX than mainline drives the 8188e at — the vendor phydm/ODM RX stack.

## ⚠ Port-completeness audit — RX/waiver/EFUSE axes CLEARED 2026-06-16 (see `SEVERE-AUDIT.md`)

**Severe audit done — verdict: faithful (`SEVERE-AUDIT.md`).** The `verify_pcap` waivers (aireplay
bulk-OUT + `0x4F0`), the no-link phydm watchdog (incl. the 24 SYS_CFG reads + tick boundaries), the RX
decode (**3120/3120** beacons our-decoder-vs-raw over the capture bulk-IN, offline), the RF/BB/AGC init,
and every RX-relevant EFUSE field are reproduced faithfully. **The live ~6.5-vs-~8.9 bcn/s gap is
RF/silicon/environment, not a port defect.** The prior "efuse CONFIRMED gap" is **RX-inert on this card**
(`0xCA=0xFF` blank ⇒ internal LNA = our default) — it stays a *robustness* concern only for a different
8188eus with `0xCA/0xC9/0xB8` programmed. Two **non-default** deferred items remain flagged: the
receiver-blocking NBI notch (arms only with `rtw_adaptivity_en=1`, e.g. ETSI) and powertrack IQK/LCK
(only on ≥8 °C thermal drift). Still un-walked: uncaptured TX-desc variants / 40 MHz / power-save /
sreset-recovery.

`verify_pcap` green and `beacon_watch` healthy do **not** mean this port is faithful. Both gates
have structural blind spots, and we were flying blind to a whole gap class until a question about
the `misc` names accidentally surfaced it. Do not trust this driver on any card / efuse variant /
chip cut / band / mode / code path outside the single 20 MHz-2.4-monitor capture until this is done.

### Why the gates are blind
- **`verify_pcap` is green by construction.** The hardcoded constants (`phy_cond` driver words,
  board / PA-LNA / antenna / channel-plan assumptions) were *tuned to reproduce the recorded wire*.
  You cannot validate a constant against the wire you derived it from. It only catches a wrong value
  that changes a **captured register write**.
- **`beacon_watch` only catches catastrophic RX loss**, on the one channel/scenario tested.

### The poisoned-comment problem (why a naive code-reading audit fails)
Our comments are the porter's assumptions written as fact — e.g. `0xCA[3:2]=iPA+iLNA on all 3 boots`
reads like a measurement but is a *wrong inference* (the byte is blank `0xFF`). An agent that audits
by **reading our code** anchors on these and rubber-stamps them. **The audit must be comment-blind:**
derive expected behaviour from the **kernel source + real chip state** (extract the real efuse /
chip-version from the pcap — never trust the byte a comment claims), then diff our code's *emitted
bytes / computed values* against it. Every `always X / never runs / no-op here / we skip` comment is
a **hypothesis to falsify**, default-assume-wrong until silicon or source proves it.

### Method (tractable + verifiable — not "ask an agent if each function looks right")
1. Walk the kernel **call graph** (`rtl8188eu_hal_init` → stages → helpers → leaves). Straight-line
   table writes are already wire-verified — *not* the risk surface.
2. Risk surface = **leaves that branch on per-card state, or are omitted / `#ifdef`'d / deferred.**
   Classify each: `faithful` / `hardcoded-assumption` / `omitted` / `N/A-this-config`.
3. **Every verdict cites a ground-truth anchor** — real efuse map (pcap), pcap wire, chip-version
   read, or kernel source line. No "looks fine."
4. Prioritise conditional / per-card / runtime (watchdog, channel-set, IQK) over init tables.

### Audit axes
1. **Hardcoded per-card-variable values** the kernel reads/derives (efuse fields, chip cut, board
   type, RFE/PA-LNA, antenna, channel plan, gain offset). Read dynamically, or guard fail-loud.
2. **Collapsed conditionals** — kernel `if/switch` on per-card state where we took only our branch.
3. **Omitted / deferred helpers**, esp. ones with skip-rationale comments. Re-derive from kernel.
4. **Uncaptured code paths** — TX-desc variants, 40 MHz, power-save, sreset/recovery, the runtime
   IQK/LCK/power-track triggers. **No wire ground-truth exists** — source-faithfulness only.
5. **Constants from memory vs source** — verbatim re-grep of every reg addr / `BIT(n)` / magic.
6. **Decomposition boundaries** — intra-stage op misattribution (the single cursor can't see it).

### Confirmed findings
**efuse — CONFIRMED** (real bytes, capture-1, decoded by our own `read_chip_params`): we decode the
full 512-byte logical map but use only 4 fields (MAC, TX power, crystal, thermal) and **hardcode the
rest from this card's values** —

| efuse byte | real | our hardcode | status |
|---|---|---|---|
| `0xCA` PA/LNA (RFE option) | `0xFF` **blank** | internal (iPA+iLNA) | "matches" only via blank→internal default |
| `0xC1` board option | `0x00` | board_type 0 | genuinely matches |
| `0xC9` antenna option | `0x03` **programmed** | single antenna | **ignored, non-default** |
| `0xB8` channel plan | `0xA2` **programmed** | 1–13 | **ignored, non-default** |

Fix = wire from the map we already hold: **decode** the programmed-here fields (channel plan,
antenna) faithfully to the kernel; **fail-loud** (`NotImplementedError` naming the byte) on fields
default/blank here that need un-ported code if non-default (external PA → `PHY_SetRFEReg_8188E`;
board_type≠0 → `phy_cond` driver words; antdiv → `_InitAntenna_Selection`). verify_pcap stays green
for this card by construction; the value is for *other* 8188eus.

**IQK — RESOLVED, faithful (proven on the wire).** Init-time IQK is faithful (kernel's
`HAL_INIT_STAGES_IQK` only flags `neediqk_24g`, no calibration — `usb_halinit.c:1611` — same as us).
The deferred IQK fires in `rtl8188e_PHY_SetSwChnlBWMode` (`phycfg.c:1870`) only when `bNeedIQK &&
neediqk_24g`; `bNeedIQK` is armed by `HW_VAR_DO_IQK` (`hal_com.c:10069`) from **link / AP-start / join
/ sreset** (`rtw_mlme_ext.c`, `rtw_ap.c`, `rtw_sreset.c`) — **never a monitor-mode channel hop.**
Confirmed empirically: the IQK one-shot value (`0xf9000000`/`0xf8000000`, the literal "fire the
calibration" write) appears as a write **zero** times anywhere in the capture — IQK is never
triggered. And the `0xe30–0xe8c` writes that *look* IQK-adjacent are the **BB-config table**, verbatim:
each (addr,value) matches `halhwimg8188e_bb.c` rows (`0xE30,0x1000DC1F`@1581; `0xE40,0x01007C00`@1585;
`0xE68,0x001B25A4`@1594), inside a monotonic `0x0d38→0x0f14→0x0c78` sweep — config our `phy_bb_config`
already reproduces, not a calibration. So the kernel never IQKs in monitor mode and `chan.set_channel`
skipping it is faithful. The dm.py "fires on first link" comment is correct, but verified by the wire,
not trusted. (NB: the full `hal_init` BB/RF config executes *within* the airmon-window timestamps —
it runs at iface bring-up — so register writes there are bring-up config, not airmon-specific.)

**Status:** efuse axis confirmed (fix pending); IQK resolved-faithful; the **runtime DIG/AGC long-run**
(only the first watchdog ticks are wire-verified) is the next RX-perf suspect; axes 2–6 not walked.
Fleet-wide — every driver brought up against one dev card likely shares this pattern; see
`planning/PORTING.md`.

## Why — the A/B that justifies the re-port

Clean fixed-ch1 **passive** reception, canary AP, same physical card
(`beacon_watch_usbcap.py` on the cold-boot captures vs the live mainline port):

| Driver | reception | min | max |
|---|--:|:--:|:--:|
| **DKMS** (vendor — this port's target) | **86–89%** | **7** | 10 |
| mainline kernel | 83% | 5 | 10 |
| our mainline port (`chips/rtl8188eus/`) | ~77% | 3 | 9–10 |

The mainline port is byte-faithful to the mainline kernel and tops out at ~77–83% **with
bad-window collapses (min 3–5)**. The DKMS stack runs ~10 pts hotter with a **tight floor
(min 7 — no collapse)**, from `captures_8188eu/capture-{1,3}` (capture-2 is an RF-moment
outlier at 59%; even its floor, min 4, beats ours). **That floor is the win** — it kills the
bimodal collapse, not just the mean.

## Coordinates

| | |
|---|---|
| Vendor source | `usb_dumps_new/captures_8188eu/driver-source/` — `realtek-rtl8188eus` 5.3.9, module `8188eu` |
| Cold-boot captures | `usb_dumps_new/captures_8188eu/capture-{1,2,3}.pcap` (+ `_logs/main.log`) |
| FW blob in capture | extract from the pcap, byte-verify vs `linux-firmware`, ship in `assets/` |
| VID:PID | `2357:010c` |
| Env var (A/B) | `WIFIT3_RTL8188` — DKMS default, `=mainline` opts back |
| Branch | `dkms/8188eu` |

## Chip facts (decoded from source + wire)

- **RTL8188EUS = 1T1R, 2.4 GHz only.** The capture log records "5GHz Support NOT detected";
  the port is 2.4 GHz / 20 MHz throughout (no 5 GHz milestones, unlike the Jaguar siblings).
- **Vendor request 0x05** [SRC include/usb_ops.h:21] — the same Realtek convention as the
  rest of the family, so the shared `scripts/rtw88_pcap_replay` engine verifies this port too.
- **Config style = phydm `odm_read_and_config`** (`hal/phydm/rtl8188e/halhwimg8188e_{mac,bb,rf}.c`)
  — flat-u32 tables walked by a conditional parser, same extraction shape as `rtl8814au_dkms`.
- **hal_init spine** [SRC usb/usb_halinit.c:1215 `rtl8188eu_hal_init`]: power-on
  (`Rtl8188E_NIC_PWR_ON_FLOW`) → MISC01 queue/page → **FW download** → MAC → BB → RF → LLT →
  MISC02 → turn-on block → security → MISC11 (txpower + RFE) → InitHalDm → IQK/PWtrack/LCK.
- **Cold-boot op stream** (capture-1, dev34): chip-version read (op 0) → power-on (ops 6–40) →
  efuse probe read via packet-buffer indirect (ops 41–551) → **FW download** (ops 552–795) →
  MAC config (op 796+). The efuse read uses REG_PKTBUF_DBG (0x140/0x143/0x144/0x148) after a
  REG_FDHM0 (0x88) autoload poll — **not** REG_EFUSE_CTRL (0x30, never touched).

## Current state

**The whole capture is byte-faithful — `verify_pcap.py` PASSes end-to-end on cap1/2/3** (5740 /
5800 / 5723 ops, every op matched or named-and-counted waived). One cursor walks power-on →
set-macid (init), then dispatches the operational stream to the real handlers, carrying channel +
DM state: RX-BAR enable → per-hop channel tunes → monitor opmode → 22 dynamic-check ticks (the
2 s `rtw_dynamic_chk_wk_hdl`: silent-reset poll + no-link phydm watchdog). RX/TX are HW-proven on a
live TL-WN722N v2 (2357:010c) [HW 2026-06-07]: 78 APs / 940 beacons in a 28 s hop (canary clean);
`deauth_hw.py` landed (target reconnected, 20/20 EAPOL to/from it); promiscuous monitor both
directions (9 M2/M4 ToDS + 11 M1/M3 + 262 ToDS data — no ToDS gap).

We never run airmon/airodump/iw/aireplay against this port; the chip only sees register writes, so
the *vendor-driver* writes those tools trigger are what we reproduce (wifit3's `connect()` /
channel hopper / dig task are the triggers). **Waivers** (separate producers, not the vendor
bring-up; each named + counted in the report): the read-only chip-version probe prologue; the async
`R SYS_CFG/4` 2 s poll; and aireplay-ng's injected TX — its bulk-OUT frames plus the TX-report-timing
write (`REG_TX_RPT_TIME`) its injection triggers. Everything else — including the silent-reset
status poll — is ported and reproduced.

**Top RX-gap lead — now also the faithfulness fix (DONE in code, needs HW A/B):** `monitor.py` used
to write an *ungrounded* `RXFLTMAP0/1 = 0xffff` (accept every control subtype incl. ACK — suspected
bulk-IN flood starving beacons; a write the vendor never made). The wire's real RXFLTMAP1 is
`init_hw_mlme_ext` → `HW_VAR_ENABLE_RX_BAR` = `|= BIT(8)` (BlockAckReq only), and RXFLTMAP0 stays at
reset. The port now does exactly that (`monitor.enable_rx_bar` + `enter_monitor`). The HW A/B
(plug in, `beacon_watch.py`, confirm RX held/improved) is the remaining human gate before flipping
the default. Default stays `WIFIT3_RTL8188=mainline` until that A/B settles the re-port.

**Done since the MISC02 milestone (all pcap-verified ×3 where applicable, unit-tested, committed):**
- **M4a (RfRegChnlVal reads): complete.** `rf.read_rf_chnl_val` ports `phy_RFSerialRead` /
  `PHY_QueryRFReg8188E` — the 3-wire LSSI *read* (stage offset into HSSI param2 0x824/0x82c,
  read back from PI 0x8b8 / serial 0x8a0 per HSSI param1[8]). RfRegChnlVal[0] is the base the
  channel tune RMWs. 11 ops. [WIRE cap1 1573–1583]
- **M4b (_BBTurnOnBlock): complete.** `bb.bb_turn_on_block` — enable CCK(BIT24)+OFDM(BIT25) in
  rFPGA0_RFMOD(0x800), 2 masked RMW. 4 ops. [WIRE 1584–1587]
- **M4c (invalidate_cam_all): complete.** `mac.invalidate_cam_all` — REG_CAMCMD(0x670) =
  CAM_POLLING|CAM_CLR (0xC0000000). 1 op. [WIRE 1588]
- **M5 (PHY_SetTxPowerLevel8188E + efuse PG decode): complete.** `txpower.set_tx_power` ports
  `phy_set_tx_power_level_by_path(RF_PATH_A)` over the CCK/OFDM/HT-MCS0-7 sections (1T1R/2.4G),
  each rate a masked byte RMW of the packed txagc regs. Index = clamp(base + extra_bias, 0,
  0x3F): this build is TXPWR_BY_RATE_EN=0 + TXPWR_LIMIT_EN=0 so by_rate/limit are dead code and
  tpt=0; only the −9 MGN_2M bias survives. base from the efuse PG block (`efuse._parse_tx_power`
  → `TxPwr2G` in `ChipParams`: 6 CCK + 5 BW40 base groups + signed-nibble 1TX diffs). Init
  channel = 6 (`current_channel` default). 40 ops. [WIRE 1589–1628]
- **M6 (MISC11 tail): complete.** `mac.init_misc11_tail` — REG_BAR_MODE_CTRL(0x4cc)=0x0201ffff +
  REG_HWSEQ_CTRL(0x423)=0xFF. `_InitAntenna_Selection` (CONFIG_ANTENNA_DIVERSITY off) and
  `PHY_SetRFEReg_8188E` (efuse RFE option 0xCA is **blank `0xFF`** on all 3 boots → kernel defaults
  to iPA+iLNA → no external-PA writes) are no-ops on this card. 2 ops. [WIRE 1629–1630] ⚠ blank
  DEFAULT, not a confirmed board value — a card with `0xCA` programmed external needs this ported;
  see the port-completeness audit at the top of this doc.
- **M7 (rtl8188e_InitHalDm phydm seed): complete — the RX-critical seed.** `dm.init_hal_dm` ports
  `dm_InitGPIOSetting` + `rtw_phydm_init`→`odm_dm_init`'s register-touching sub-inits: GPIO, the
  DIG IGI read (0xc50→0x20), the IGI-derived NHM env-monitor thresholds (th[0]=(IGI−14)<<1,
  th[i]=th[0]+4i on 0x898/0x89c/0xe28/0x890, CLM period 0x894), adaptivity MAC-EDCCA (0x520),
  and **`phydm_search_pwdb_lower_bound`** — the EDCCA pwdb search: `phydm_set_lna(disable)` (RF
  gain commit) → step the EDCCA L2H/H2L threshold (0xc4c) while counting EDCCA assertions on the
  BB debug port (0x908 select=0x208 / 0xdf4 value, BIT30) 20×/step, `while(is_adjust)` until the
  band reads clear or L2H hits 10 → `phydm_set_lna(enable)` + reset threshold to 0x7f/0x7f. **The
  loop is data-dependent** (the replay serves the dbg-port reads); ported as the real algorithm
  so it reproduces all 3 boots — incl. capture-2's noisier 279-op run vs 235 on cap1/cap3. Adds
  `rf.phy_rf_serial_write` + `rf.set_rf_reg` (PHY_SetRFReg). 235/279/235 ops. [WIRE 1631–1865]
- **M8 (hal_init tail): complete — closes hal_init.** `dm.init_hal_tail` ports the post-InitHalDm
  block [SRC] usb_halinit.c:1597-1633: fw_ractrl-off MAC defaults (Tx-report 0x421/0x4f0,
  early-mode 0x4d3, DROP_DATA_EN 0x20c), the IQK-stage `odm_txpowertracking_check` init pass
  (arm RF_T_METER_NEW 0x42[17:16]=3, defers the thermal read), `_phy_lc_calibrate_8188e` (VCO
  LCK: TXPAUSE-bracketed RF reg18 begin-bit), REG_USB_HRPWM(0xfe58)=0, xmit-ack BIT12. **IQK is
  deferred** (only `neediqk_24g` flagged — runtime fires on first link), matching 8814au_dkms.
  28 fixed ops; the LCK/power-track write values are **read-derived** (replay serves the RF
  reads). `rf.set_rf_reg` merges `(orig & ~mask) | (data << shift)` with **no re-mask** of the
  shifted data (PHY_SetRFReg8188E quirk LCK relies on to set bit15 via a 0xfff call). [WIRE
  1866–1893]
- **M9 (channel tune): complete — byte-diffed.** `chan.set_channel` ports `PHY_SwChnl8188E`
  (TX-power re-tune + RF_CHNLBW channel write) + `PHY_SetBWMode8188E`(20 MHz) (BWOPMODE +
  rFPGA0/1_RFMOD + RF_CHNLBW BW bits). RfRegChnlVal[A] is stateful (channel [9:0], BW [11:10]),
  seeded from M4a; spur cal is I-cut-only (skipped, cut A). `verify_channels.py` byte-diffs the
  initial ch1 set (49 ops) on all 3 captures (RfRegChnlVal 0x07407→0x07c01). The per-hop airodump
  differ (DIG-burst interleave) is deferred to the DIG-watchdog milestone.
- **M10 (monitor-mode entry): complete — byte-faithful, ungrounded over-add removed.** The chip
  enters monitor through two vendor functions (in wire order): `init_hw_mlme_ext` →
  `HW_VAR_ENABLE_RX_BAR` = `RXFLTMAP1 |= BIT(8)` (`monitor.enable_rx_bar`, accept BlockAckReq) then
  the channel tune, and `hw_var_set_opmode(MONITOR)` (`monitor.enter_monitor`): Set_MSR(NOLINK) +
  RCR=0x9000382f (accept-all-physical + append-FCS, no ACRC32/AICV — the 8188e #if 0) +
  RXFLTMAP2=0xffff (data subtypes). RXFLTMAP0 stays at reset (`hal_init` leaves it unwritten). The
  port previously wrote an **ungrounded** `RXFLTMAP0/1 = 0xffff` (accept every control subtype incl.
  ACK) — a write neither the kernel nor the wire makes; removed (faithfulness + the top RX-gap lead,
  since the ACK flood was the suspected beacon-starve). `RXFLTMAP1=0x100` on the wire is decoded as
  `HW_VAR_ENABLE_RX_BAR` [SRC] rtw_mlme_ext.c:1560 / hal_com.c:10257, **not** airmon — so we port it.
  The before/after RX A/B (`beacon_watch.py`) is the remaining human gate.
- **M11 (RX path): complete — HW-proven.** `rx.py` ports `rtl8188e_query_rx_desc_status` (24-byte
  desc) + the `recvbuf2recvframe` walk (_RND4 / RX_AGG_USB) + `decode_rssi` (CCK byte5 →
  lna_gain_table_1[LNA]−2·VGA for cut A; OFDM byte4 → ((pwdb>>1)&0x7f)−110). Deviations: crc/icv
  skip-and-continue, FCS stripped. `transport.bulk_in/out` already had WinUSB-timeout handling.
  Decode unit-tested; [HW] 78 APs / 940 beacons / 1527 frames in a 28 s hop, canary clean.
- **driver + manager: complete.** `Rtl8188eusDkmsDriver` (WlanDriver Protocol): connect()
  orchestrates the full bring-up + monitor + channel tune + RxReaderThread; set_channel threads
  RfRegChnlVal; registered behind `WIFIT3_RTL8188` (mainline default, =dkms opts in). Also fixed
  `firmware.download_firmware` to return a bool (it returned None on success, which connect()
  misread as failure — the bug that initially blocked HW bring-up).
- **M13 (TX wiring): complete — VERIFIED [HW] TX works on the air.** `tx.py` ports
  `rtl8188e_fill_fake_txdesc` (32-byte mgmt desc: OWN|FSG|LSG, OFFSET=32, PKT_SIZE, MGMT queue,
  HW-seq, driver-uses-rate=1M, BMC from addr1) + `rtl8188e_cal_txdesc_chksum`. `driver.inject_frame`
  sends [desc | frame] on bulk-OUT under _io_lock. **[HW] `deauth_hw.py` against a live AP+client
  on ch1: 300 deauth frames injected (no pipe fault), the target client reconnected and 20/20
  captured EAPOL were to/from it — the deauth landed, TX confirmed.** Also proves **full
  promiscuous monitor RX in both directions:** 9 M2/M4 (client->AP, ToDS) + 11 M1/M3 (AP->client,
  FromDS) + 262 ToDS data frames — so client->AP frames not addressed to us are captured (no
  ToDS-filter gap; the crackable WPA M2 is reachable). The whole attack column (deauth, handshake,
  and by the shared `inject_frame`/`WlanInterface` path PMKID/WEP/WPS) is reachable on this port.
- **M12 (phydm watchdog `dig.py` + sreset `sreset.py`): complete — multi-tick gated end-to-end.**
  Each 2 s `rtw_dynamic_chk_wk_hdl` fire (rtw_cmd.c:2737) is reproduced as: the silent-reset status
  poll (`sreset.status_check` — R TXDMA_STATUS/RXDMA_STATUS/FMETHR, recovery branch guarded, never
  fires healthy [SRC] rtl8188e_sreset.c) then the no-link `phydm_watchdog` tick (`dig.watchdog_tick`):
  FA-stats → DIG (IGI clamp [0x1c,0x2a], 0xC50) → CCK-PD 0xa0a → adaptivity EDCCA 0xc4c → thermal
  power-track → NHM/CLM env-monitor. `verify_pcap` carries DM state across **all 22 ticks** of each
  capture byte-faithfully (`WIFIT3_RTL8188_DIG=off` disables the live task). **NHM fix found by the
  gate:** `phydm_nhm_get_result` reads the 12-bin histogram (`0x8d8/0x8dc/0x8d0/0x8d4`) only when the
  report is ready (`0x8b4 BIT17`); the original tick skipped the result reads and diverged on the
  second tick (results ready) — now gated on the ready bit [SRC] phydm_ccx.c:472,506.

Per-milestone detail (early init):

- **M1 (power-on + firmware upload + FW-ready ACK): complete — pcap-verified on all 3 boots.**
  - `pwrseq.power_on` ports `_InitPowerOn_8188EU` = `Rtl8188E_NIC_PWR_ON_FLOW`
    (CARDEMU_TO_ACT: poll 0x06[1] power-ready, RMW 0x02/0x26/0x05×4 + poll 0x05[0], 0x23) then
    REG_CR enable (`0x063F`). [SRC] include/Hal8188EPwrSeq.h, usb/usb_halinit.c:124.
  - `firmware.download_firmware` ports `rtl8188e_FirmwareDownload`: strip the 32-byte header,
    `_FWDownloadEnable`, per-page (4 KB) `_BlockWrite` over EP0 control writes (75×196 B + 66×8 B
    + 2×1 B = 143 writes to the FW SRAM window 0x1000), chksum poll, `_FWFreeToGo` (writes
    MCUFWDL_RDY, runs `_8051Reset88E`, polls WINTINI_RDY). [SRC] rtl8188e_hal_init.c:859.
  - FW blob `assets/rtl8188eufw.bin` = vendor `array_mp_8188e_t_fw_nic[]` (15262 B, sig 0x88E1),
    **byte-identical to linux-firmware `rtl8188eufw.bin`** (SHA256 match). Extracted by
    `scripts/rtl8188eus_dkms/extract_fw.py`.
  - The FW download tail folds in `rtl8188e_InitializeFirmwareVars` (REG_HMETFR 0x1cc <- 0x0f).
- **M2a (MAC register config): complete — pcap-verified on all 3 boots.**
  `mac.phy_mac_config` ports `PHY_MACConfig8188E` [SRC] rtl8188e_phycfg.c:758 — walk
  `array_mp_8188e_mac_reg` (212 u32) through the phydm conditional walker (`phy_cond.walk_table`,
  each taken row an 8-bit write), then REG_MAX_AGGR_NUM (0x4CA) = 0x0707 (USB build's
  MAX_AGGR_NUM=0x07). The conditional 0x040 block is board-type gated; this plain board
  (driver1 = `0x00040200`) takes the ELSE default 0x040=0x00.

- **M2b (BB + AGC config): complete — pcap-verified on all 3 boots.**
  `bb.phy_bb_config` ports `PHY_BBConfig8188E` [SRC] rtl8188e_phycfg.c:964 — enable BB/RF
  (REG_SYS_FUNC_EN |= 0x2003, REG_RF_CTRL=0x07, REG_SYS_FUNC_EN=0x17 for USB), walk
  `array_mp_8188e_phy_reg` (1338 u32) + `array_mp_8188e_agc_tab` (1950 u32) as full-32-bit
  writes (PHY_REG addresses 0xF9–0xFE are settling delays, not writes), then
  `hal_set_crystal_cap` → REG_AFE_XTAL_CTRL(0x24)[22:11] = cap|(cap<<6). **crystal_cap=0x20
  now comes from the decoded efuse** (`efuse.read_chip_params`, see EFUSE milestone) — no
  hardcode. 328 ops/boot (deterministic).
- **EFUSE (probe-phase chip-param read): complete — pcap-verified on all 3 boots.**
  `efuse.read_chip_params` reproduces the probe IOL efuse read (ops 41–544) and decodes
  crystal_cap (0xB9) + MAC (0xD7) from the 512 B logical map; feeds M2b's crystal_cap. Mechanism:
  the IOL engine reads the physical efuse map out of the TX packet buffer (PKTBUF debug regs, see
  Chip facts), then PG-walks it to the 512 B logical map. 504 ops/boot.

- **M2c (RF radio config): complete — pcap-verified on all 3 boots.**
  `rf.phy_rf_config` ports `PHY_RFConfig8188E` -> `PHY_RF6052_Config8188E` (1T1R, path A only):
  store RFENV (query 0x870[bRFSI_RFENV]), set RF_ENV enable/output (0x860), zero the 3-wire
  addr/data bit-length selectors (0x824), walk `array_mp_8188e_radioa` (1228 u32) as LSSI writes
  (`((addr<<20)|(data&0xFFFFF))&0x0FFFFFFF` -> 0x840; 0xFFE/0xF9..0xFD are settling delays),
  then restore RFENV (0x870). 102 ops/boot (6 reads, 96 writes incl. 91 radio-A rows).
  `bb.set_bb_reg`/`query_bb_reg` are the shared masked-BB-register helpers.

- **M2d (EFUSE_PATCH / IOL engine): complete — pcap-verified on all 3 boots.**
  `efuse.iol_efuse_patch` ports `rtl8188e_iol_efuse_patch` (HAL_INIT_STAGES_EFUSE_PATCH) =
  `iol_mode_enable(1)` + `iol_execute(CMD_READ_EFUSE_MAP)` + `iol_execute(CMD_EFUSE_PATCH)` +
  `iol_mode_enable(0)`. The IOL engine (`iol_mode_enable` toggles SW_OFFLOAD_EN 0xF0[7];
  `iol_execute` writes the command to REG_HMEBOX_E0 0x88, polls until it clears, checks the
  <<4 error bit) is the shared MCU-offload primitive (also LLT init, the probe efuse read).
  This build runs `rtw_fw_iol=1` (IOL always on). 393/395/379 ops/boot (the READ_EFUSE_MAP
  poll iterates ~390× per boot).

- **M2e (TX-buffer boundary + LLT table): complete — pcap-verified on all 3 boots.**
  `mac.init_tx_buffer_boundary` (`_InitTxBufferBoundary`: page boundary 0xA8 → BCNQ/MGQ/
  WMAC_LBK/TRXFF/TDECTRL+1) + `mac.init_llt` (`InitLLTTable`, direct non-IOL path — this build
  doesn't define CONFIG_IOL_LLT): chain TX pages 0→1…167→0xFF, ring 168→…→175→0xA8, each entry
  a `_LLTWrite` (REG_LLT_INIT 0x1E0 write + poll-to-idle). 357 ops/boot (176 LLT entries × 2 + 5).

- **M3 (MISC02 'open the MAC'): complete — pcap-verified on all 3 boots.**
  `mac.init_misc02` ports the ~14 hal_init helpers between InitLLTTable and the turn-on block:
  `_InitDriverInfoSize`, `_InitInterrupt` (HISR/HIMR/HIMRE + USB bulk-int sel), `_InitNetworkType`
  (MSR=NT_LINK_AP), `_InitWMACSetting` (STA RCR 0x700060CE + accept-all MAR), `_InitAdaptiveCtrl`
  (RRSR/SIFS/RL), `_InitEDCA`, `_InitRetryFunction`, `InitUsbAggregationSetting` (Tx BLK_DESC +
  **RX_AGG_USB** mode), `InitBeaconParameters_8188e`, MACTXEN/MACRXEN, drop-incorrect-bulkout,
  Tx-report, early-mode-off, MACID-no-link, per-AC lifetime. Chip-state values (RCR, USB-agg)
  resolved to this card's wire-confirmed values. 55 ops/boot. The STA RCR is overwritten by the
  monitor-mode entry (upcoming).

### ⚠️ Two async 2 s producers interleave the EP0 stream (load-bearing for replay)
Background kernel timers fire **every ~2 s** and interleave their transfers into the single
serialized EP0 control stream. Two distinct producers, often confused:

1. **An `R REG_SYS_CFG(0xF0)/4` poll** — first fire ≈ frame 2731 ≈ op 1320 (RF→efuse-patch
   boundary). It interleaves at *arbitrary* points (not lock-serialized with the bring-up), so it
   cannot be positionally dispatched; the verify **filters it out globally** as a named, counted
   waiver (`R 0xF0/4`, 23 polls in cap1). The init thread never does a 32-bit REG_SYS_CFG read —
   `read_chip_version` runs once at probe. M1–M2c passed only because they finish before the first
   fire. (This is **not** the silent-reset timer — that reads TXDMA/RXDMA/FMETHR, see below.)

2. **The `rtw_dynamic_chk_wk_hdl` tick** [SRC] rtw_cmd.c:2737 — one IO-locked burst per ~2 s,
   so it never splits a channel tune. It runs the **silent-reset status poll** (`sreset.status_check`
   — R TXDMA_STATUS 0x210 / RXDMA_STATUS 0x288 / FMETHR 0x1c8) then the **no-link phydm watchdog**
   (`dig.watchdog_tick` — FA 0xC00/0xD00, CCK 0xA2C, NHM 0xF84–0xF94, EDCCA 0x8C4, …). Both are
   vendor code, **reproduced** (not waived): the operational dispatch runs both real handlers at the
   tick opener and carries DM state across all ticks.

### The phydm conditional walker (`phy_cond.py`)
`odm_read_and_config_mp_8188e_*` pairs the flat-u32 table two words at a time: a BIT31 word is a
positive condition (IF/ELSE-IF/ELSE/ENDIF in bits[29:28]); a BIT30 word is its negative pair that
triggers `check_positive`. The 8188e `check_positive` matches four condition words against four
driver words: `driver1` = cut<<24 | (intf&0xF0)<<16 | platform<<16 | package<<12 | (intf&0x0F)<<8
| board_type; `driver2/4` carry the per-path GLNA/GPA/ALNA/APA types (all 0 here). For this card
cut=ODM_CUT_A(0), platform=ODM_CE(0x04), interface=ODM_ITRF_USB(0x02), package=0, board_type=0 →
**driver1 = 0x00040200**. Shared by the MAC/BB/AGC/RF tables (each supplies its own `emit`).

Verified `[SRC]`/`[WIRE]` facts accumulate here as the port progresses.
