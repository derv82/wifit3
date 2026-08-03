"""RTL8814AU bring-up to FW-ready (M1) — port of the vendor (morrownr) stack.

Mirrors ``rtl8814au_hal_init`` up to and including ``FirmwareDownload8814A``:

    power_on            [SRC] _InitPowerOn_8814AU (usb/usb_halinit.c:214)
    init_llt            [SRC] InitLLTTable8814A   (rtl8814a_hal_init.c:71)
    init_drop_incorrect [SRC] _InitHardwareDropIncorrectBulkOut_8814A
    download_firmware   [SRC] FirmwareDownload8814A (rtl8814a_hal_init.c:669)

The 8814AU does NOT block-write firmware over EP0. ``FirmwareDownload8814A``
streams the blob as beacon-queue TX packets into the TX packet buffer, then uses
the 3081 IDDMA channel to copy each block into MCU IMEM/DMEM with a running
checksum (``HalROMDownloadFWRSVDPage8814A`` + ``IDDMADownLoadFW_3081``). FW-ready
is ``_FWFreeToGo8814A`` polling CPU_DL_READY in REG_8051FW_CTRL.

Every register access here matches the vendor read/write width and ordering, and
reproduces the cold-boot capture byte-for-byte (verify via
``scripts/chips/rtl8814au_dkms/verify_m1_pcap.py``). [WIRE] cap1 frames 5713..6667.
"""
from __future__ import annotations

import struct
import time

from .constants import (
    BIT,
    CPU_DL_READY,
    CR_ENABLE_BITS,
    DDMA_CHKSUM_EN,
    DDMA_CHKSUM_FAIL,
    DDMA_CH_CHKSUM_CNT,
    DDMA_CH_OWN,
    DDMA_LEN_MASK,
    DIS_TSF_UDT,
    DMEM_CHKSUM_OK,
    DMEM_DL_RDY,
    EN_BCN_FUNCTION,
    EPQ_PGNUM,
    FW_CHKSUM_DUMMY_SZ,
    FW_HDR_OFF_DMEM_SZ,
    FW_HDR_OFF_IRAM_SZ,
    FW_HDR_OFF_SIGNATURE,
    FW_HEADER_SIZE,
    FW_SIGNATURE_8814A,
    FWDL_EN_BIT,
    FWDL_EN_KEEP_MASK,
    FWDL_RAM_DL_SEL,
    FWDL_ROM_DL,
    HPQ_PGNUM,
    IMEM_CHKSUM_OK,
    IMEM_DL_RDY,
    LPQ_PGNUM,
    MAX_RSVD_PAGE_BUF,
    MCU_CORE_EN,
    NPQ_PGNUM,
    OCPBASE_DMEM_3081,
    OCPBASE_IMEM_3081,
    OCPBASE_TXBUF_3081,
    PUB_PGNUM,
    REG_8051FW_CTRL,
    REG_AUTO_LLT,
    REG_BCN_CTRL,
    REG_CPU_DMEM_CON,
    REG_CR,
    REG_DDMA_CH0CTRL,
    REG_DDMA_CH0DA,
    REG_DDMA_CH0SA,
    REG_FIFOPAGE_CTRL_2,
    REG_FIFOPAGE_INFO_1,
    REG_FIFOPAGE_INFO_2,
    REG_FIFOPAGE_INFO_3,
    REG_FIFOPAGE_INFO_4,
    REG_FIFOPAGE_INFO_5,
    REG_FWHW_TXQ_CTRL,
    REG_HMETFR,
    REG_MGQ_PGBNDY,
    REG_RQPN_CTRL_2,
    REG_TXDMA_DROP_DATA_EN,
    REG_TXDMA_OFFSET_CHK,
    REG_TXPKTBUF_BCNQ1_BDNY,
    REG_TXPKTBUF_BCNQ_BDNY,
    RQPN_CTRL_2_VALUE,
    RSVD_PAGE_DDMA_PAGE_SIZE,
    TX_PAGE_BOUNDARY,
    TXDESC_SIZE,
    DDMA_RESET,
)
from .pwrseq import NIC_ENABLE_FLOW, PWR_CUT_TESTCHIP, PWR_INTF_USB, run_pwr_seq

# ---------------------------------------------------------------------------
# Beacon-queue TX descriptor for firmware-download packets
# ---------------------------------------------------------------------------

