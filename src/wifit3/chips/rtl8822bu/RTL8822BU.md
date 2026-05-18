# RTL8822BU — verified facts

Family: rtw88, modern (iDDMA) FW path, NOT 8051. 2T2R, 802.11ac, dual-band.

This doc accumulates facts that have been confirmed wire-side against
`usb_dumps/captures_rtw88_8822bu/capture-1.pcap` AND/OR runtime-tested
end-to-end on the TP-Link Archer T3U Plus v1 (VID:PID 2357:0138, CUT_D,
MP chip, 2T2R). Anything not in this doc should be treated as hypothesis.

Source citations: `[SRC] data_dumps/rtw88-source-v6.18/<file>:<line>`,
`[WIRE] frame N of capture-1.pcap`.

## Device IDs

`USB_IDS_8822BU` in `constants.py` enumerates 25 known VID:PID combos from
`data_dumps/rtw88-source-v6.18/rtw8822bu.c:11` (id_table). The lab device
is `2357:0138` "TP-Link Archer T3U Plus v1".

## USB topology (HS / USB 2.1 enumeration on the dev box)

```
EP 0x84  IN  bulk  512B   — RX data
EP 0x05  OUT bulk  512B   — TX HIGH-priority lane  (BEACON/MGMT/HIGH/H2C qsels)
EP 0x06  OUT bulk  512B   — TX NORMAL lane         (BE/BK qsels)
EP 0x87  IN  int   64B    — interrupt / C2H events
EP 0x08  OUT bulk  512B   — TX LOW lane            (VI/VO qsels)
```

Source: `rtw_usb_parse` (usb.c:238) + `rqpn_table_8822b[3]` (rtw8822b.c:2117).
For 3 bulk-OUTs, `dma_map_hi = dma_map_mg = HIGH` → both BEACON and MGMT
qsels go to `out_ep[0] = 0x05`.

## Chip-ID register (M1)

`REG_SYS_CFG1 (0x00F0)` = `0x0C493D35` on capture-1. Decoded:
- cut_version = `(val >> 12) & 0xF` = 3 = CUT_D
- BIT_RTL_ID (BIT 23) clear → MP chip
- BIT_RF_TYPE_ID (BIT 27) set → 2T2R

`cut_mask_from_sys_cfg1` = `0x1 << (cut + 1)` → 0x10 (= RTW_PWR_CUT_D_MSK).

## FW upload protocol — modern iDDMA (M2 + M3)

- FW file: `rtw88/rtw8822b_fw.bin`, 161240 bytes total
  = 64-byte rtw_fw_hdr + 11216 DMEM (incl 8 chksum) + 149960 IMEM (incl 8 chksum).
- Our pcap-extracted blob (`assets/rtw8822b_fw.bin`) is 161176 bytes
  (header stripped). **Byte-for-byte verified** against
  `linux-firmware/rtw88/rtw8822b_fw.bin[FW_HDR_SIZE:]`.
- DMEM_ADDR = 0x00200000 (BIT(31) masked off from 0x80200000).
- IMEM_ADDR = 0x00000000 (BIT(31) masked off from 0x80000000).
- No EMEM (mem_usage bit 4 = 0).
- Upload path: bulk-OUT EP 0x05 (BEACON qsel via TX desc) chunks → iDDMA
  register triggers (REG_DDMA_CH0SA / DA / CTRL). Each chunk:
  1. Build 48-byte tx_pkt_desc (qsel=BEACON, ls=1, offset=48, tx_pkt_size=N)
  2. Bulk-OUT [tx_desc][chunk] up to 4096+48 bytes (+1 ZLP-avoidance byte
     when (N+48) % 512 == 0 — happens for the partial DMEM-final chunk
     of 3024 bytes → +1 byte → 3025 → URB 3073 bytes)
  3. Poll BIT_BCN_VALID_V1 of REG_FIFOPAGE_CTRL_2
  4. Write src=`OCPBASE_TXBUF_88XX + 48 = 0x18780030`, dst=section+offset,
     ctrl=`OWN | CHKSUM_EN | length [| CHKSUM_CONT if !first]`
  5. Poll BIT_DDMACH0_OWN to clear
- Section boundary: `check_fw_checksum` reads CH0CTRL for CHKSUM_STS;
  if clean, sets `BIT_IMEM_DW_OK | BIT_IMEM_CHKSUM_OK` (or DMEM equiv)
  in REG_MCUFW_CTRL.
- End-of-flow: REG_TXDMA_STATUS = BTI_PAGE_OVF; REG_MCUFW_CTRL |= FW_DW_RDY,
  clear BIT_MCUFWDL_EN.
