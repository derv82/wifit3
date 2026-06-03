# RTL8814AU — vendor (morrownr DKMS) cleanroom port

ALFA AWUS1900, Realtek RTL8814AU 4T4R, USB `0bda:8813`. This is a **fresh port
from the Realtek PHYDM/ODM vendor source** (morrownr `8814au` 5.8.5.1), NOT the
mainline-`rtw88`-derived `chips/rtw88_8814au/`. The two are different codebases;
addresses, init flow, and the firmware-download mechanism all differ. Goal: regain
the vendor driver's 2.4 GHz monitor RX breadth (robust 21–24 APs vs mainline's
noisy 1–11). See `planning/PORTING.md` → "Cleanroom DKMS re-ports".

Sources of truth: the vendor tree at
`usb_dumps_new/captures_rtl8814au/driver-source/` and the cold-boot captures
`usb_dumps_new/captures_rtl8814au/capture-{1,2,3}.pcap`. `[SRC]` cites the vendor
file; `[WIRE]` cites a capture frame range; `[HW]` a hardware run.

## Potential Known Gaps (audit before trusting any milestone)
- [x] **2.4 GHz per-channel spur/NBI — DONE.** `chan._spur_nbi_2g` ports
      `phydm_spur_nbi_setting_8814a` [SRC phydm_rtl8814a.c:47] + `phydm_nbi_setting`: on
      2.4 GHz ch 4-8 (spur 2440 MHz) and ch14 (2480) it computes the per-channel NBI notch
      tap (`_nbi_reg_idx`: fc=2412+(ch-1)*5, tone=(|fc-f_intf|<<5), FFT-128 bin), writes
      0x87c[19:14]=reg_idx and enables NBI 0x87c[13]; every other channel resets + disables
      NBI (the prior behaviour). [WIRE] `verify_channels.py` confirms ALL 2.4 GHz hops
      byte-for-byte across all three captures (ch 4-8 now PASS; reg_idx 19/4/9 for ch4/6/8
      match the wire). This was the same class as the 5 GHz miss — a skip-rationale ("2.4G
      has no spur") that mispredicted, hidden because `verify_pcap` only diffed ch1 (a
      no-spur channel); the per-channel differ is the standing gate that caught + verified
      it. (The two ch1 lines `verify_channels` still flags are non-tune artifacts: init-tail
      windowing on the first ch1, and the 5G→2G band switch on the 165→1 wrap = M5c.)
- [x] **DIG regression — RESOLVED (the controlled A/B clears the watchdog).** The M3c
      watchdog was the prime suspect for a strong-AP RX regression; a fixed-channel
      *and* hopping A/B (DIG ON vs OFF via `scan_hw --no-dig`) refutes it. [HW]:
      - **FA reset works (not the bug).** The raw per-tick `fa_cnt` (DigTick debug line)
        stays bounded and bounces (~2.8k–11k per 2 s window) — it does *not* climb
        monotonically, so the OFDM (0x9a4[17]) + CCK (0xa2c[15]) pulses do clear the
        counters. (M3e adds the third vendor reset pulse — the page-F CCA reset
        `phydm_reset_bb_hw_cnt` 0xb58[0] — for byte-faithfulness with the chip's
        runtime reset, since the cold-boot wire emits all three every FA-stats cycle.)
      - **No strong-AP collapse.** Fixed-ch1 30 s, the strongest on-ch1 AP (~−44 dBm)
        held **150–197 beacons** with DIG ON (vs the earlier uncontrolled "~19"),
        matching DIG OFF (150–185). Across 4 fixed + 2 hop runs the unique-AP and beacon
        breadth track each other within run-to-run variance; a single DIG-OFF outlier
        (5599 frames) drove the original "halved frames" worry and did not repeat.
        Beacon *count* follows beacon interval / multi-BSSID radios, not RSSI, so a
        strong AP ranking below a chatty distant one is expected, not a regression.
      - **IGI 0x2a is faithful + harmless.** In a busy band the no-link FA is genuinely
        > fa_th, so DIG steps IGI to the [0x1c, 0x2a] ceiling (= DIG_MAX_OF_MIN_BALANCE_
        MODE); on a hop scan it also steps *down* (0x2a→0x28→0x26→0x24) on quiet
        channels. 0x2a is the least-sensitive bound but sits well below the strong AP's
        level, so it does not deafen RX. The earlier "collapse" was an uncontrolled
        hop-vs-fixed comparison at a different time. The `--no-dig` toggle + DigTick
        `fa_cnt` log stay in as the standing A/B harness.
- [x] **RX CRC-walk coherence — DONE (M3d).** `rx.iter_frames` now SKIPS a crc/icv-error
      frame and continues the buffer walk (it used to `break` on the first one — the
      vendor STA `goto _exit` for `mp_mode==0`), so good frames aggregated after a
      crc-error frame are delivered instead of dropped. The monitor RCR sets
      `ACRC32|AICV` so such frames legitimately arrive; only a malformed descriptor
      length (no recoverable next-frame boundary) still ends the walk. [HW] no
      regression (26 APs fixed / 67-74 hop, frames within run-to-run variance); the fix
      can only recover dropped frames, never drop good ones, so the benefit is
      environment-dependent (how often a crc-error frame lands mid-aggregate). An
      ESSID-variance canary (`scan_hw.py`: distinct ESSIDs per BSSID — random frame
      corruption would show a dominant correct ESSID plus rare one-off variants) came
      back **clean: 0 variant BSSIDs of 33 (fixed-ch1) / 72 (hop) over ~5750 beacons**,
      confirming the recovered frames are intact and the walk does NOT desync after a
      crc-error frame.
- [x] **2.4 GHz RX/AGC (the whole point):** the phydm DIG/AGC path is the reason for
      this re-port. AGC *table* (M2b) + DIG/AGC *seed* (M3a) + bulk-IN RX path (M3b-3a,
      **69 APs in a 30 s 1-13 hop** [HW]) + per-frame RSSI (M3b-3b) + the runtime
      DIG/AGC watchdog (M3c) are done. The watchdog adapts IGI within [0x1c, 0x2a]; a
      controlled A/B shows no strong-AP or breadth regression (see **DIG regression —
      RESOLVED** above).
- [x] **Monitor-mode deviation — RCR/RX-filter done (M3b-2).** vendor inits for
      STA/AP (M2b applies the STA-init `RCR = 0xf40060ce` + beacon-filtered RXFLTMAP);
      wifit3 is always-monitor, so `monitor.enter_monitor` overwrites RCR with the
      accept-all `0x90003b2f` and opens RXFLTMAP0/1/2 to `0xffff` (+ Set_MSR NOLINK).
      This **diverges from the cold-boot pcap on purpose** — it runs only the vendor
      monitor opmode entry, not airmon's STA→monitor transition. See **M3b**. [HW] the
      filter is **fully promiscuous in both directions** — a live deauth-reconnect test
      captured client→AP (ToDS) frames not addressed to us, incl. WPA handshake M2/M4
      (9 ToDS + 7 FromDS EAPOL, 2064 ToDS data frames). No ToDS-filter gap (the failure
      mode that would break WPA handshake capture).
- [x] **efuse / chip params — ported & verified.** The probe-phase efuse read
      (frames 51–5677, device 51, *outside* the M1+ window) is now ported in
      `efuse.py` and verified byte-for-byte by `verify_efuse_pcap.py`. It decodes
      `rfe_type` (BB walker discriminator), `crystal_cap` (AFE trim), and the
      `mac_address` — all read live from the card, replacing the M2b constants. The
      decode independently yields `rfe_type=1` and `crystal_cap=0x23`, confirming
      the M2b values. cut/package come from `REG_SYS_CFG1` (read, not decoded —
      they don't gate this card's walker; A-cut assumed). See **EFUSE** below.
- [ ] **TX descriptor (full):** only the beacon-queue FW-download descriptor is
      built so far (see below). Data-frame TX (rates/aggregation/sec) is unported.