def txdesc_checksum(desc: bytes) -> int:
    """16-bit descriptor checksum: XOR of the first 16 LE u16 (32 bytes).

    [SRC] rtl8814a_cal_txdesc_chksum. The checksum field must be zero while
    summing. The USB MAC drops any TX packet whose descriptor checksum is wrong,
    which is what lets the firmware download recover from bulk-out glitches.
    """
    chk = 0
    for i in range(0, 32, 2):
        chk ^= desc[i] | (desc[i + 1] << 8)
    return chk & 0xFFFF


def build_fw_txdesc(length: int, bmc: bool) -> bytes:
    """Build the 40-byte TX descriptor for one firmware-download beacon packet.

    The blob rides ``dump_mgntframe`` on the beacon queue, so update_txdesc emits
    a fixed QSLT_BEACON management descriptor whose only content-dependent field
    is BMC (set when the "frame" addr1 is multicast). Constant words below are the
    update_txdesc output for this packet class, verified byte-identical across all
    46 FW packets in all three cold boots. The full update_txdesc port (data
    frames, rate/aggregation) belongs to the TX milestone. [WIRE] cap1 5851..6667.
    """
    d = bytearray(TXDESC_SIZE)
    # word0: PKT_SIZE[15:0] | OFFSET=TXDESC_SIZE[23:16] | LAST_SEG+OWN (0x84) | BMC(bit24)
    w0 = (length & 0xFFFF) | (TXDESC_SIZE << 16) | (0x84 << 24)
    if bmc:
        w0 |= 1 << 24
    struct.pack_into("<I", d, 0, w0)
    struct.pack_into("<I", d, 4, 0x00081001)   # MACID=1, QSEL=BEACON(0x10), RATE_ID=8
    if bmc:
        struct.pack_into("<I", d, 8, 0x3F000000)  # BMC-coupled default
    struct.pack_into("<I", d, 12, 0x00000100)   # HWSEQ_EN / DISQSELSEQ
    struct.pack_into("<I", d, 16, 0x001A0000)   # data-rate field default
    struct.pack_into("<I", d, 24, 0x00000001)
    struct.pack_into("<I", d, 32, 0x00008000)
    struct.pack_into("<H", d, 28, txdesc_checksum(d))  # word7[15:0] = chksum
    return bytes(d)


# ---------------------------------------------------------------------------
# Power-on (rtw_hal_power_on -> _InitPowerOn_8814AU)
# ---------------------------------------------------------------------------

def _init_queue_reserved_page(t) -> None:
    """[SRC] _InitQueueReservedPage_8814AUsb (non-WMM config)."""
    t.write32(REG_FIFOPAGE_INFO_1, HPQ_PGNUM)
    t.write32(REG_FIFOPAGE_INFO_2, LPQ_PGNUM)
    t.write32(REG_FIFOPAGE_INFO_3, NPQ_PGNUM)
    t.write32(REG_FIFOPAGE_INFO_4, EPQ_PGNUM)
    t.write32(REG_FIFOPAGE_INFO_5, PUB_PGNUM)
    t.write32(REG_RQPN_CTRL_2, RQPN_CTRL_2_VALUE)
    t.write16(REG_TXPKTBUF_BCNQ_BDNY, TX_PAGE_BOUNDARY)
    t.write16(REG_TXPKTBUF_BCNQ1_BDNY, TX_PAGE_BOUNDARY)
    t.write16(REG_MGQ_PGBNDY, TX_PAGE_BOUNDARY)
    t.write16(REG_FIFOPAGE_CTRL_2, TX_PAGE_BOUNDARY)
    t.write16(REG_FIFOPAGE_CTRL_2 + 2, TX_PAGE_BOUNDARY)


def power_on(t) -> None:
    """[SRC] _InitPowerOn_8814AU. Card-enable power seq + enable MAC DMA/sched."""
    v = t.read8(0x10C2)                       # YX-suggested early write
    t.write8(0x10C2, v | BIT(1))
    # Rtl8814A_NIC_ENABLE_FLOW with cut=~TESTCHIP, intf=USB (fab is ALL throughout).
    run_pwr_seq(t, NIC_ENABLE_FLOW, cut=(~PWR_CUT_TESTCHIP) & 0xFF, intf=PWR_INTF_USB)
    t.write16(REG_CR, 0x0000)                 # suggested by zhouzhou
    v = t.read16(REG_CR)
    t.write16(REG_CR, v | CR_ENABLE_BITS)     # HCI/TX/RX DMA + protocol + sched + sec + caltmr
    _init_queue_reserved_page(t)