- `wlan_cpu_enable(true)` then `download_firmware_validate`:
  `(REG_MCUFW_CTRL & FW_READY_MASK=0xCFFF) == (FW_READY=0xC078)`
  where `FW_READY = FW_INIT_RDY | FW_DW_RDY | IMEM_DW_OK | DMEM_DW_OK |
                    IMEM_CHKSUM_OK | DMEM_CHKSUM_OK`.

Runtime budget: ~106 ms end-to-end for FW upload + iDDMA on Windows/WinUSB.

## Critical reg.h bit positions vs the 8821a (legacy) path

Found these differences while bringing up M2 — `chips/rtw88_base/registers.py`
now matches the kernel exactly:

| Bit | Correct | (initial bug had) |
|---|---|---|
| `BIT_HCI_TXDMA_EN` | BIT(0) = 0x01 | BIT(2) — caused FW bulk-OUT to time out |
| `BIT_TXDMA_EN`     | BIT(2) = 0x04 | BIT(3) |
| `BIT_DDMACH0_RESET_CHKSUM_STS` | BIT(25) | BIT(30) |
| `BIT_DDMACH0_CHKSUM_CONT`      | BIT(24) | BIT(28) |
| `BIT_DMEM_CHKSUM_OK` | BIT(6) | BIT(10) |
| `BIT_DMEM_DW_OK`     | BIT(5) | BIT(11) |
| `BIT_IMEM_CHKSUM_OK` | BIT(4) | BIT(12) |
| `BIT_IMEM_DW_OK`     | BIT(3) | BIT(13) |
| `FW_READY`           | 0xC078 (with above) | wrong |

`BIT_HCI_TXDMA_EN` is the load-bearing one — without it the chip silently
refuses bulk-OUT to the TX path, manifesting as `USBTimeoutError`.

## PHY init — minimal port of rtw8822b_phy_set_param (M4)

`phy.py:phy_set_param` does:
1. `REG_SYS_FUNC_EN |= FEN_BB_RSTB | FEN_BB_GLB_RST` — power on BB
2. `REG_RF_CTRL |= RF_EN | RF_RSTB | RF_SDM_RSTB` — power on RF
3. `REG_WLRF1 |= BIT_WLRF1_BBRF_EN`
4. `REG_RXPSEL &= ~BIT_RX_PSEL_RST`
5. Load 5 tables via the phy_cond walker (mac/agc/bb/rf_a/rf_b)
6. `REG_RXPSEL |= BIT_RX_PSEL_RST`

**Skipped intentionally** (not needed for monitor-mode RX): crystal_cap
(needs EFUSE), `config_trx_mode` (rfe-dependent), `rtw_phy_init` (DIG),
`phy_rfe_init`, `pwrtrack_init`, `phy_bf_init`.

