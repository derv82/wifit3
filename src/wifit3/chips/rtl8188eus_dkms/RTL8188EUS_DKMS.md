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

## Status

**NEXT (resume here). The card does 94% with the full vendor DM (capture-proven); the live
busy-band A/B was RF-capped and INCONCLUSIVE; the clean canary A/B is the real test.**
The full no-link `phydm_watchdog` DM tick is byte-faithful ×3 (94/94, `verify_dm_tick`).
- **The card CAN do it:** `beacon_watch_usbcap.py` on capture-{1,3} (vendor driver, full DM,
  fixed-ch1, 15 s) reads **94% / 88% reception** (~9 beacons/s; cap-2 a noisier 71%). So the
  full DM is **correct** for monitor — the airmon capture runs all of it (DIG/CCK-PD/adaptivity/
  thermal/NHM) and performs great. An earlier "monitor doesn't need the DM / DM is
  counterproductive" note was a misread of busy-band noise and is **RETRACTED**.
- **The live busy-band A/B was inconclusive:** DKMS-full-DM ≈ mainline ≈ DKMS-seed-only, all
  RF-capped at ~3.4/s (35%). A busy band caps *every* driver (mainline included), so it can't
  differentiate anything. The "6.5→3" drop was RF, not the code (`--no-dig` is also ~3/s).