def init_llt(t) -> None:
    """[SRC] InitLLTTable8814A — HW auto-init of the link-list table.

    The vendor loop polls the *pre-write* value of REG_AUTO_LLT, so when bit0 was
    already 0 (the normal cold-boot case) it skips the read-back entirely. Ported
    verbatim so the emitted register traffic matches the capture.
    """
    tmp = t.read8(REG_AUTO_LLT)
    t.write8(REG_AUTO_LLT, tmp | BIT(0))
    testcnt = 0
    while tmp & BIT(0):
        tmp = t.read8(REG_AUTO_LLT)
        time.sleep(100e-3)
        testcnt += 1
        if testcnt > 100:
            raise RuntimeError("LLT auto-init did not complete")


def init_drop_incorrect(t) -> None:
    """[SRC] _InitHardwareDropIncorrectBulkOut_8814A (ENABLE_USB_DROP_INCORRECT_OUT)."""
    v = t.read32(REG_TXDMA_OFFSET_CHK)
    t.write32(REG_TXDMA_OFFSET_CHK, v | REG_TXDMA_DROP_DATA_EN)


# ---------------------------------------------------------------------------
# Firmware download (FirmwareDownload8814A -> HalROMDownloadFWRSVDPage8814A)
# ---------------------------------------------------------------------------

def _fwdl_enable(t, enable: bool) -> None:
    """[SRC] _FWDownloadEnable_8814A."""
    if enable:
        v = t.read16(REG_8051FW_CTRL)
        v &= FWDL_EN_KEEP_MASK
        v &= ~FWDL_ROM_DL
        v |= FWDL_EN_BIT
        v |= FWDL_RAM_DL_SEL
        t.write16(REG_8051FW_CTRL, v)
    else:
        v = t.read8(REG_8051FW_CTRL)
        t.write8(REG_8051FW_CTRL, v & ~FWDL_RAM_DL_SEL)


def _mcu_core(t, enable: bool) -> None:
    """[SRC] _3081Enable/_3081Disable8814A — REG_SYS_FUNC_EN+1 bit2."""
    v = t.read8(0x0003)
    if enable:
        t.write8(0x0003, v | MCU_CORE_EN)
    else:
        t.write8(0x0003, v & ~MCU_CORE_EN)


def _ddma_reset(t) -> None:
    """[SRC] FirmwareDownload8814A DDMA reset (MAC yodar)."""
    v = t.read32(REG_CPU_DMEM_CON)
    t.write32(REG_CPU_DMEM_CON, v & ~DDMA_RESET)
    t.write32(REG_CPU_DMEM_CON, v | DDMA_RESET)


def _download_blocks(fw: bytes, dmem_pkt_size: int, iram_pkt_size: int):
    """Yield (data, dst_addr, fs, ls) per FW block — DMEM region then IRAM region.

    [SRC] HalROMDownloadFWRSVDPage8814A. DMEM is fw[64:64+dmem], IRAM is the
    tail fw[len-iram:len] (the two are contiguous). Each region is chunked into
    MAX_RSVD_PAGE_BUF blocks; the vendor's 64-byte-alignment ``-= 4`` shave on the
    penultimate block is reproduced even though our blob never triggers it.
    """
    for base_off, base_dst, total in (
        (FW_HEADER_SIZE, OCPBASE_DMEM_3081, dmem_pkt_size),
        (len(fw) - iram_pkt_size, OCPBASE_IMEM_3081, iram_pkt_size),
    ):
        remaining = total
        pkt_offset = 0
        while remaining > 0:
            if remaining > MAX_RSVD_PAGE_BUF:
                block = MAX_RSVD_PAGE_BUF
                ls = False
                last = remaining - MAX_RSVD_PAGE_BUF
                if last < MAX_RSVD_PAGE_BUF and ((last + 40) & 0x3F) == 0:
                    block -= 4
            else:
                block = remaining
                ls = True
            fs = pkt_offset == 0
            src = fw[base_off + pkt_offset:base_off + pkt_offset + block]
            yield src, base_dst + pkt_offset, fs, ls
            remaining -= block
            pkt_offset += block


