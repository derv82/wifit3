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
- [ ] **2.4 GHz RX/AGC (the whole point):** the phydm DIG/AGC path is the reason
      for this re-port. Not yet ported (RX is a later milestone). The AGC *table*
      lands at M2b, but the runtime DIG/AGC watchdog is M3.
- [ ] **Monitor-mode deviation:** vendor inits for STA/AP; wifite3 is always-monitor
      and will need explicit RCR / RX-filter / address-match rewrites once RX lands.
      M2b applies the vendor's STA-init `RCR = 0xf40060ce` verbatim (CBSSID match,
      etc.) — monitor mode must overwrite it at the RX milestone.
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
- Verification: `scripts/rtl8814au_dkms/verify_pcap.py` replays all three cold
  boots; the port reproduces the USB conversation **byte-for-byte** through M2e
  (**4334/4334/4340 ops**, all 46 FW packets, BB+RF tables, RCK1 copy, channel tune,
  and the 268-write TX-power table). It first replays the probe-phase efuse read to
  recover the real chip params (rfe_type / crystal_cap / tx_power) and feeds those
  into M2b+, so nothing is hardcoded. [HW] a live ALFA AWUS1900 reached
  `CPU_DL_READY` and applied the full MAC+BB+RF+channel+TX-power init.
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
- **PHY_ConfigBB_8814A** — one masked write: rOFDMCCKEN (0x808)[29:28] = 3 (enable
  OFDM + CCK).
- **PHY_SwitchWirelessBand8814A(2.4G)** — gate the CCK/OFDM clock off (0x1002[0]=0),
  AGC-table select 0x958[4:0]=0, **PHY_SetRFEReg8814A** (rfe=1: the four RFE pinmux
  regs 0xcb0/0xeb0/0x18b4/0x1ab4 = 0x77777777, 0x1abc[27:20]=0x77), rTxPath 0x80c
  [7:4]=2, rCCK_RX 0xa04[27:24]=5, CCK_CHECK 0x454=0, 0xa80[18]=0, BB-swing per path
  (0xc1c/.../0x1a1c[31:21] = 0x200, the 0 dB efuse default), ADC/AGC bw regs, clock on.
- **phy_SwChnl8814A** — band detect (read 0x454, already 2.4G), fc-area 0x860[28:17]
  = 0x96A, per-path RF channel write (RF 0x18, mask 0x703ff, value = channel for
  2.4G), CCK TX-DFIR (0xa20/0xa24/0xa28; ch 1-11 vs 12-13 arms).
- **phy_SetBwMode8814A (20 MHz)** — MAC bw 0x668 clear BIT7|BIT8, secondary-channel
  0x483=0, ADC/AGC bw regs, per-path RF bw (RF 0x18[11:10]=3). phy_ADC_CLK is A-cut
  only (skipped). **Spur cal**: 2.4G has no spur, so reset NBI/CSI (0x87c/0x874/
  0x880/0x884/0x898/0x89c) then disable NBI (0x87c[13]=0).
- **Deferred:** the TX-power table (rtw_hal_set_tx_power_level — 764 writes to 0x1998,
  needs the per-rate power computation) and IQK follow in the vendor flow; both are
  TX/cal concerns. The differ stops exactly at the first 0x1998 write. The per-board
  TxBBSwing efuse decode is likewise deferred (this card uses the 0 dB default).
- **5G** band tune is not ported (`set_channel` accepts 2.4G channels 1-13 only).

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
- **Deferred (M4 TX):** the full `update_txdesc` data-frame TX path. The per-board
  TxBBSwing efuse decode (BB swing in M2d, currently the 0 dB default) also stays here.

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
rfe/xtal/mac decode) · `mac.py` (M2a MAC table + M2b MISC stage) · `phy_cond.py`
(shared phydm conditional-table walker) · `bb.py` (M2b PHY_BBConfig8814) · `rf.py`
(M2c PHY_RFConfig8814A) · `chan.py` (M2d channel tune, 2.4G/20MHz) · `txpower.py`
(M2e per-rate txagc table) · `bb_phy_reg_tbl.py` / `bb_agc_tab_tbl.py` /
`rf_radio_{a,b,c,d}_tbl.py` (generated flat-u32 BB/AGC/RF tables) · `driver.py`
(WlanDriver Protocol; connect() chains EFUSE→M1→M2a..M2e, set_channel hops 2.4G;
RX/TX raise until their milestone). Standalone — does **not** import
`chips/rtw88_base/`.

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
       CHANNEL_WIDTH_20, ...)` channel tune (channel 1). **DONE.** 5G band tune deferred.
- M2e: per-rate TX-power txagc table (0x1998). **DONE.** IQK is skipped at init.
- M3: RX path + `rtl8814_InitHalDm` (DIG/AGC watchdog — confirmed in source and on
      the wire as 103 IGI=0xc50 writes adapting 0x1c..0x2a). The 2.4 GHz breadth
      payoff; finish with a live A/B vs mainline. Monitor-mode RX is just the
      captured RCR (0x608) values — port what's on the wire. The contiguous wire now
      reaches InitHalDm directly: after M2e (TX power) comes a tiny hal_init MISC11
      block (invalidate_cam, HWSEQ_CTRL=0xff @0x423, BAR_MODE @0x4cc, SECONDARY_CCA
      @0x577, 0x652=0) then InitHalDm at frame ~14389 (CCK/AGC regs 0xa00/0xa70/0xa14).
- M4: TX (full `update_txdesc`) for deauth/replay; includes the per-board TxBBSwing
      efuse decode (BB swing currently the 0 dB default).