`EfuseDefaults` uses `rfe_option=3` (= IFEM with ext, the most common
choice for retail dongles per rtw8822bu.c's id_table). The agc/rf_a/rf_b
tables have IF blocks gated on `rfe ∈ {2, 3, 5, 7, …}`. With rfe=3 the
walker takes the IFEM_EXT branches.

Table sizes (extracted via `scripts/rtl8822bu/extract_init_tables.py`):
- mac: 125 cfg, 0 IF (250 u32)
- agc: 9839 cfg, 9 IF, 138 ELIF, 9 ELSE, 9 ENDIF (20302 u32)
- bb:  1492 cfg, 0 IF (2984 u32)
- rf_a: 3929 cfg, 35 IF, 510 ELIF, 35 ELSE, 35 ENDIF (10178 u32)
- rf_b: 3668 cfg, 23 IF, 346 ELIF, 23 ELSE, 23 ENDIF (8904 u32)

Total runtime ~950 ms on Windows/WinUSB.

## MAC init for RX (M5)

`mac.py:mac_init_for_rx` does the RX-essential subset of `rtw_mac_init`:
- txdma_queue_mapping: REG_TXDMA_PQ_MAP = `0xFA50` (per rqpn_table_8822b[3]),
  REG_CR = 0; REG_CR = MAC_TRX_ENABLE; REG_TXDMA_PQ_MAP |= BIT_RXDMA_ARBBW_EN
- RX filter: REG_RXFLTMAP0 = 0x0FFFFFFF, REG_RXFLTMAP2 = 0xFFFF,
  REG_RCR = 0xE400220E
- drv_info: REG_RX_DRVINFO_SZ = 4, REG_RCR |= BIT_APP_PHYSTS,
  REG_WMAC_OPTION_FUNCTION+4 &= ~(BIT(8)|BIT(9))
- USB burst: REG_RXDMA_MODE = burst_size=512, REG_TXDMA_OFFSET_CHK |= DROP_DATA_EN

After this, REG_CR = `0x000004FF` (low byte has MAC_TRX_ENABLE = 0xFF).

## Priority queue init (needed for MGMT TX — M7)

`mac.py:init_priority_queue_8822b` does `__priority_queue_cfg` for 8822b
on USB with 3 bulk-OUTs:
- `txff_size = 262144`, `page_size = 128` → 2048 pages total
- `rsvd_pg_num = 52`, `acq_pg_num = 1996`, `rsvd_boundary = 1996`
- `page_table[3] = {hq=64, lq=64, nq=64, exq=0, gapq=1}` → `pubq_num = 1803`
- USB-specific: `REG_AUTO_LLT_V1` BIT_MASK_BLK_DESC_NUM = 3 (usb_tx_agg_desc_num)
- Triggers BIT_AUTO_INIT_LLT_V1 and polls for clear

Without this, MGMT bulk-OUT to EP 0x05 stalls (the queue has no pages).

## Channel tune (M6)

`chan.py:set_channel_2g_20mhz` ports `rtw8822b_set_channel` (rtw8822b.c:717):
1. `set_channel_bb_2g_20mhz` — 2G BB pokes (REG_RXPSEL, CCK_CHECK, ENTXCCK,
   RXCCAMSK, ACGG2TBL, CLKTRK=0x96A, TXSF2/6, RFEINV, ADCCLK, ADC160)
2. `set_channel_mac` — `rtw_set_channel_mac` (DATA_SC, RFMOD clear,
   AFE_CTRL1, USTIME_TSF=80, USTIME_EDCA=80, CCK_CHECK clear)
3. `set_channel_rf` — RF18 (band+channel+RFSI+BW), RF_MALSEL with rfbe=0
   for 2G, RF_LUTDBG, RF_XTALX2 toggle; for 2T2R also write RF18 on path B
4. `set_channel_rxdfir` — RX DFIR for BW20 (REG_ACBB0, ACBBRXFIR, TXDFIR)
5. `toggle_igi` — re-arm IGI on both paths; reset REG_RXPSEL byte 0
6. `set_channel_cca_ifem` — CCA thresholds for {1R/2R}×{2G/5G} from
   `cca_ifem_ccut[]` (cca_ifem_ccut col=1 for 2R/2G)
7. `set_channel_rfe_ifem` — RFE/TRSW switch for IFEM 2G

5G path is identical except `set_channel_bb_5g_20mhz` uses different
register values, and `set_channel_rf` looks up rfbe from `LOW_BAND[]`,
`MIDDLE_BAND[]`, `HIGH_BAND[]` tables.

Verified on hardware:
- ch 1 (2.4 GHz): 8 distinct BSSIDs in 8 s (NETGEAR2G, Jubbers, NETGEAR3's
  Megabit LAN Party, Songsong, Castle LeRoy, …)
- ch 36 (UNII-1): 8 distinct BSSIDs in 5 s

## TX inject (M7)

`tx.py:build_tx_desc_mgmt` builds a 48-byte tx_pkt_desc:
- W0[15:0] = tx_pkt_size
- W0[23:16] = offset = 48
- W0[24] = bmc (from addr1[0] I/G bit)
- W0[26] = LS = 1
- W0[31] = DISQSELSEQ = 1
- W1[12:8] = qsel = TX_DESC_QSEL_MGMT (18)
- W1[20:16] = rate_id = RTW_RATEID_B_20M (8) for 2.4 GHz
- W3[8] = use_rate, W3[10] = disdatafb
- W4[6:0] = DATARATE = DESC_RATE1M
- W7[15:0] = checksum (XOR of first 16 u16s)
- W8[15] = en_hwseq

**Difference from 8821a**: `old_datarate_fb_limit = false` (rtw8822b.c:2547)
— so we DO NOT set W4[12:8] = 0x1F like the 8821a does.

Verified: 20/20 broadcast deauth frames bulk-OUT to EP 0x05 in 217 ms.

## Warm reattach

`mac.is_chip_warm`: chip is warm if `(REG_MCUFW_CTRL & FW_READY_MASK)` has
`FW_INIT_RDY | FW_DW_RDY` set AND `REG_CR` has `MACTXEN | MACRXEN` set.
The driver skips bring-up on warm and just resumes USB polling, with a
1.5 s bulk-IN smoke test that surfaces "please replug" if the pipe is
wedged (same lesson as 8821a — `pwr_off_seq` cycle doesn't recover bulk-IN
on Windows/WinUSB).

## What's NOT yet implemented

- EFUSE read (for accurate TX power, BT coex, real `rfe_option`,
  `crystal_cap` fine-tuning).
- `phy_init` DIG / `pwrtrack_init` / `phy_bf_init` / `phy_rfe_init` —
  affect TX quality + adaptive RX gain, not basic RX.
- `set_antenna` API.
- Per-rate TX power tuning from `phy_pg_type{2,3,5}` tables.
- USB 3.0 path (`rtw_usb_switch_mode`) — driver works at USB 2.0 HS.