- **THE REAL TEST:** the clean canary-AP A/B — one known AP, *quiet* channel, replug between
  runs: `WIFIT3_RTL8188=dkms|mainline … beacon_watch.py --bssid <AP> --channel <quiet>`. Compare
  DKMS to mainline AND to the ~94% capture bar.
    - DKMS reaches ~90% → it reproduces the vendor → flip the default.
    - DKMS < 90% in a *clean* env → a **runtime faithfulness gap** (verify_dm_tick checks one
      tick from documented seeds; the multi-tick DM evolution, the thermal-arm tick, the per-hop
      tunes, and our always-monitor deviation from airmon's STA→monitor dance are all unverified)
      — root-cause it. **Do NOT conclude fall-back from busy-band data.**
- **Remaining verify (full operational Z=0):** the thermal-arm tick (every-other fire) + the
  per-hop channel tunes (model on `rtl8814au_dkms/verify_channels.py`); the guarded thermal
  IQK/LCK deferral (fires at |Δthermal| ≥ 8 °C — port before long TX runs).

**Init + the RX/TX/monitor pipeline are COMPLETE and HW-PROVEN. The operational phydm DM is being
faithfully reconstructed — that, not a mystery register, is the weak-AP gap's root cause.**
[HW 2026-06-07] A live TL-WN722N v2 (2357:010c) brought up clean through the full chain.
- **RX:** the pipeline is clean (instrumented loss-free), but an earlier A/B showed DKMS hearing
  far fewer weak APs than mainline (~22 vs ~70 APs on a busy ch1). **Root cause found:** the
  operational phydm DM (the ~57% of the capture after the monitor entry) was stripped from
  `verify_pcap` and never re-verified, so the runtime port ran only IGI-DIG. Being fixed via
  `verify_dm_tick` — see **Potential Known Gaps → Weak-AP RX sensitivity**. The RX-relevant DM
  fixes (CCK-PD + the adaptivity EDCCA drive) are now ported + live; the HW A/B is the remaining
  gate.
- **TX:** `deauth_hw.py` injected 300 deauth frames (no pipe fault); the target client
  reconnected and **20/20 captured EAPOL were to/from it** — the deauth landed, TX confirmed.
- **Promiscuous monitor (both directions):** 9 M2/M4 (client->AP, ToDS) + 11 M1/M3 + 262 ToDS
  data frames — no ToDS-filter gap, the crackable WPA M2 is reachable; the whole attack column
  (deauth/handshake/PMKID/WEP/WPS via the shared `inject_frame`/`WlanInterface` path) is reachable.

Registered behind `WIFIT3_RTL8188=dkms` (mainline-derived port stays the default for 2357:010c
until a controlled canary-AP A/B confirms a clear RX win on the floor — see the A/B note below).

**Runtime DM watchdog — the FULL no-link `phydm_watchdog` tick (NOT the earlier IGI-only
`dig.py`).** The previous "DIG watchdog DONE, IGI-only, tied with mainline" status was a
**partial port**: it ran only the DIG step and read the FA counters via a non-wire-faithful
shortcut. The runtime DM is now reconstructed faithfully and byte-diffed by
`scripts/.../verify_pcap.py:verify_dm_tick` against one operational `phydm_watchdog` tick (cap1
op 2617+), wire order [SRC] phydm.c:1846-1878: faithful FA-statistics (incl. the CRC/SC/CCA
reads + the EDCCA-flag dbg port) → DIG (carried-state IGI, clamp [0x1c,0x2a], 0xC50) → **CCK-PD**
(0xa0a) → **adaptivity EDCCA** (0xc4c) → **halrf thermal power-track** (`powertrack.py`) →
**NHM/CLM env-monitor**. Driven by a 2 s `connect()` task (`dig.watchdog_tick`, toggle
`driver.enable_dig`). **The full 94-op callback tick is byte-faithful ×3 (Z=0).** Remaining for
full operational Z=0: the thermal-arm tick (every-other fire) + the per-hop channel tunes.

**A/B status / default-flip gate.** The earlier "tie (~7.0 vs 7.3)" A/B was run against the
IGI-only partial port, so it is **superseded** — re-run once the full DM tick is complete. The
re-port's claimed edge (86–89% vs 83%) came from the cold-boot captures in a cleaner setting.
**To flip the default to DKMS, run a controlled canary-AP A/B** (quieter channel, same AP,
replug between runs) and confirm DKMS ties-or-beats on the floor (min). Until then mainline
stays default. The open (a)-vs-(b) question stands — does a byte-faithful full DM beat mainline,
or is "vendor is hotter" false for this card (→ fall back to mainline) — but the port is now
faithful enough to settle it honestly.

**The entire hal_init is ported and byte-for-byte on all 3 captures** (`verify_pcap.py`:
power-on → efuse → MISC01 → FW → MAC → BB → RF → EFUSE_PATCH → LLT → MISC02 → M4a RF-chnl read
→ M4b BB-turn-on → M4c CAM → M5 TX-power → M6 MISC11-tail → M7 InitHalDm seed → M8 hal_init
tail (power-track arm + LCK)). No hardcodes (crystal_cap + tx-power from efuse). Async 2 s
watchdog sreset filtered. 67 hardware-free tests. After M8 the wire hands off to airmon-ng's
monitor + channel setup (cap1 op ~1894), which our always-monitor flow does NOT replay verbatim.

**The runtime path is DONE** (M9 channel tune `verify_channels.py` ×3 + M10 monitor block ×3 +
M11 RX path + driver/manager registration + M13 TX wiring), and 2.4 GHz RX is HW-proven (see
the top of this Status). Only the DIG watchdog (`dig.py`, detailed above) is left.

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
- **M10 (monitor-mode entry): complete — pcap-verified vendor block.** `monitor.enter_monitor`
  ports `hw_var_set_opmode(MONITOR)`: Set_MSR(NOLINK) + RCR=0x9000382f (accept-all + append-FCS,
  no ACRC32/AICV — the 8188e #if 0) + RXFLTMAP2=0xffff. The always-monitor deviation additionally
  opens RXFLTMAP0/1 (mgmt/beacons + control) since hal_init leaves RXFLTMAP0 unwritten and the
  vendor monitor only opens data. `verify_pcap.verify_monitor_block` byte-diffs the 5 vendor ops
  ×3; the 2 RXFLTMAP0/1 opens are the documented additions.
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
- **M12 (DIG/AGC watchdog): complete — HW-validated.** `dig.watchdog_tick` ports the 8188e 11N
  `phydm_dig` no-link path (hold→read→reset FA, step IGI by fa_th, clamp [0x1c,0x2a], write 0xC50),
  driven every 2 s by a `connect()` task (toggle `driver.enable_dig` / `scan_hw --no-dig`). The
  pinned-AP beacon-watch A/B established it is the missing per-AP-reception piece (seed-only ~6.1
  vs mainline 7.3 → with watchdog ~7.0, tied) and that it is healthy (FA bounded/reset works).
  See **Potential Known Gaps → DIG/AGC runtime watchdog**.

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
  crystal_cap (0xB9) + MAC (0xD7) from the 512 B logical map; feeds M2b's crystal_cap.
  See **Potential Known Gaps → EFUSE probe read** for the mechanism. 504 ops/boot.

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
synchronous port never emits these, so `verify_pcap._strip_async_watchdog` removes the
`R 0xF0/4` sreset reads before diffing (the init thread never does a 32-bit REG_SYS_CFG read —
read_chip_version runs once at probe). M1–M2c passed only because they finish before the first
fire. **The per-channel-tune verification (later) must handle the heavier DIG-burst
interleaving**; the DIG burst itself is the runtime DIG-watchdog milestone (cf. `dig.py` in the
siblings).

### The phydm conditional walker (`phy_cond.py`)
`odm_read_and_config_mp_8188e_*` pairs the flat-u32 table two words at a time: a BIT31 word is a
positive condition (IF/ELSE-IF/ELSE/ENDIF in bits[29:28]); a BIT30 word is its negative pair that
triggers `check_positive`. The 8188e `check_positive` matches four condition words against four
driver words: `driver1` = cut<<24 | (intf&0xF0)<<16 | platform<<16 | package<<12 | (intf&0x0F)<<8
| board_type; `driver2/4` carry the per-path GLNA/GPA/ALNA/APA types (all 0 here). For this card
cut=ODM_CUT_A(0), platform=ODM_CE(0x04), interface=ODM_ITRF_USB(0x02), package=0, board_type=0 →
**driver1 = 0x00040200**. Shared by the MAC/BB/AGC/RF tables (each supplies its own `emit`).

  - Verify: `scripts/rtl8188eus_dkms/verify_pcap.py` reproduces power-on (35/39/40 ops — the
    power-ready polls iterate per-boot) + a contiguous FW download (245 ops) + MAC config (93 ops),
    byte-for-byte on capture-{1,2,3}. The efuse probe read between power-on and FW is a later
    milestone (the FW/MAC chain is verified from REG_MCUFWDL via `start_addr`).

## Potential Known Gaps (audit before trusting any milestone)
- [~] **Weak-AP RX sensitivity vs mainline — root cause FOUND, faithful fix in progress.**
      The root cause is NOT a mystery runtime register and NOT the airmon dance: it is that the
      **operational phydm DM — the ~3,256-op / ~57% phase after the monitor entry — was stripped
      from `verify_pcap` and never re-verified**, so the runtime port ran only IGI-DIG (and even
      that sampled the FA counters via a non-wire-faithful shortcut). The previous session's
      register-diff lead (**CCK 0x0a50/0x0a54, OFDM-AGC 0x08c4**) was a **red herring**: 0x8c4 is
      a read-only FA counter the DM *samples* (it reads different because RF state differs), and
      0x0a50/0x0a54 are **never written by the vendor at all** — the vendor adapts a *different*
      set (0xa0a CCK-PD, 0xc4c EDCCA, the NHM thresholds 0x890/0x898/0x89c/0xe28).
      **Fix (byte-faithful, no hardcodes):** `verify_pcap.py:verify_dm_tick` byte-diffs one
      operational `phydm_watchdog` tick (cap1 op 2617+) against `dig.watchdog_tick`. Ported +
      byte-faithful ×3 — the **full 94-op callback tick**: faithful FA-statistics, carried-state
      DIG, **CCK-PD** (0xa0a — the CCK CCA threshold that gates 1 Mbps weak-AP beacons; never ran
      before), **adaptivity EDCCA drive** (0xc4c 0x7f→0x1c/0x24 — the "frozen at the no-link
      seed" bug), the halrf thermal power-track, and **NHM/CLM** (IGI-tracked noise thresholds).
      The two RX-relevant fixes (CCK-PD + EDCCA drive) are already live in the driver.
      This is the methodology PORTING.md now codifies (**"Start from the source … strip, but
      never forget"** — every stripped async stream needs a paired `verify_`). **Remaining gate:**
      the controlled HW A/B (canary floor + busy breadth) to confirm the now-faithful vendor DM
      ties-or-beats mainline. The (a)-vs-(b) question still stands — does a byte-faithful full DM
      beat mainline, or is "vendor is hotter" false for this card (→ fall back to mainline) — but
      the port is finally faithful enough to settle it honestly rather than guess.
- [x] **EFUSE probe read — DONE.** `efuse.read_chip_params` ports the probe-phase IOL efuse
      read (ops 41–544): `iol_mode_enable(1, fw_ready=False)` (incl. the 8051 reset since FW
      isn't up yet) → `iol_execute(READ_EFUSE_MAP)` → `efuse_read_phymap_from_txpktbuf` (read
      the physical map out of the TX packet buffer via PKTBUF debug 0x140/0x143/0x144/0x148) →
      `iol_mode_enable(0)`, then `efuse_phymap_to_logical` (PG-header walk → 512 B logical map).
      Decodes **crystal_cap=0x20** (offset 0xB9 — now fed to M2b, **no more hardcode**) and the
      6-byte MAC (offset 0xD7). 504 ops/boot. Verified byte-for-byte on all 3 boots; tx-power
      (PG block) decode lands with the TX-power milestone.
- [x] **MISC01 queue/page setup — DONE.** `mac.init_misc01` ports the hal_init pre-FW block
      (`_InitQueueReservedPage` RQPN_NPQ=0 / RQPN=0x80A70000, `_InitQueuePriority` TRXDMA map
      0xFAF0, `_InitPageBoundary` RXFF=0x25FF, `_InitTransferPageSize` PBP=0x11). Resolved for
      this card's **single bulk-OUT EP** (coverage audit: only EP 0x02 OUT → all pages public,
      every queue → the one EP). The efuse read tail now also emits `hal_EfusePowerSwitch(OFF)`
      (REG_EFUSE_ACCESS=0). The pre-FW window (power-on→efuse→MISC01) is now contiguous and
      adjacent to the main chain (FW at 0x80) — no gap.
- [ ] **[0..5] chip-version prologue** (read_chip_version 0xf0/4 + `hal_EfusePowerSwitch(ON)`
      0xcf=0x69 + FEN_ELDR/CLK checks): ahead of the 0x06 power-seq start; not yet ported (the
      pre-FW window starts at 0x06). Small, self-contained — port for full op-0 contiguity.
- [~] **Runtime DM watchdog — full no-link `phydm_watchdog` tick (was IGI-only; now byte-diffed).**
      (a) the seed via **InitHalDm** (M7): `dm.init_hal_dm` seeds DIG/NHM/EDCCA incl. the EDCCA
      pwdb search; (b) the periodic tick (`dig.watchdog_tick` + `powertrack.py`): FA-statistics →
      DIG (IGI 0xC50) → CCK-PD (0xa0a) → adaptivity EDCCA (0xc4c) → thermal power-track → NHM,
      a 2 s `connect()` task. **Now byte-diffed** by `verify_dm_tick` (94/94, the full callback
      tick, ×3) —
      the earlier IGI-only watchdog was a partial port that `verify_pcap` simply stripped. The
      runtime tick is the driver's own watchdog AND now a replay-verified stream (the paired
      `verify_` for the stripped async DM, per PORTING.md "strip, but never forget"). thermal
      power-track defers IQK/LCK + the over-swing-limit TX-AGC reset behind explicit guards.

Verified `[SRC]`/`[WIRE]` facts accumulate here as the port progresses.