def _wait_rsvd_page_ok(t) -> None:
    """[SRC] WaitDownLoadRSVDPageOK_3081 — poll beacon-valid, then clear it."""
    bcn_valid = t.read8(REG_FIFOPAGE_CTRL_2 + 1)
    count = 0
    while not (bcn_valid & BIT(7)) and count < 20:
        count += 1
        time.sleep(50e-6)
        bcn_valid = t.read8(REG_FIFOPAGE_CTRL_2 + 1)
    if bcn_valid & BIT(7):
        t.write8(REG_FIFOPAGE_CTRL_2 + 1, bcn_valid | BIT(7))
    else:
        raise RuntimeError("rsvd-page download not acknowledged (beacon-valid)")


def _iddma_block(t, src: int, dst: int, length: int, fs: bool, ls: bool) -> None:
    """[SRC] IDDMADownLoadFW_3081 — copy one block TXBUF->MCU mem with checksum."""
    for _ in range(20):
        if not (t.read32(REG_DDMA_CH0CTRL) & DDMA_CH_OWN):
            break
        time.sleep(1e-3)
    else:
        raise RuntimeError("DDMA ch0 busy before block")

    ctrl = DDMA_CHKSUM_EN | DDMA_CH_OWN | (length & DDMA_LEN_MASK)
    if not fs:
        ctrl |= DDMA_CH_CHKSUM_CNT          # continue the running checksum
    t.write32(REG_DDMA_CH0SA, src)
    t.write32(REG_DDMA_CH0DA, dst)
    t.write32(REG_DDMA_CH0CTRL, ctrl)

    for _ in range(20):
        if not (t.read32(REG_DDMA_CH0CTRL) & DDMA_CH_OWN):
            break
        time.sleep(1e-3)
    else:
        raise RuntimeError("DDMA ch0 transfer timeout")

    if ls:
        tmp = t.read8(REG_8051FW_CTRL)
        if t.read32(REG_DDMA_CH0CTRL) & DDMA_CHKSUM_FAIL:
            raise RuntimeError("DDMA block checksum failed")
        if dst < OCPBASE_DMEM_3081:         # IMEM
            t.write8(REG_8051FW_CTRL, tmp | IMEM_DL_RDY | IMEM_CHKSUM_OK)
        else:                               # DMEM
            t.write8(REG_8051FW_CTRL, tmp | DMEM_DL_RDY | DMEM_CHKSUM_OK)


def _hal_rom_download_rsvd_page(t, fw: bytes, dmem_pkt_size: int, iram_pkt_size: int) -> None:
    """[SRC] HalROMDownloadFWRSVDPage8814A."""
    bcn_ctrl = t.read8(REG_BCN_CTRL)
    # DMA beacon by SW: REG_CR[8] = 1
    v = t.read8(REG_CR + 1)
    t.write8(REG_CR + 1, v | BIT(0))
    # Disable HW beacon function during download.
    t.write8(REG_BCN_CTRL, (bcn_ctrl & ~EN_BCN_FUNCTION) | DIS_TSF_UDT)
    # Tell HW these rsvd-page packets are not real beacons (0x422[6]=0).
    reg422 = t.read8(REG_FWHW_TXQ_CTRL + 2)
    t.write8(REG_FWHW_TXQ_CTRL + 2, reg422 & ~BIT(6))
    send_beacon = bool(reg422 & BIT(6))
    # Beacon-queue head page + clear beacon-valid (0x205[7]=1).
    t.write16(REG_FIFOPAGE_CTRL_2, TX_PAGE_BOUNDARY)
    bcn_valid = t.read8(REG_FIFOPAGE_CTRL_2 + 1)
    t.write8(REG_FIFOPAGE_CTRL_2 + 1, bcn_valid | BIT(7))

    mem_src = OCPBASE_TXBUF_3081 + TX_PAGE_BOUNDARY * RSVD_PAGE_DDMA_PAGE_SIZE + 40
    for data, dst, fs, ls in _download_blocks(fw, dmem_pkt_size, iram_pkt_size):
        # bmcst = IS_MCAST(addr1); addr1 is bytes [4:10] of the "frame" (the chunk).
        bmc = bool(data[4] & 0x01)
        t.bulk_out(build_fw_txdesc(len(data), bmc) + data)
        _wait_rsvd_page_ok(t)
        _iddma_block(t, mem_src, dst, len(data), fs, ls)

    t.write8(REG_BCN_CTRL, bcn_ctrl)
    if send_beacon:
        t.write8(REG_FWHW_TXQ_CTRL + 2, reg422)
    v = t.read8(REG_CR + 1)
    t.write8(REG_CR + 1, v & ~BIT(0))         # clear CR[8]
    tmp = t.read8(REG_8051FW_CTRL)
    if (tmp & DMEM_CHKSUM_OK) and (tmp & IMEM_CHKSUM_OK):
        tem = t.read8(REG_8051FW_CTRL + 1)
        t.write8(REG_8051FW_CTRL + 1, tem | BIT(6))


