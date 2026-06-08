# RTL8188EUS — DKMS (vendor) port

Sibling vendor port of `chips/rtl8188eus/` (mainline). Goal: hotter, **stable** 2.4 GHz
monitor RX than mainline drives the 8188e at — the vendor phydm/ODM RX stack.

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

Init bring-up is byte-faithful: `verify_pcap.py` walks one cursor from power-on through set-macid
(op 2452 of cap1, no gaps — per-milestone record below). RX/TX are HW-proven on a live TL-WN722N
v2 (2357:010c) [HW 2026-06-07]: 78 APs / 940 beacons in a 28 s hop (canary clean); `deauth_hw.py`
landed (target reconnected, 20/20 EAPOL to/from it); promiscuous monitor both directions (9 M2/M4
ToDS + 11 M1/M3 + 262 ToDS data — no ToDS gap).

**Operational frontier** (where the gate now stops): the airmon monitor entry at op 2452
(`RXFLTMAP1`). Beyond it — the interleaved monitor setup, per-hop channel tunes, and the 2 s phydm
watchdog — is dispatched by the rewritten `verify_pcap` but not yet gated end-to-end.

**Top RX-gap lead:** `monitor.enter_monitor`'s `RXFLTMAP0/1 = 0xffff` over-add (see M10) — accepts
all control subtypes incl. ACK; suspected bulk-IN flood starving beacons. Needs the HW A/B. Default
stays `WIFIT3_RTL8188=mainline` until a clean A/B settles the re-port.

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
  `PHY_SetRFEReg_8188E` (efuse RFE option 0xCA[3:2]=iPA+iLNA on all 3 boots → no external
  PA/LNA) are no-ops on this card. 2 ops. [WIRE 1629–1630]
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
- **M10 (monitor-mode entry): vendor block reproduced; RXFLTMAP0/1 over-add is a known divergence.**
  `monitor.enter_monitor` ports `hw_var_set_opmode(MONITOR)`: Set_MSR(NOLINK) + RCR=0x9000382f
  (accept-all + append-FCS, no ACRC32/AICV — the 8188e #if 0) + RXFLTMAP2=0xffff — all on the wire.
  It ALSO writes RXFLTMAP0=0xffff and RXFLTMAP1=0xffff, which neither the kernel `hw_var_set_monitor`
  (writes only RXFLTMAP2) nor the wire does (wire: RXFLTMAP0 never written; RXFLTMAP1=0x100).
  Ungrounded — and RXFLTMAP1=0xffff accepts all control subtypes incl. ACK, which may flood the
  bulk-IN pipe and starve beacons. **Top RX-gap lead; needs the HW A/B (0xffff → 0x100, re-measure).**
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
- **M12 (phydm watchdog `dig.py`): no-link `phydm_watchdog` tick ported.** FA-stats → DIG (IGI
  clamp [0x1c,0x2a], 0xC50) → CCK-PD 0xa0a → adaptivity EDCCA 0xc4c → thermal power-track → NHM,
  driven every 2 s by a `connect()` task (`WIFIT3_RTL8188_DIG=off` disables it). The DIG step/clamp
  matches the vendor wire (its IGI also walks to 0x2a by the {+2,+1,−2} FA steps). The live
  *multi-tick* trajectory and the per-hop channel tunes are NOT yet gated — that is what the new
  `verify_pcap` operational dispatch is for.

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

### ⚠️ Async 2 s watchdog interleaves the EP0 stream (load-bearing for replay)
A background kernel thread (`rtw_dynamic_check_timer` / phydm watchdog) fires **every
2.016 s** (first fire ≈ frame 2731 ≈ op 1320, right at the RF→efuse-patch boundary) and
interleaves its transfers into the single serialized EP0 control stream. Per tick it issues
a **sreset read `R REG_SYS_CFG(0xF0)/4`**; once the chip is up it also runs the full **DIG
burst** (FA counters 0xC00/0xD00, CCK reset 0xA2C, NHM 0xF84–0xF94, EDCCA 0x8C4, …). The
synchronous port never emits these, so the verify waives the sreset read as a named producer
(`R 0xF0/4`, 23 polls in cap1; the init thread never does a 32-bit REG_SYS_CFG read —
read_chip_version runs once at probe). M1–M2c passed only because they finish before the first
fire. The watchdog's DIG burst is a separate producer the operational-phase dispatch handles
(run `dig.watchdog_tick` at each FA-hold).

### The phydm conditional walker (`phy_cond.py`)
`odm_read_and_config_mp_8188e_*` pairs the flat-u32 table two words at a time: a BIT31 word is a
positive condition (IF/ELSE-IF/ELSE/ENDIF in bits[29:28]); a BIT30 word is its negative pair that
triggers `check_positive`. The 8188e `check_positive` matches four condition words against four
driver words: `driver1` = cut<<24 | (intf&0xF0)<<16 | platform<<16 | package<<12 | (intf&0x0F)<<8
| board_type; `driver2/4` carry the per-path GLNA/GPA/ALNA/APA types (all 0 here). For this card
cut=ODM_CUT_A(0), platform=ODM_CE(0x04), interface=ODM_ITRF_USB(0x02), package=0, board_type=0 →
**driver1 = 0x00040200**. Shared by the MAC/BB/AGC/RF tables (each supplies its own `emit`).

Verified `[SRC]`/`[WIRE]` facts accumulate here as the port progresses.