## Status
- **M1 (firmware upload + FW-ready ACK): complete — pcap-verified AND hardware-proven.**
- **M2a (MAC register table): complete — pcap-verified AND hardware-proven.**
  `PHY_MACConfig8814`'s 143-entry `array_mp_8814a_mac_reg` applied as a flat
  `write8` loop (`mac.py`); also folds in `FirmwareDownload8814A`'s
  `InitializeFirmwareVars8814` tail (REG_HMETFR 0x1cc <- 0x0f).
- **EFUSE (probe-phase chip-param read): complete — pcap-verified AND
  hardware-proven.** `efuse.read_chip_params` (`efuse.py`) reproduces the probe
  efuse read and decodes `rfe_type`, `crystal_cap`, `mac_address`. Wired into
  `connect()` ahead of bring-up (vendor probe order), feeding M2b's BB config. See
  **EFUSE** below.
- **M2b (hal_init MISC stage + PHY_BBConfig8814): complete — pcap-verified AND
  hardware-proven.** Two parts:
  - **MISC stage** (`mac.mac_init_misc`) — the `rtl8814au_hal_init` block between
    PHY_MACConfig and PHY_BBConfig [SRC usb_halinit.c:1168–1198]: queue priority,
    page/driver-info sizes, interrupt mask, network type, WMAC/RCR/EDCA, retry,
    USB aggregation, beacon params, burst length, MACTXEN/MACRXEN.
  - **PHY_BBConfig8814** (`bb.phy_bb_config`) — prefix (SYS_FUNC_EN|FEN_USBA, 0x1002
    BB reset, RF_CTRL0/1/3 power-on), then the two conditional tables applied via
    the phydm walker, then crystal-cap + TRX-path. See **BB config** below.
- **M2c (PHY_RFConfig8814A): complete — pcap-verified AND hardware-proven.**
  `rf.phy_rf_config` walks the four per-path radio tables (radio_a..d) through the
  shared phydm walker, then copies the path-A RCK1 calibration to paths B/C/D. See
  **RF config** below.
- **M2d (channel tune, 2.4 GHz / 20 MHz): complete — pcap-verified AND
  hardware-proven.** `chan.init_tune` runs PHY_ConfigBB_8814A (OFDM+CCK enable),
  PHY_SwitchWirelessBand8814A(2.4G), then phy_SwChnl8814A + phy_SetBwMode8814A +
  spur-cal reset to land on channel 1 @ 20 MHz. `set_channel` hops 2.4G channels.
  See **Channel tune** below.
- **M2e (TX-power table): complete — pcap-verified AND hardware-proven.**
  `txpower.set_tx_power` writes the per-(path,rate) txagc table (0x1998). This build
  compiles with power-by-rate and regulatory-limit **disabled**, so the index
  collapses to `clamp(efuse_base + nTX_diff + 2, 0, 63)`. See **TX power** below.
  (IQK, which follows in the vendor flow, is **skipped at init** — `bNeedIQK` is
  false — so the contiguous wire goes straight from TX power to `rtl8814_InitHalDm`.)
- **M3a (InitHalDm phydm seed): complete — pcap-verified AND hardware-proven.**
  `dm.init_hal_dm` runs the hal_init MISC11 block (CAM clear, HWSEQ/BAR/CCA) then
  `rtl8814_InitHalDm` = `dm_InitGPIOSetting` + `rtw_phydm_init` (the DIG/AGC/
  false-alarm/CCK-PD/adaptivity **seed**). This is the initial state the runtime
  DIG/AGC watchdog (M3c) adapts. See **DM seed** below.
- **M3b-1 (hal_init turn-on tail): pcap-verified AND hardware-proven.**
  `chan.set_rfe_reg_init` = `PHY_SetRFEReg8814A(bInit=TRUE)` (RFE control enable
  `0x1994[3:0]=0xf` + GPIO antenna pinmux `0x42`), then `mac.hal_init_turn_on` =
  the turn-on writes (REG_QUEUE_CTRL, NAV upper, Tx-report, USB mode-switch reset)
  + the efuse MAC programmed to REG_MACID (`0x610`) and read back. See **M3b** below.
- **M3b-2 (monitor opmode entry): pcap-verified AND hardware-proven.**
  `monitor.enter_monitor` = the vendor `hw_var_set_opmode(MONITOR)` = Set_MSR(NOLINK)
  + `hw_var_set_monitor` (RCR `0x90003b2f` accept-all + RXFLTMAP0/1/2 `0xffff`). This
  is the always-monitor deviation; it deliberately skips airmon's STA→monitor dance
  and is verified out-of-line as a targeted 10-op block. See **M3b** below.
  [HW] a live ALFA AWUS1900 ran `connect()` through M3b-2 cleanly (no pipe wedge);
  live RX breadth still depends on M3b-3 (RX path) + M3c (DIG watchdog).
- **M3b-3a (RX path, frames only): ported AND hardware-proven.**
  `rx.py` (24-byte rx desc decode + `recvbuf2recvframe` aggregation walk, FCS
  stripped) + `transport.bulk_in` (bulk-IN probe + read) + the shared
  `RxReaderThread`. Not pcap-diffable; decode unit-tested, end-to-end RX validated
  by a live beacon count (`scan_hw.py`). [HW] a 30 s hop across channels 1-13 heard
  **69 unique APs / 735 beacons / 1315 frames** — strong 2.4 GHz breadth, the payoff
  this re-port exists for. See **M3b** below.
- **M3b-3b (RSSI / PHY-status decode): ported AND hardware-proven.**
  `rx.decode_rssi` derives the per-frame dBm (`recv_signal_power`) from the PHY-status
  struct — CCK via `phydm_cck_rssi_8814a`, OFDM via `((pwdb_all>>1)&0x7f)-110`. [HW] a
  live scan showed a realistic dBm spread — a nearby router at −44 dBm down to distant
  APs near −80 dBm — confirming the byte offsets and the `>>1` (is_mp_chip) branch.
  See **M3b** below.
- **M3c (runtime DIG/AGC watchdog): complete — ported AND validated by a controlled
  A/B.** `dig.watchdog_tick` ports `phydm_dig` for the no-link path — read FA counters
  → step IGI by FA → clamp [0x1c, 0x2a] → write all 4 paths — driven every 2 s by a
  periodic `connect()` task, serialized with `set_channel`. [HW] on a busy band the IGI
  rides up to the 0x2a ceiling and steps back down on quiet channels; a fixed-channel +
  hopping A/B (DIG ON vs OFF) shows no strong-AP or breadth regression and a bounded,
  self-resetting `fa_cnt` — the suspected regression is refuted (see **DIG regression —
  RESOLVED** in Potential Known Gaps). The `--no-dig` toggle + DigTick `fa_cnt` debug
  log are the A/B harness.
- **M3d (RX CRC-walk skip-and-continue): done.** `rx.iter_frames` skips crc/icv-error
  frames instead of ending the buffer walk, so good frames aggregated after a bad one
  are delivered (monitor RCR accepts crc/icv-error frames; only a malformed length ends
  the walk). [HW] beacon scans show no regression (26 APs fixed / 67-74 hop).
- **M3e (DIG CCA-reset faithfulness): done.** `dig._reset_fa_cnt` now emits all three
  vendor FA/CCA reset pulses (OFDM 0x9a4[17] 1->0, CCK 0xa2c[15] 0->1, page-F CCA
  0xb58[0] 1->0), matching the chip's runtime reset. [WIRE] confirmed: the cold-boot
  captures show this exact 6-write group repeating each FA-stats cycle (108/70/88
  0xb58 writes across cap-1/2/3). [HW] no regression (24 APs fixed / 67 hop; fa_cnt
  still bounded/self-resetting; ESSID canary clean).
- **M4a (TX descriptor builder): done.** `tx.build_mgmt_txdesc` ports
  `rtl8814a_fill_fake_txdesc` (minimal self-contained mgmt descriptor) + reuses the
  M1-verified XOR checksum; SET_TX_DESC field positions + QSLT_MGNT/RATEID/DESC_RATE
  constants grepped verbatim. Unit-tested; not yet wired into `connect()` (that is M4b).
- **M4b (inject_frame send path): code + unit tests done; live smoke deferred to M4c.**
  `driver.inject_frame` builds the M4a descriptor (BMC from addr1), prepends it, and
  sends `[desc | frame]` via `transport.bulk_out` under `_io_lock`. Unit-tested with a
  fake transport; no real frame is transmitted until the M4c live test. RX unchanged.
