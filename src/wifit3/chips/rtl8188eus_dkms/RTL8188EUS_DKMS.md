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
  `hal_set_crystal_cap` → REG_AFE_XTAL_CTRL(0x24)[22:11] = cap|(cap<<6). **crystal_cap=0x20**
  read from the wire (the masked 0x24 write decodes to field 0x820 = 0x20|0x20<<6); to be
  replaced by the efuse decode. 328 ops/boot (deterministic).

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
- [ ] **EFUSE probe read** (ops 41–545): the packet-buffer indirect read + the REG_FDHM0
      autoload poll are not yet ported. Needed to recover crystal_cap / tx-power / MAC. M2b
      currently hardcodes crystal_cap=0x20 (wire-confirmed); the efuse decode must reproduce it.
      The FDHM0 poll exits on a value-change (0x02→0x00 at the last read), so it IS replayable.
- [ ] **MISC01 queue/page setup** (ops 546–551): between the efuse read and FW download; not
      yet ported (folded into the efuse milestone, since both precede FW on the wire).
- [ ] **DIG/AGC runtime watchdog — NOT YET PORTED (only filtered).** The 2 s phydm watchdog
      is **central to this port's RX goal** (without periodic IGI/gain adaptation the gain
      freezes at the seed → deaf/saturating, the exact 2.4 GHz weakness we re-port to fix).
      Status: `verify_pcap` strips only the per-tick sreset read (`R REG_SYS_CFG/4`); the full
      DIG burst (FA counters 0xC00/0xD00, CCK reset 0xA2C, NHM 0xF84–0xF94, EDCCA 0x8C4) is
      NOT yet reproduced. Plan: (a) port the seed via **InitHalDm** (`rtw_phydm_init`, on the
      wire — upcoming milestone); (b) port the periodic tick as `dig.py` (`phydm_dig` no-link
      path: read FA → step IGI → clamp → reset), driven by a 2 s task like the 8814_dkms.
      **Note:** the watchdog *startup* emits no USB ops (it's a kernel `_set_timer` arming) —
      nothing was skipped on the wire; the wire only shows it firing. Porting it precisely also
      lets the per-channel-tune differ filter the DIG burst cleanly (see the ⚠️ section below).

Verified `[SRC]`/`[WIRE]` facts accumulate here as the port progresses.
