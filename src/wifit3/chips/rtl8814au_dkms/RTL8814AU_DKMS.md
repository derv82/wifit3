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
      for this re-port. Not yet ported (RX is a later milestone).
- [ ] **Monitor-mode deviation:** vendor inits for STA/AP; wifite3 is always-monitor
      and will need explicit RCR / RX-filter / address-match rewrites once RX lands.
- [ ] **efuse / MAC address:** probe-phase efuse read is intentionally skipped for
      M1 (not a FW-download prerequisite); `mac_address` is `None` until ported.
- [ ] **TX descriptor (full):** only the beacon-queue FW-download descriptor is
      built so far (see below). Data-frame TX (rates/aggregation/sec) is unported.

## Status
- **M1 (firmware upload + FW-ready ACK): complete — pcap-verified AND hardware-proven.**
- **M2a (MAC register table): complete — pcap-verified AND hardware-proven.**
  `PHY_MACConfig8814`'s 143-entry `array_mp_8814a_mac_reg` applied as a flat
  `write8` loop (`mac.py`); also folds in `FirmwareDownload8814A`'s
  `InitializeFirmwareVars8814` tail (REG_HMETFR 0x1cc <- 0x0f).
- Verification: `scripts/rtl8814au_dkms/verify_pcap.py` replays all three cold
  boots; the port reproduces the USB conversation **byte-for-byte** through the
  latest milestone (646/646/652 ops, all 46 FW packets). [HW] a live ALFA AWUS1900
  reached `CPU_DL_READY` and applied the MAC table via
  `scripts/rtl8814au_dkms/test_hw.py`.
- Not registered in `wlan/manager.py` — master keeps the working mainline
  `rtw88_8814au` until this port is HW-proven to beat it on breadth/stability.

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
(power-on → LLT → FW download → ready) · `driver.py` (WlanDriver Protocol;
channel/RX/TX raise until their milestone). Standalone — does **not** import
`chips/rtw88_base/` (mainline-derived).

## Roadmap (each milestone pcap-diffed before "done"; post-FW init = frames 6668+)
- M2a: `PHY_MACConfig8814` MAC register table. **DONE.**
- M2b: `PHY_BBConfig8814` (BB tables — these DO use phy_cond conditional rows, so
       the BIT31/30 walker is needed here, unlike the flat MAC table).
- M2c: `PHY_RFConfig8814A` (RF tables, per-path).
- M2d: band switch + `rtw_hal_set_chnl_bw(..., CHANNEL_WIDTH_20, ...)` channel tune.
- M3: RX path + `rtl8814_InitHalDm` (DIG/AGC watchdog — confirmed in source and on
      the wire as 103 IGI=0xc50 writes adapting 0x1c..0x2a). The 2.4 GHz breadth
      payoff; finish with a live A/B vs mainline. Monitor-mode RX is just the
      captured RCR (0x608) values — port what's on the wire.
- M4: TX (full `update_txdesc`) for deauth/replay.