- **M4c (deauth): VERIFIED [HW] — TX works.** `scripts/rtl8814au_dkms/deauth_hw.py`
  (targeted-only, `--dry-run` preview) deauth-and-listens: bursts the bidirectional deauth
  via `inject_frame` over a window while the RX reader runs, and tallies the reconnect's
  4-way handshake. [HW] against a live AP+client on ch1: the deauthed client reconnected
  and **7/7 captured EAPOL frames were to/from the target client** (MAC-confirmed, not
  another station's). The control run with an idle client showed 0/0 — no false positive.
  Confirms the M4a/M4b inject path emits real, effective frames on the air. **Also proves
  full promiscuous monitor RX:** a follow-up run captured the reconnect's **M2/M4
  (client→AP, ToDS) = 9** alongside M1/M3 (AP→client) = 7, plus **2064 ToDS data frames**
  from the air — so RX sees frames not addressed to us in BOTH directions (no ToDS-filter
  gap; the crackable WPA M2 is reachable).
- **M4e (per-board TxBBSwing efuse decode): done.** `efuse._parse_bb_swing_2g` decodes
  byte 0xC6 (2 bits/path, 0xFF->0 dB guard); `chan._set_bb_swing_2g` writes the per-path
  TxScale. Faithful no-op on this card (verify_pcap byte-for-byte all 3 captures; live
  `bb_swing=0x200/0x200/0x200/0x200`); handles a burned fuse on any board.
- **M4d (WEP ARP replay): VERIFIED [HW].** `scripts/rtl8814au_dkms/wep_replay_hw.py` runs
  the stock `WepCampaign` over the dkms driver via a `WlanInterface` — no port-specific
  injection/attack code (replay shares the verified M4c `inject_frame` path). [HW] against
  the WEP test router (ch6): fake-auth associated, the captured ARP was injected and the
  AP **echoed our replay** (`winner=True`, ~3.5k frames injected), generating fresh IVs at
  ~55/s — confirms data-frame injection + replay on this port. (The harness auto-learns
  the target SSID from its beacon; without it the assoc-req carried an empty SSID and the
  AP rejected with status 12 — a harness input gap, not a port bug.)
- Verification: `scripts/rtl8814au_dkms/verify_pcap.py` replays all three cold
  boots; the port reproduces the USB conversation **byte-for-byte** through M3b-1
  (**4451/4451/4457 ops**, all 46 FW packets, BB+RF tables, RCK1 copy, channel tune,
  TX-power table, the InitHalDm phydm seed, and the hal_init turn-on tail). It first
  replays the probe-phase efuse read to recover the real chip params (rfe_type /
  crystal_cap / tx_power) and feeds those into M2b+, so nothing is hardcoded. [HW] a
  live ALFA AWUS1900 reached `CPU_DL_READY` and applied the full init through the
  monitor opmode entry, then received on 2.4 GHz with realistic RSSI (M3b HW-proven;
  M3c watchdog live A/B pending).
- Not registered in `wlan/manager.py` — master keeps the working mainline
  `rtw88_8814au` until this port is HW-proven to beat it on breadth/stability.

## EFUSE — probe-phase chip-param read
`ReadAdapterInfo8814AU` -> `hal_InitPGData_8814A` -> `EFUSE_ShadowMapUpdate` ->
`hal_EfuseReadEFuse8814A` reads the burned-in fuses. Ported in `efuse.py`,
verified byte-for-byte by `verify_efuse_pcap.py` (all three boots, 2814 ops).
- **Per-byte protocol** [WIRE] cap1 frames 51–5677, device 51 (before
  `_InitPowerOn`): each physical byte is a 9-transfer EFUSE_CTRL cycle —
  bank-select (`REG_EFUSE_TEST` 0x34, clear `EFUSE_SEL` for WIFI bank 0), address
  (`REG_EFUSE_CTRL`+1 = addr[7:0], +2[1:0] = addr[9:8]), trigger (+3 bit7→0), poll
  (+3 bit7), data (`REG_EFUSE_CTRL`). Gated by `REG_EFUSE_ACCESS` (0x69 on / 0x00
  off). 312 physical bytes on this card.
- **Header unpacking** [SRC hal_EfuseReadEFuse8814A:1646]: PG blocks (header =
  section offset + 4-bit word-enable; `EXT_HEADER` for offsets ≥ 16) fill
  `eFuseWord[64][4]`, flattened into a 512 B logical map (`section*8 + word*2`).
- **Decoded params**: `rfe_type` = map[0xCA]&0x7F (else 8814AU fallback 1);
  `crystal_cap` = map[0xB9] (else 0x20); `mac_address` = map[0xD8:0xDE]. For this
  card the read yields `rfe_type=1`, `crystal_cap=0x23`, and a valid ALFA MAC,
  independently confirming the values M2b's BB writes implied. `cut`/`package`
  come from `REG_SYS_CFG1` (0xF0, read but not decoded — they don't gate the walker).

## BB config — the phydm conditional walker (M2b)
`phy_BB8814A_Config_ParaFile` [SRC rtl8814a_phycfg.c:381] loads two flat-u32 tables
through the phydm walker [SRC halhwimg8814a_bb.c odm_read_and_config_mp_8814a_*]:
- **PHY_REG** `array_mp_8814a_phy_reg` — 4622 u32 (`bb_phy_reg_tbl.py`).
- **AGC_TAB** `array_mp_8814a_agc_tab` — 6280 u32 (`bb_agc_tab_tbl.py`).
Both extracted 1:1 by `scripts/rtl8814au_dkms/extract_bb_tables.py`. Every data row
is a plain `write32` (`odm_set_bb_reg` with MASKDWORD); neither table contains the
`0xf9..0xfe` delay pseudo-addresses, so there is no delay handling on the wire.

The walker (`bb._walk_table`) pairs the array two u32 at a time. A control word with
BIT31 set is a positive condition (IF/ELSE-IF/ELSE/ENDIF in bits[29:28]); BIT30 is
its negative pair. `check_positive` matches the IF word's low 28 bits against
`driver1` — and for 8814A it compares **only** the cut[27:24], package[15:12],
interface[11:8] nibbles (when non-zero) and the rfe byte[7:0]. driver2/3/4 are
computed in the vendor source but never read, so the port carries only `driver1`.

`driver1 = 0x0F08F201` (cut A→0xF, package 0→0xF, interface USB=0x2, platform CE=0x8,
**rfe_type=1**). `rfe_type` is read from efuse (see above); cut/package are fixed
8814AU/A-cut constants. Empirically, only `rfe_type` selects branches in this
card's taken path — every cut/package combination reproduces the wire identically.
[WIRE] this `driver1` reproduces **all 2102** cold-boot BB writes byte-for-byte.

Suffix: `crystal_cap` packed into 0x2C[26:15] (`cap | cap<<6`, cap=0x23) +
`_rtw_config_trx_path_8814a` CCK path selection (0xa2c, 0xa04).

The walker lives in `phy_cond.py` (shared by BB and RF); it takes an
`emit(addr, value)` callback so each table family supplies its own write action.

## RF config — per-path radio tables (M2c)
`PHY_RFConfig8814A` [SRC rtl8814a_phycfg.c:570] -> `PHY_RF6052_Config_8814A` loads
one conditional radio table per RF path through the same walker (`rf.py`):
- **radio_a..d** `array_mp_8814a_radio{a,b,c,d}` — 4634/4396/4524/4600 u32
  (`rf_radio_{a,b,c,d}_tbl.py`), extracted by `extract_rf_tables.py`. For rfe=1
  the walker takes **1176** writes total. [WIRE] cap1 frames 11335+.
- **RF register access is memory-mapped, not at the RF address.** A write rides the
  per-path LSSI write register (A 0xc90 / B 0xe90 / C 0x1890 / D 0x1A90) as
  `(addr<<20 | data) & 0x0FFFFFFF` [SRC phy_RFWrite_8814A]; a read is a direct
  `read32(base + addr*4)` where base = A 0x2800 / B 0x2c00 / C 0x3800 / D 0x3c00
  [SRC phy_RFRead_8814A] — so the radioa[0] pair `(0x018, 0x13124)` becomes
  `0xc90 <- 0x01813124` on the wire. Pseudo addresses 0xfe/0xffe are 50 ms settling
  delays, not writes (radioa has 3).
- **RCK1 copy**: `read32(0x2870)` (= path-A RF reg 0x1c) then write that value to
  paths B/C/D RF reg 0x1c. The TX-power-tracking table that follows only fills
  software dm arrays (no register I/O), so it is absent from the wire — confirmed by
  the differ landing on PHY_ConfigBB right after the RCK1 copy.

## Channel tune — 2.4 GHz / 20 MHz (M2d)
`chan.py` mirrors the hal_init tail [SRC usb_halinit.c:1229-1237]. 20 MHz primary
only — the 40/80 MHz width math is omitted by scope. [WIRE] cap1 frames 13695-13855.
Every captured per-channel tune is now byte-diffed by `verify_channels.py` (not just
ch1). **[WIRE] scan-mode parity:** the airodump native 250 ms hop issues the SAME full
tune as an explicit `iw set channel` — identical per-channel txpower (0x1998 ~250×),
NBI (0x87c), RF (0xc90), fc-area (0x860) footprint — so this chip has no reduced
"scan-mode" channel-set path, and `set_channel(scan=True)` running the full tune is
faithful (no scan-mode command-skip to mirror).
- **PHY_ConfigBB_8814A** — one masked write: rOFDMCCKEN (0x808)[29:28] = 3 (enable
  OFDM + CCK).
- **PHY_SwitchWirelessBand8814A(2.4G)** — gate the CCK/OFDM clock off (0x1002[0]=0),
  AGC-table select 0x958[4:0]=0, **PHY_SetRFEReg8814A** (rfe=1: the four RFE pinmux
  regs 0xcb0/0xeb0/0x18b4/0x1ab4 = 0x77777777, 0x1abc[27:20]=0x77), rTxPath 0x80c
  [7:4]=2, rCCK_RX 0xa04[27:24]=5, CCK_CHECK 0x454=0, 0xa80[18]=0, BB-swing per path
  (0xc1c/.../0x1a1c[31:21], efuse-decoded per path — M4e; 0x200/0 dB on this card),
  ADC/AGC bw regs, clock on.
- **phy_SwChnl8814A** — band detect (read 0x454, already 2.4G), fc-area 0x860[28:17]
  = 0x96A, per-path RF channel write (RF 0x18, mask 0x703ff, value = channel for
  2.4G), CCK TX-DFIR (0xa20/0xa24/0xa28; ch 1-11 vs 12-13 arms).
- **phy_SetBwMode8814A (20 MHz)** — MAC bw 0x668 clear BIT7|BIT8, secondary-channel
  0x483=0, ADC/AGC bw regs, per-path RF bw (RF 0x18[11:10]=3). phy_ADC_CLK is A-cut
  only (skipped). **Spur/NBI (`_spur_nbi_2g`):** reset the NBI tap + CSI
  (0x87c/0x874/0x880/0x884/0x898/0x89c), then per `phydm_spur_nbi_setting_8814a` either
  set the per-channel notch tap + enable NBI (2.4 GHz ch 4-8 spur 2440, ch14 spur 2480)
  or disable NBI (all other channels). Byte-diffed per channel by `verify_channels.py`.
- **Deferred:** the TX-power table (rtw_hal_set_tx_power_level — 764 writes to 0x1998,
  needs the per-rate power computation) and IQK follow in the vendor flow; both are
  TX/cal concerns. The differ stops exactly at the first 0x1998 write. (The per-board
  TxBBSwing efuse decode is now done in M4e — this card reads the 0 dB default.)
- **5G** band tune is M5 (not yet built): `set_channel` accepts 2.4G channels 1-13 only
  for now. 5 GHz @ 20 MHz parity is scoped in the **M5 — 5 GHz @ 20 MHz** plan.

## TX power — the txagc table (M2e)
`rtw_hal_set_tx_power_level` -> `PHY_SetTxPowerLevel8814` writes a per-(path,rate)
power index into the txagc table at BB reg 0x1998 [SRC PHY_SetTxPowerIndex_8814A]:
`0x00801000 | (path<<8) | hw_rate | (PowerIndex<<24)`. 268 writes = 67/path × 4
(66 rates + MGN_1M written twice). [WIRE] cap1 frames 13843-14377.
- **The decisive build fact:** this morrownr build compiles with
  `CONFIG_TXPWR_BY_RATE_EN=0` and `CONFIG_TXPWR_LIMIT_EN=0` [SRC Makefile/drv_conf.h],
  so `PHY_GetTxPowerByRate` returns 0 and `PHY_GetTxPowerLimit` returns the
  non-binding ceiling. The whole power-by-rate (`phy_reg_pg`) and regulatory-limit
  (`txpwr_lmt`) table machinery is **dead code** — none of it is ported. The index
  collapses to `clamp(pg + (CurrentTxPwrIdx−18=2), 0, 63)`.
- **`pg`** = efuse base for the rate's group + cumulative nTX diff
  [SRC phy_get_pg_txpwr_idx]: CCK rates use the CCK base, everything else the BW40
  base; the channel→group map (`txpower._ch_group_2g`) selects the group. The
  per-path base + signed-nibble nTX diffs are parsed from the efuse PG block
  (`efuse._parse_tx_power`, offsets 0x10/0x3A/0x64/0x8E). For this card every diff
  nets to zero across the txagc rate set, so PowerIndex = base + 2 — but the diff
  accumulation is ported faithfully (channel/efuse general).
- **Empirically confirmed** the EN=0 model against the wire: path A base 0x20 → 0x22,
  path B CCK 0x27 → 0x29 / BW40 0x28 → 0x2a, etc., all matching the captured PP bytes.
- **Deferred (M4 TX):** the full `update_txdesc` data-frame TX path (M4a ports the
  minimal mgmt descriptor). The per-board TxBBSwing efuse decode is done in M4e.

## DM seed — InitHalDm (M3a)
`dm.py` ports the hal_init tail after the channel tune: the MISC11 block
(`invalidate_cam_all` 0x670, HWSEQ 0x423, BAR 0x4cc, SECONDARY_CCA 0x577, 0x652)
then `rtl8814_InitHalDm` [SRC rtl8814a_dm.c:203] = `dm_InitGPIOSetting` (USB) +
`rtw_phydm_init` -> `odm_dm_init`. 91-op seed; [WIRE] cap1 frames 14379-14563.
- **Clean phydm sub-inits** (resolved masks, ported as RMW): CCK 2R-antenna
  (`phydm_config_cck_rx_antenna_init` — 0xa00/0xa70/0xa74/0xa14/0xa20/0xa84), DIG
  (reads IGI at 0xc50), CCK-PD level 0 (0xa0a=0x40), env-monitor (CCX hw-restart +
  **NHM thresholds** 0x998/0x99c/0x9a0/0x994 + CLM 0x990), adaptivity (EDCCA
  0x944/0x8a4/0x520/0x524).
- **NHM thresholds are IGI-derived**: `th[i] = ((IGI − 14) << 1) + 4i`, computed from
  the 0xc50 read (not hardcoded) so they track the seed IGI (0x20 → 0x24,0x28,...).
- **RF AGC gain-table commit**: per-path RF 0xEF page open → RF 0x30/0x31/0x32 rows
  → close (path A gets one extra row), plus a BB rx-gain commit (0x910). These
  values are computed in the RF/AGC path and absent from the static tables, so they
  are reproduced from the wire (deterministic across all three boots). The
  0x1994[3:0]=0xf write that immediately follows on the wire is **not** part of this
  commit — it is the first op of `PHY_SetRFEReg8814A(TRUE)` (M3b-1); the only
  `0x1994` writes in the vendor source are that one and the static BB table's
  `0x1994=0x77` (M2b). (Both `[3]=1` and `[3:0]=0xf` map 0x77→0x7f, which is why the
  original M3a mis-attribution still byte-diffed.) The differ confirms the seed
  byte-for-byte.

## M3b — hal_init turn-on tail + monitor RX
The hal_init tail after `rtl8814_InitHalDm` [SRC usb_halinit.c:1285-1305], then the
open-path MAC config and the airmon monitor-mode transition. [WIRE] cap1 14573+.

**M3b-1 (turn-on tail): ported, pcap-verified.** [WIRE] cap1 frames 14573-14611.
- `chan.set_rfe_reg_init(rfe_type)` = `PHY_SetRFEReg8814A(bInit=TRUE)` [SRC
  rtl8814a_phycfg.c:1026]: `0x1994[3:0]=0xf` (RFE control enable) then the GPIO
  antenna pinmux at `REG_GPIO_IO_SEL_8814A` (0x42) — rfe 1/2 set [23:20]=0xf
  (`|0xf0`), rfe 0 sets [23:22]=0b11 (`|0xc0`). All cases ported; this card is rfe 1.
- `mac.hal_init_turn_on(mac)` [SRC usb_halinit.c:1290-1305 + HW_VAR_MAC_ADDR]:
  `REG_QUEUE_CTRL`(0x4c6)[3]=0 (RTS BW follows CCA/secondary-CCA), HW_VAR_NAV_UPPER →
  `REG_NAV_UPPER`(0x652) = `roundup(WiFiNavUpperUs 30000 / HAL_NAV_UPPER_UNIT 128)` =
  0xeb, `REG_FWHW_TXQ_CTRL+1`(0x421)=0x0f (Tx-report enable), `REG_SDIO_CTRL`(0x70)=0,
  `REG_ACLK_MON`(0x3e)=0, then the efuse MAC (`params.mac_address`, **not** hardcoded)
  written to `REG_MACID`(0x610-0x615) and read back (`set/get_macaddr_port`). The wire
  MAC equals the efuse MAC on all three cards — the differ recovers each card's real
  MAC from its probe efuse read and confirms it byte-for-byte.

**M3b-2 (monitor opmode entry): ported, pcap-verified — with a deliberate
DIVERGENCE from the pcap (documented here and in `monitor.py`).**
`monitor.enter_monitor` = the vendor's `hw_var_set_opmode(_HW_STATE_MONITOR_)`
[SRC rtl8814a_hal_init.c:3222]:
- `Set_MSR(_HW_STATE_NOLINK_)` [SRC rtw_hal_set_msr] — `MSR`(0x102)[1:0]=0
  (hal_init left it at `NT_LINK_AP`; monitor needs NOLINK).
- `hw_var_set_monitor` [SRC rtl8814a_hal_init.c:3155] — back up RCR + RXFLTMAP0/1/2,
  then `REG_RCR`(0x608) = **0x90003b2f** (`RCR_AAP|APM|AM|AB|APWRMGT|ACRC32|AICV|ADF|
  ACF|AMF|APP_PHYST_RXFF|APPFCS` — "receive all type", accept CRC/ICV-error frames,
  append FCS; overwrites the STA `0xf40060ce` from M2b) and `REG_RXFLTMAP0/1/2`
  (0x6a0/0x6a2/0x6a4) = **0xffff** (accept all data/mgmt/ctrl subtypes).

**DIVERGENCE — why this is NOT a contiguous extension of the M3b-1 differ:** the
cold-boot capture was taken under airmon-ng, which drives the *STA-initialised*
driver into monitor through a chain of cfg80211 ioctls. On the wire [WIRE] cap1 ops
4451-4764 that is: enable beacon RX (RXFLTMAP1 0x420→0x520), re-tune the channel
hal_init already tuned, re-program the MAC already written, tear down the STA/AP
beacon function (StopTxBeacon + BCN_CTRL + 0x541/0x542), set opmode NOLINK, and
*then* enter monitor. wifit3 is **always monitor** — it never enters STA mode — so it
runs only the vendor monitor opmode entry (the last 10 ops, 4755-4764) and skips
airmon's STA-mode dance (the ~300 ops 4451-4754). The skipped ops are airmon
artifacts, not vendor monitor init. The monitor RCR/RXFLTMAP **values** are taken
straight from that wire (they are what airmon's working monitor session programmed).
The byte-for-byte differ therefore stops at M3b-1; M3b-2 is verified out-of-line by
`verify_pcap.verify_monitor_block`, a targeted 10-op diff against wire 4755-4764
(anchored on the unique monitor RCR write). Confirmed on all three cold boots.

**M3b-3a (RX path, frames only): ported AND hardware-proven.** [HW] a 30 s hop
across channels 1-13 heard 69 unique APs / 735 beacons / 1315 frames — strong
2.4 GHz breadth. `rx.py` + `transport.bulk_in` + the shared `RxReaderThread`:
- `transport.bulk_in()` probes the interface's bulk-IN endpoint and does one
  blocking read (32 KB buffer ≥ the RX-DMA aggregation ceiling; benign timeouts
  return None). `driver._read_once` calls it on the reader thread.
- `rx.query_rx_desc` decodes the 24-byte RX status desc [SRC rtl8814a_rxdesc.c]:
  `pkt_len`, `crc_err`, `icv_err`, `drvinfo_sz`, `shift_sz`, `physt`, `rpt_sel`.
- `rx.iter_frames` is `recvbuf2recvframe` [SRC usb_ops_linux.c:105]: walks the
  USB-aggregated buffer (`pkt_offset = _RND8(24 + drvinfo_sz + shift_sz + pkt_len)`),
  yields each NORMAL_RX MPDU, skips C2H reports and crc/icv-error frames. **Two
  intentional deviations** (documented in `rx.py`): a crc/icv-error packet is skipped
  but the walk continues (M3d — the vendor STA bails for `mp_mode==0`; monitor must
  keep the good frames aggregated after a bad one), and the 4-byte FCS is stripped (the
  vendor keeps it in monitor; wifit3 delivers FCS-stripped frames —
  [project_rx_frames_include_fcs]).
- `driver._dispatch` parses each frame via `WlanFrameParser` and fans the dict to the
  rx callback (RSSI = placeholder `0`). The reader is started at the end of
  `connect()`; per [project_rx_reader_start_ordering] this start-vs-RX-enable ordering
  is the prime suspect if a cold boot shows no RX.
- **Not pcap-diffable** (RX is environment-dependent); the decode is unit-tested
  (`test_rx.py`), end-to-end RX is validated by a live beacon count via
  `scripts/rtl8814au_dkms/scan_hw.py` (the A/B headline vs mainline).

**M3b-3b (RSSI): ported, unit-tested — live value sanity-check pending.** `rx.py`
`decode_rssi` reads the PHY-status struct (the `drvinfo` region after the desc, when
`physt=1`) for the per-frame `recv_signal_power` (dBm), and `iter_frames` now yields
`(frame, rssi)`. [SRC phydm_phy_sts_jaguar_series_parsing + phydm_cck_rssi_8814a]:
- **OFDM/HT/VHT** (DESC rate > 3): `((pwdb_all >> 1) & 0x7f) - 110`, where `pwdb_all`
  is PHY-status byte 4. 8814AU is a production part (`is_mp_chip`) so it takes the
  `>> 1` branch — not the 8812/8821 raw-pwdb branch. *(The `>> 1` is the one live-
  tunable assumption: if a known AP reads ~2× off, drop it.)*
- **CCK** (DESC rate ≤ 3): the CCK AGC report (byte 5, `cfosho[0]`) splits into
  `lna_idx`/`vga_idx` → `phydm_cck_rssi_8814a` (the `-38/-28/-8/-1 − 2·vga` table).
- Only the combined `recv_signal_power` is computed — per-path gain / EVM / SNR /
  CFO are not needed for the single signal level the UI shows. Decode unit-tested
  (`test_rx.py`); [HW] live values via `scan_hw.py` were realistic — a nearby router
  at −44 dBm down to distant APs near −80 dBm — confirming the offsets + the `>>1`.

## Firmware download — the load-bearing M1 fact
The 8814AU does **not** block-write firmware over EP0. `FirmwareDownload8814A`
[SRC rtl8814a_hal_init.c:669] uses the **3081 IDDMA reserved-page** path
(`HalROMDownloadFWRSVDPage8814A`): the blob streams out as **beacon-queue TX
packets** on bulk EP `0x02` (40-byte TX desc + ≤1488 B payload → 1528 B on the
wire), and the 3081 DDMA channel copies each block from the TX packet buffer into
MCU IMEM/DMEM with a running checksum. The legacy `_WriteFW`/`_BlockWrite`
(`rtw_writeN`) path is dead code for this chip. [WIRE] cap1 frames 5851–6667:
46 bulk packets, 70096 B = 46×40 TXDESC + 68256 B payload.

- **Blob:** `array_mp_8814a_fw_nic` (68320 B) [SRC hal8814a_fw.c]; shipped as
  `assets/rtl8814au_fw.bin` via `scripts/rtl8814au_dkms/extract_fw.py`. There is no
  8814au blob in linux-firmware — the vendor C array is the source of truth. The
  pcap bulk payloads *are* the blob (verified by the replay differ).
- **Header (64 B, 3081):** sig `0x8814`@0, DMEM size u32@36 = 5784, IRAM size
  u32@48 = 62456 [SRC rtl8814a_hal.h GET_FIRMWARE_HDR_*_3081]. Each region gets an
  8-byte checksum dummy: `dmem_pkt=5792`, `iram_pkt=62464`; `+64 hdr = 68320`.
  Downloaded payload = `fw[64:68320]` (DMEM region then IRAM, contiguous).
- **TX descriptor (FW packets):** 40 B. word0 = `PKT_SIZE | OFFSET(0x28) |
  LAST_SEG/OWN(0x84) | BMC(bit24)`; word7[15:0] = checksum = XOR of the first 16
  LE u16 with the field zeroed [SRC rtl8814a_cal_txdesc_chksum]. `BMC = (chunk
  byte[4] & 1)` — the "frame" addr1 LSB; verified 46/46 in all three captures. The
  remaining words are the constant `update_txdesc` output for a QSLT_BEACON mgmt
  frame, byte-stable across all FW packets. (Full `update_txdesc` port = TX milestone.)
- **IDDMA per block** [SRC IDDMADownLoadFW_3081]: CH0SA=`0x187BFB28`
  (TXBUF base + bndy×128 + 40, constant), CH0DA=`OCPBASE_DMEM/IMEM + pkt_offset`,
  CH0CTRL=`CHKSUM_EN|OWN|len` with `CHKSUM_CNT` on every block but each region's
  first. [WIRE] cap1 frames 5857–.

## Bring-up order (M1)
`rtl8814au_hal_init` [SRC usb_halinit.c:968], `rtl8814au_hw_reset` is `#if 0`:
1. `_InitPowerOn_8814AU` — write `0x10C2|=BIT1`; `Rtl8814A_NIC_ENABLE_FLOW`
   power-seq (CARDDIS→CARDEMU→ACT, cut=~TESTCHIP, intf=USB); `REG_CR=0` then
   `REG_CR|=0x063F`; `_InitQueueReservedPage` (FIFOPAGE_INFO/RQPN/page boundaries).
2. `InitLLTTable8814A` — `REG_AUTO_LLT(0x208)|=BIT0`, poll the *pre-write* value
   (so on a cold boot with bit0=0 it does no read-back — ported verbatim).
3. `_InitHardwareDropIncorrectBulkOut_8814A` — `REG_TXDMA_OFFSET_CHK(0x20C)|=BIT9`.
4. `FirmwareDownload8814A` — FWDL-enable, 3081 disable, DDMA reset,
   `HalROMDownloadFWRSVDPage8814A`, 3081 enable, FWDL-disable, `_FWFreeToGo` (poll
   `CPU_DL_READY` = `REG_8051FW_CTRL` bit15). **M1 ends here.**

The probe-phase efuse readout (≈1250 writes) precedes this in the capture but is
**not** a FW-download prerequisite, so M1 skips it; the replay differ starts at the
first `0x10C2` access (the `_InitPowerOn` entry).

## Module layout
`constants.py` (regs/bits/sizes, all grepped verbatim) · `pwrseq.py` (power tables
+ parser) · `transport.py` (PyUSB vendor ctrl 0x05 + bulk OUT) · `firmware.py`
(power-on → LLT → FW download → ready) · `efuse.py` (probe-phase EFUSE read +
rfe/xtal/mac decode) · `mac.py` (M2a MAC table + M2b MISC stage + M3b-1 turn-on
tail / MAC addr) · `phy_cond.py` (shared phydm conditional-table walker) · `bb.py`
(M2b PHY_BBConfig8814) · `rf.py` (M2c PHY_RFConfig8814A) · `chan.py` (M2d channel
tune, 2.4G/20MHz + M3b-1 set_rfe_reg_init) · `txpower.py` (M2e per-rate txagc table)
· `dm.py` (M3a InitHalDm DIG/AGC seed) · `monitor.py` (M3b-2 monitor opmode entry)
· `rx.py` (M3b-3a RX desc decode + walk, M3b-3b RSSI) · `dig.py` (M3c runtime DIG
watchdog) · `bb_phy_reg_tbl.py` /
`bb_agc_tab_tbl.py` / `rf_radio_{a,b,c,d}_tbl.py` (generated flat-u32 BB/AGC/RF
tables) · `transport.py` (+ M3b-3a bulk-IN read) · `driver.py` (WlanDriver Protocol;
connect() chains EFUSE→M1→M2a..M2e→M3a→M3b-1→M3b-2 then starts the RX reader,
set_channel hops 2.4G; TX raises until M4).
Standalone — does **not** import `chips/rtw88_base/`.

## M4 — TX (plan; not yet built)

Goal: implement `Rtl8814auDkmsDriver.inject_frame()` so deauth + replay attacks work.
TX is **passive-by-default sensitive** — it runs only on an explicit higher-layer call,
never at connect/scan time. The executor stays cleanroom (vendor source + this tree
only) and greps every constant verbatim before coding.

**Vendor TX-path map** (confirm each fact against the cited source before coding):
- **TX descriptor = 40 B (0x28)**, same size + XOR checksum as the M1 FW-download
  descriptor — so the known-good FW descriptor (pcap-verified 46/46) is a unit anchor
  for the builder. Word layout: word0 PKT_SIZE[15:0] / OFFSET[23:16]=40 / BMC[24] /
  LAST_SEG[26]; word1 MACID[6:0] / QSEL[12:8] / RATE_ID[20:16] / SEC_TYPE[23:22]=0;
  word3 WHEADER_LEN[4:0] / USE_RATE[8]=1 / DISABLE_FB[10]=1 / NAV_USE_HDR[15]=1; word4
  TX_RATE[6:0]; word5 DATA_BW[6:5] / DATA_SC / DATA_SHORT; word6 SW_DEFINE[11:0]; word7
  CHECKSUM[15:0] + USB_TXAGG_NUM[31:24]. [SRC] include/rtl8814a_xmit.h SET_TX_DESC_*;
  minimal field set = `rtl8814a_fill_fake_txdesc` [SRC rtl8814a_xmit.c:267]; checksum =
  `rtl8814a_cal_txdesc_chksum` [SRC rtl8814a_xmit.c:238] (XOR of the first 16 LE u16
  with the checksum word zeroed — identical to the M1 path).
- **A mgmt/deauth frame**: qsel = QSLT_MGNT (= **0x12**, confirmed in hal_com.h),
  mac_id = RTW_DEFAULT_MGMT_MACID, raid
  by wireless mode, rate 1M (CCK) / 6M (OFDM), no encryption, no aggregation. [SRC]
  update_mgntframe_attrib, core/rtw_mlme_ext.c.
- **USB endpoint**: the MGMT queue maps to `RtOutPipe[0]` — the FIRST bulk-OUT endpoint,
  the SAME pipe M1 streams firmware on (`transport.bulk_out`, EP 0x02). No new endpoint;
  send = build the 40 B desc -> prepend to the frame -> bulk_out. No TX-FIFO enable
  beyond the M2b MISC stage (MACTXEN + queue/page setup already applied) — but verify
  live in M4b. [SRC] hal_com.c dma_mapping + os_dep/.../usb_ops_linux.c ffaddr2pipehdl.

**Milestones (tiny; each tagged HW-FREE/DELEGATABLE or HW-LOOP):**
- **M4a — `tx.py` descriptor builder + unit tests. DONE.** `tx.build_mgmt_txdesc`
  ports `rtl8814a_fill_fake_txdesc` (the minimal self-contained mgmt descriptor) with
  the SET_TX_DESC bit positions grepped verbatim and the M1-verified
  `firmware.txdesc_checksum` reused. **Finding:** the M1 FW-download descriptor is the
  QSLT_BEACON `update_txdesc` path (it carries DISQSELSEQ word0[31], MACID=1, retry-limit
  word4, sw_define word6), *distinct* from `fill_fake_txdesc` — so the cross-check anchor
  is the shared checksum + the shared word0 sub-fields (PKT_SIZE/OFFSET/LAST_SEG), not a
  full byte reproduction. `QSLT_MGNT=0x12`, `RATEID_IDX_B=8`, `DESC_RATE1M=0` confirmed
  from source. Unit-tested (62 tests); not wired into `connect()` yet (M4b), so RX is
  unchanged (beacon scan: 73 APs, ESSID canary clean).
- **M4b — `inject_frame` send path. Code + unit tests DONE; live smoke deferred to M4c.**
  `driver.inject_frame` builds the M4a descriptor (BMC derived from addr1's group bit),
  prepends it, and sends `[desc | frame]` via `transport.bulk_out` (RtOutPipe[0], the
  same pipe as the FW download) under the `_io_lock` (so it never TXes mid-retune).
  `frame_bytes` is the MPDU without FCS (HW appends it). Unit-tested with a fake
  transport (broadcast/unicast BMC, too-short reject). No frame is transmitted until the
  M4c live test (passive-by-default + user at the machine). RX unchanged (beacon scan
  68 APs, ESSID canary clean).
- **M4c — deauth end-to-end. VERIFIED [HW].**
  `scripts/rtl8814au_dkms/deauth_hw.py` drives the dkms driver directly (like scan/test_hw):
  bring up -> tune to the AP channel -> inject a bidirectional targeted deauth burst via
  `inject_frame`. **Targeted-only** (`--client` must be unicast; broadcast/multicast is
  refused so it can't deauth the dev machine) and a `--dry-run` that builds frames +
  brings up + tunes but transmits nothing. Frame construction mirrors `WlanInterface.deauth`
  (FC=0xc0, reason 7). [HW] dry-run validated end-to-end (clean bring-up + tune + correct
  26-byte frames, zero TX). The live "a real client drops" check is the user's on return.
- **M4d — replay TX (ARP/EAPOL). VERIFIED [HW].**
  `scripts/rtl8814au_dkms/wep_replay_hw.py` wraps the dkms driver in a real `WlanInterface`
  and runs the stock `WepCampaign` (fake-auth -> ARP replay -> ChopChop -> PTW crack) — NO
  port-specific injection or attack code; replay injects via the same `inject_frame` path
  as M4c. `--dry-run` validated end-to-end (bring-up + WlanInterface + campaign construct +
  tune + RX->registry, zero TX). The dkms driver is `WlanInterface`-compatible because its
  parsed-dict rx callback carries `"raw"` (the echo oracle's signal). A data-queue/rate
  descriptor variant may be a later replay-*speed* tuning, but is not a correctness blocker
  (replay rides the shared mgmt descriptor at 1M). Live IV/crack run is the user's on return.
- **M4e — per-board TxBBSwing efuse decode. DONE.** `efuse._parse_bb_swing_2g` reads
  byte 0xC6 (2.4G), 2 bits per path -> {0:0x200 (0 dB), 1:0x16A (-3), 2:0x101 (-6),
  3:0x0B6 (-9)}, with the unburned-fuse (0xFF) -> 0 dB guard; `chan._set_bb_swing_2g`
  writes the per-path value into [31:21] of 0xC1C/0xE1C/0x181C/0x1A1C. [SRC]
  EEPROM_TX_BBSWING_2G_8814=0xC6 (hal_pg.h), PHY_GetTxBBSwing_8814A
  (rtl8814a_phycfg.c:762; only the registry-AUTO efuse path, the one the wire takes).
  **Confirmed a no-op on this card** (as predicted): verify_pcap stays byte-for-byte on
  all three captures (decode -> 0x200 = wire) and the live card logs
  `bb_swing=0x200/0x200/0x200/0x200`. The decode now handles a burned fuse on any board.

Sequencing: M4a -> M4b -> (M4c, M4d via the HW loop); M4e is independent. The HW-free
delegatable units are **M4a**, the **M4b code**, and **M4e** (+ its pcap check); gate the
live deauth/replay (M4b smoke, M4c, M4d) on the hardware loop.

## M5 — 5 GHz @ 20 MHz (plan; not yet built)

**Scope correction.** "20 MHz only" was meant to skip the bonded 40/80 MHz channels —
NOT the 5 GHz band. The 8814AU is a dual-band 4T4R card; 5 GHz @ 20 MHz (beacons,
inject, deauth, EAPOL) must reach full parity with 2.4 GHz. M1–M4 are 2.4 GHz-only; M5
adds the missing band. 40/80 MHz stay out of scope. (This is the milestone loosely
called "M6" in passing — it is the next port milestone, M5; driver *registration* in
wlan/manager.py is a separate productization gate, not a numbered milestone.)

**Verification anchor — decide first.** The 2.4 GHz init is byte-diffed against the
cold-boot pcap. The 5 GHz band-switch + channel-tune + TX-power are likewise
DETERMINISTIC, so the faithful anchor is a **5 GHz cold-boot capture** (airmon-ng
bringing the card up / hopping onto a 5 GHz channel) — strongly recommended so
`verify_pcap` can byte-diff M5. Without it we port from the vendor source and validate
live (a 5 GHz beacon count); the 5 GHz channel→RF / fc-area / PG-parse math is intricate
enough that a capture is worth it.

**What we already have (de-risks M5):**
- The init BB/AGC tables are **already 5 GHz-inclusive** — `array_mp_8814a_agc_tab` (M2b)
  carries the 5 GHz rows; the switch just selects them via `0x958[4:0]` (1 = 5GL 36-64,
  2 = 5GM 100-144, 3 = 5GH ≥149). **No new table load.** [SRC halhwimg8814a_bb.c]
- The txagc write (`0x1998` formula) and the `inject_frame`/descriptor path are
  **band-independent** — only the per-rate PowerIndex source + the channel tune change.
- `phy_SetBwMode(20 MHz)` is essentially band-independent (reuse M2d's ADC/AGC bw regs).

**Vendor 5 GHz deltas** (executor confirms every value verbatim — esp. the rfe=1 RFE
values, the RF 0x18 MOD_AG bit decode, and the 5G PG offsets):
- **Band switch 5G** [SRC PHY_SwitchWirelessBand8814A 5G]: rTxPath 0x80c[7:4]=0 (vs 2),
  rCCK_RX 0xa04[27:24]=0xF (vs 5), CCK_CHECK 0x454=0x80 (vs 0), 0xa80[18]=1 (vs 0),
  OFDM-only enable 0x808=0x2 (vs 0x3), the rfe-specific 5G RFE pinmux (PHY_SetRFEReg 5G),
  BB-swing-5G; the 0x958 AGC select is DEFERRED to the channel switch.
- **Channel tune 5G** [SRC phy_SwChnl8814A 5G]: fc-area 0x860[28:17] per sub-band
  (36-48→0x494, 50-64→0x453, 100-116→0x452, 118+→0x412), per-path RF 0x18 = channel[7:0]
  + RF_MOD_AG[18:16] per sub-band, 0x958[4:0] AGC select per sub-band (1/2/3),
  band-detect 0x454; the 2.4G CCK TX-DFIR block (0xa20/24/28) is skipped.
- **5G TX power** [SRC hal_com_phycfg.c 5G PG loader + phy_get_pg_txpwr_idx]: the 5G PG
  block is 24 B/path (14 BW40 group bases + 10 diff bytes, no CCK), following the 2.4G
  block in the per-path stride; a 5G channel→group map (14 UNII groups); an OFDM/HT/VHT
  rate set (no CCK) → ~220 txagc writes/channel.
- **5G TxBBSwing** [SRC PHY_GetTxBBSwing 5G]: efuse byte 0xC7, same 2-bit-per-path decode
  + value table as M4e's 2.4G (0xC6).
- **5G spur (20 MHz)** [SRC phy_SpurCalibration_8814A 5G]: most channels reset NBI/CSI
  (reuse M2d); only ch153 (NBI notch) and ch140 (RFE0-specific 0x82c/0x830) need a real
  notch at 20 MHz — fine-tuning, not a blocker for basic RX.

**Milestones (tiny; each tagged):**
- **M5a — 5G band switch (`chan.switch_wireless_band_5g`). [HW-FREE code, DELEGATABLE.]**
  Port the 5G branch (rfe=1 values verbatim); unit-test the register sequence.
- **M5b — 5G channel tune (`chan` 5G channel select: fc-area + RF 0x18 + 0x958 select,
  skip CCK-DFIR). [HW-FREE code, DELEGATABLE.]** Reuse phy_SetBwMode(20 MHz).
- **M5c — runtime band switching (`driver.set_channel` / `chan.set_channel_bw`).
  [code DELEGATABLE; LIVE validation key.]** Detect a 2.4G↔5G crossing and run the band
  switch (not just the channel tune); extend `SUPPORTED_CHANNELS` to the 5 GHz 20 MHz set
  (36…165). The one genuinely new runtime behavior — the 2.4G-only port never
  band-switched mid-run. **[WIRE] confirmed the vendor band-switches ONLY at 2G↔5G
  crossings** (band-switch signature 0x1002 clk-gate + 0x808 OFDM-only + 0xcb0 RFE pinmux
  is present at ch12→36 and ch165→1, absent on every same-band hop) — so M5c gates the
  band switch on a band *change*, not on every tune (same-band hops stay the pure tune
  that already byte-matches the wire).
- **M5d — 5G TX power (`efuse._parse_tx_power_5g` + `txpower` 5G loop). [HW-FREE code,
  DELEGATABLE.]** Needed for 5G inject/deauth at correct power (RX does not need it).
- **M5e — 5G TxBBSwing (`efuse._parse_bb_swing_5g`, 0xC7). [HW-FREE, DELEGATABLE, trivial.]**
  Mirror M4e.
- **M5f — 5G spur cal 20 MHz (ch153 / ch140 cases). [HW-FREE code, DELEGATABLE, LOW pri.]**

**Sequencing:** M5e anytime (trivial). M5a→M5b→M5c gets **5 GHz RX** (validate: do we see
5 GHz beacons / how many APs on a 5G `scan_hw`). Then M5d gets **5 GHz TX** (deauth/replay
reuse the existing `inject_frame` at 5G power). M5f is polish. Live-validate 5 GHz RX
before trusting 5 GHz TX. HW-free delegatable units: M5a/M5b/M5d/M5e/M5f code + unit
tests; M5c's runtime logic + all on-air validation need the hardware loop (and ideally
the 5 GHz capture).

## Roadmap (each milestone pcap-diffed before "done"; post-FW init = frames 6668+)
- EFUSE: probe-phase chip-param read (rfe_type / crystal_cap / mac_address). **DONE.**
- M2a: `PHY_MACConfig8814` MAC register table. **DONE.**
- M2b: hal_init MISC stage + `PHY_BBConfig8814` — PHY_REG (4622 u32) + AGC_TAB
       (6280 u32) via the phydm walker, prefix/crystal-cap/TRX-path. **DONE.**
       (Chip params now read from efuse. The actual table sizes are 4622/6280 u32,
       not the 2595/3254 originally scoped.)
- M2c: `PHY_RFConfig8814A` — radio_a..d RF tables (1176 writes for rfe=1) + RCK1
       copy, via the shared phy_cond walker. **DONE.**
- M2d: `PHY_ConfigBB_8814A` + 2.4G band switch + `rtw_hal_set_chnl_bw(...,
       CHANNEL_WIDTH_20, ...)` channel tune (channel 1). **DONE.** 5G band tune is M5
       (5 GHz @ 20 MHz — the band M1–M4 left out; see the M5 plan).
- M2e: per-rate TX-power txagc table (0x1998). **DONE.** IQK is skipped at init.
- M3a: hal_init MISC11 + `rtl8814_InitHalDm` (phydm DIG/AGC/false-alarm seed). **DONE.**
- M3b: hal_init continuation + monitor RX. **M3b-1 DONE (pcap-verified):** turn-on
       tail — `PHY_SetRFEReg8814A(TRUE)` (RFE-true @0x42), QUEUE_CTRL @0x4c6, NAV
       upper @0x652, Tx-report @0x421, **MAC address** @0x610-0x615 from efuse.
       **M3b-2 DONE (pcap-verified):** monitor opmode entry `hw_var_set_opmode(MONITOR)`
       = Set_MSR(NOLINK) + RCR @0x608 = 0x90003b2f accept-all + RXFLTMAP0/1/2 @0x6a0-4
       = 0xffff (deliberately skips airmon's STA→monitor dance; see M3b above).
       **M3b-3a DONE (hardware-proven):** bulk-IN RX path — `rx.py` (24-byte desc
       decode + recvbuf2recvframe aggregation walk, FCS stripped) + `transport.bulk_in`
       + shared `RxReaderThread`. Live: 69 APs / 735 beacons / 1315 frames in a 30 s
       1-13 hop. **M3b-3b DONE (hardware-proven):** per-frame RSSI from the PHY-status
       struct (`decode_rssi`; CCK lookup + OFDM pwdb_all); live spread −44 dBm (near)
       to −80 dBm (far). **M3b complete.**
- M3c: the runtime DIG/AGC watchdog (`phydm_dig` → `odm_write_dig` — the 0xc50/E50/
       1850/1A50 IGI writes adapting 0x1c..0x2a). **DONE — ported AND validated by a
       controlled A/B (see Potential Known Gaps "DIG regression — RESOLVED").** `dig.py`
       `watchdog_tick` runs every 2 s — read FA counters (OFDM 0xf48 + CCK 0xa5c) →
       `new_igi_by_fa` (no-link step {+2,+1,−2}, fa_th {2000,4000,5000}) → clamp
       [0x1c,0x2a] → write all 4 paths, via a periodic `connect()` task serialized with
       `set_channel`. [HW] fixed-channel + hopping A/B (DIG ON vs OFF via `--no-dig`):
       no strong-AP or breadth regression, `fa_cnt` bounded/self-resetting, IGI rides to
       0x2a on busy windows and steps back down on quiet ones. **Next (optional):** a
       formal A/B vs mainline for the breadth headline.
- M3d: RX CRC-walk skip-and-continue (`rx.iter_frames`) — skip crc/icv-error frames and
       keep walking; only a malformed descriptor length ends the walk. **DONE**
       (unit-tested; [HW] beacon scans show no regression, 26 APs fixed / 67-74 hop).
- M3e: DIG CCA-reset faithfulness (`dig._reset_fa_cnt`) — add the third vendor reset
       pulse (page-F CCA 0xb58[0]) so all three FA/CCA pulses match the chip's runtime
       reset. **DONE** ([WIRE] the 6-write reset group repeats each FA-stats cycle in
       all three captures; [HW] no regression, fa_cnt still bounded, ESSID canary clean).
- M4: TX — implement `inject_frame` for deauth/replay, broken into tiny milestones
      (M4a builder + M4b send path + M4c/M4d live deauth/replay + M4e TxBBSwing decode).
      See **M4 — TX (plan)** above for the vendor TX-path map, per-milestone scope, and
      which milestones are hardware-free / subagent-delegatable.
- M5: 5 GHz @ 20 MHz parity (beacons/inject/deauth/EAPOL) — the band M1–M4 left out
      ("20 MHz only" meant skip 40/80 MHz, not skip 5 GHz). 5G band switch (M5a) +
      channel tune (M5b) + runtime band-switching (M5c) + 5G TX power (M5d) + 5G
      TxBBSwing (M5e) + 5G spur (M5f). The init AGC/BB tables are already 5G-inclusive
      (selected via 0x958), so no new table load. See **M5 — 5 GHz @ 20 MHz (plan)**
      above; ideally byte-diffed against a fresh 5 GHz cold-boot capture.