def _fw_free_to_go(t) -> bool:
    """[SRC] _FWFreeToGo8814A — poll CPU_DL_READY, 100 x 50 ms."""
    for _ in range(100):
        time.sleep(50e-3)
        if t.read32(REG_8051FW_CTRL) & CPU_DL_READY:
            return True
    return False


def parse_fw_header(fw: bytes) -> tuple[int, int]:
    """Return (dmem_pkt_size, iram_pkt_size) and validate the 64-byte header.

    [SRC] GET_FIRMWARE_HDR_TOTAL_DMEM_SZ_3081 / GET_FIRMWARE_HDR_IRAM_SZ_3081.
    Each region carries an extra FW_CHKSUM_DUMMY_SZ for the DDMA checksum.
    """
    (sig,) = struct.unpack_from("<H", fw, FW_HDR_OFF_SIGNATURE)
    if sig != FW_SIGNATURE_8814A:
        raise ValueError(f"bad FW signature 0x{sig:04x} (expected 0x8814)")
    (dmem_sz,) = struct.unpack_from("<I", fw, FW_HDR_OFF_DMEM_SZ)
    (iram_sz,) = struct.unpack_from("<I", fw, FW_HDR_OFF_IRAM_SZ)
    dmem_pkt = dmem_sz + FW_CHKSUM_DUMMY_SZ
    iram_pkt = iram_sz + FW_CHKSUM_DUMMY_SZ
    if dmem_pkt + iram_pkt + FW_HEADER_SIZE != len(fw):
        raise ValueError(
            f"FW header size mismatch: dmem={dmem_pkt} iram={iram_pkt} "
            f"hdr={FW_HEADER_SIZE} != len={len(fw)}"
        )
    return dmem_pkt, iram_pkt


def download_firmware(t, fw: bytes) -> bool:
    """[SRC] FirmwareDownload8814A. Returns True once CPU_DL_READY is set."""
    dmem_pkt, iram_pkt = parse_fw_header(fw)
    _fwdl_enable(t, True)
    _mcu_core(t, False)                       # disable 3081 MCU core
    _ddma_reset(t)
    _hal_rom_download_rsvd_page(t, fw, dmem_pkt, iram_pkt)
    _mcu_core(t, True)                        # enable 3081 MCU core
    _fwdl_enable(t, False)
    ready = _fw_free_to_go(t)
    # InitializeFirmwareVars8814 runs in FirmwareDownload8814A's exit path: seed
    # the H2C command trigger so the first host->FW message latches correctly.
    t.write8(REG_HMETFR, 0x0F)
    return ready


def _mac_power_on_check(t) -> None:
    """[SRC] rtl8814au_hal_init:1073-1086 — "Check if MAC has already power on".

    Two inert reads (REG_SYS_CLKR+1 bit3 + REG_CR) whose only effect is a log line; the
    result is not used to branch the bring-up. Reproduced so the single cursor accounts
    for the first two ops of the open path. [WIRE] cap1 frames 5707-5709.
    """
    from .constants import REG_SYS_CLKR
    t.read8(REG_SYS_CLKR + 1)
    t.read8(REG_CR)


def bring_up(t, fw: bytes) -> bool:
    """M1: power-on -> LLT -> drop-incorrect -> firmware download to FW-ready.

    [SRC] rtl8814au_hal_init lines 1073..1133 (rtl8814au_hw_reset is #if 0).
    """
    _mac_power_on_check(t)
    power_on(t)
    init_llt(t)
    init_drop_incorrect(t)
    return download_firmware(t, fw)
