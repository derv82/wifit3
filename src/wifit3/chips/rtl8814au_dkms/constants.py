"""RTL8814AU register map and magic numbers — vendor (morrownr 8814au 5.8.5.1) port.

Cleanroom: every value here is grepped verbatim from the vendor DKMS source
(``hal/rtl8814a/`` + ``include/``) and cross-checked against the cold-boot pcap
(``usb_dumps_new/captures_rtl8814au/capture-1.pcap``). [SRC] cites the vendor
file; [WIRE] cites the capture frame range that exercises the value.

This is the Realtek PHYDM/ODM vendor stack, NOT mainline rtw88 — addresses and
init flow differ from the in-tree driver even where the silicon is identical.
"""
from __future__ import annotations


def BIT(n: int) -> int:
    return 1 << n


# --- USB vendor I/O ---------------------------------------------------------
# Realtek register access rides a single vendor request (0x05); wValue carries
# the 16-bit register offset, wIndex is 0, the data stage is 1/2/4 bytes LE.
# [SRC] os_dep/.../usb_ops_linux.c usbctrl_vendorreq()
REALTEK_VENDOR_REQUEST = 0x05
REQ_TYPE_WRITE = 0x40  # host->device, vendor, device
REQ_TYPE_READ = 0xC0  # device->host, vendor, device

# Bulk-OUT endpoint that carries the firmware (sent as beacon-queue TX packets).
# [WIRE] all 46 FW packets land on EP 0x02.
EP_BULK_OUT_FW = 0x02

# --- Power-on / MAC bring-up registers --------------------------------------
# [SRC] include/rtl8814a_spec.h
REG_SYS_FUNC_EN = 0x0002      # +1 (0x03) bit2 = 3081 MCU-core reset gate
REG_CR = 0x0100              # MAC TRX enable word
REG_RXFF_PTR = 0x011C
REG_FIFOPAGE_CTRL_2 = 0x0204  # +1 (0x205) bit7 = beacon-valid
REG_AUTO_LLT = 0x0208        # bit0 = HW auto-init LLT, polls back to 0
REG_TXDMA_OFFSET_CHK = 0x020C
REG_FIFOPAGE_INFO_1 = 0x0230  # HPQ page count
REG_FIFOPAGE_INFO_2 = 0x0234  # LPQ
REG_FIFOPAGE_INFO_3 = 0x0238  # NPQ
REG_FIFOPAGE_INFO_4 = 0x023C  # EPQ
REG_FIFOPAGE_INFO_5 = 0x0240  # PUB
REG_RQPN_CTRL_2 = 0x022C
REG_TXPKTBUF_BCNQ_BDNY = 0x0424
REG_TXPKTBUF_BCNQ1_BDNY = 0x0456
REG_MGQ_PGBNDY = 0x047A
REG_FWHW_TXQ_CTRL = 0x0420    # +2 (0x422) bit6 = "this is a real beacon"
REG_BCN_CTRL = 0x0550
REG_8051FW_CTRL = 0x0080      # MCUFW download/ready control word
REG_CPU_DMEM_CON = 0x1080     # bit16 = DDMA reset

# REG_CR enable bits [SRC] include/hal_com_reg.h
HCI_TXDMA_EN = BIT(0)
HCI_RXDMA_EN = BIT(1)
TXDMA_EN = BIT(2)
RXDMA_EN = BIT(3)
PROTOCOL_EN = BIT(4)
SCHEDULE_EN = BIT(5)
ENSEC = BIT(9)
CALTMR_EN = BIT(10)
CR_ENABLE_BITS = (
    HCI_TXDMA_EN | HCI_RXDMA_EN | TXDMA_EN | RXDMA_EN
    | PROTOCOL_EN | SCHEDULE_EN | ENSEC | CALTMR_EN
)  # = 0x063F [WIRE] cap1 frame 5787 writes REG_CR=0x063F

# REG_BCN_CTRL bits [SRC] include/hal_com_reg.h
EN_BCN_FUNCTION = BIT(3)
DIS_TSF_UDT = BIT(4)

REG_TXDMA_DROP_DATA_EN = BIT(9)  # DROP_DATA_EN, REG_TXDMA_OFFSET_CHK

# --- Queue reserved-page values (our non-WMM config) ------------------------
# [SRC] _InitQueueReservedPage_8814AUsb + page-num defines
# [WIRE] cap1 frames 5789..5809
HPQ_PGNUM = 0x20
LPQ_PGNUM = 0x20
NPQ_PGNUM = 0x20
EPQ_PGNUM = 0x20
PUB_PGNUM = 0x776
RQPN_CTRL_2_VALUE = 0x80000000
TX_PAGE_BOUNDARY = 0x07F6  # TXPKT_PGNUM_8814A; txpktbuf_bndy

# --- Firmware-download enable / ready bits ----------------------------------
# _FWDownloadEnable_8814A(TRUE): (read16(0x80) & 0x3000) & ~BIT12 | BIT13 | BIT0
FWDL_EN_KEEP_MASK = 0x3000
FWDL_EN_BIT = BIT(13)
FWDL_RAM_DL_SEL = BIT(0)
FWDL_ROM_DL = BIT(12)
MCU_CORE_EN = BIT(2)         # REG_SYS_FUNC_EN+1 (0x03) — 3081 enable/disable
DDMA_RESET = BIT(16)         # REG_CPU_DMEM_CON
CPU_DL_READY = BIT(15)       # REG_8051FW_CTRL — set when FW boot completes

# 8051FW_CTRL checksum-ok flags written after a successful download
IMEM_DL_RDY = BIT(3)
IMEM_CHKSUM_OK = BIT(4)
DMEM_DL_RDY = BIT(5)
DMEM_CHKSUM_OK = BIT(6)

# --- 3081 IDDMA (firmware copy from TX buffer into MCU memory) ---------------
# [SRC] IDDMADownLoadFW_3081 + DDMA defines
REG_DDMA_CH0SA = 0x1200      # source addr (in TX packet buffer)
REG_DDMA_CH0DA = 0x1204      # dest addr (MCU IMEM/DMEM)
REG_DDMA_CH0CTRL = 0x1208    # len + flags + OWN
DDMA_CH_OWN = BIT(31)
DDMA_CHKSUM_EN = BIT(29)
DDMA_CHKSUM_FAIL = BIT(27)
DDMA_RST_CHKSUM_STS = BIT(25)
DDMA_CH_CHKSUM_CNT = BIT(24)
DDMA_LEN_MASK = 0x0001FFFF

OCPBASE_IMEM_3081 = 0x00000000
OCPBASE_DMEM_3081 = 0x00200000
OCPBASE_TXBUF_3081 = 0x18780000
RSVD_PAGE_DDMA_PAGE_SIZE = 128  # PageSize in HalROMDownloadFWRSVDPage

# --- Firmware blob layout ---------------------------------------------------
# [SRC] include/rtl8814a_hal.h GET_FIRMWARE_HDR_*_3081 (LE_BITS_TO_4BYTE)
FW_HEADER_SIZE = 64
FW_HDR_OFF_SIGNATURE = 0   # u16, == 0x8814
FW_HDR_OFF_VERSION = 4     # u16
FW_HDR_OFF_SUBVER = 5      # u8 (byte after version)
FW_HDR_OFF_DMEM_SZ = 36    # u32, total DMEM size (excl. checksum dummy)
FW_HDR_OFF_IRAM_SZ = 48    # u32, IRAM size (excl. checksum dummy)
FW_SIGNATURE_8814A = 0x8814
FW_CHKSUM_DUMMY_SZ = 8     # appended to each of DMEM/IRAM before download

# --- Beacon-queue TX descriptor (firmware-download packets) -----------------
# The FW is streamed via dump_mgntframe on the beacon queue. The wire descriptor
# is 40 bytes; the rsvd-page allocator reserves 8 extra PACKET_OFFSET bytes that
# update_txdesc "pulls" off the wire, so the bulk packet is txdesc(40)+data.
# [SRC] rtl8814a_xmit.c / usb/rtl8814au_xmit.c, hal_data sizes
TXDESC_SIZE = 40
PACKET_OFFSET_SZ = 8
TXDESC_OFFSET = TXDESC_SIZE + PACKET_OFFSET_SZ  # = 48
MAX_XMIT_EXTBUF_SZ = 1536
# Block size of one FW download chunk (= one beacon packet's payload).
# [WIRE] full chunk = 1488 B across all 46 packets in all 3 cold boots.
MAX_RSVD_PAGE_BUF = MAX_XMIT_EXTBUF_SZ - TXDESC_OFFSET  # = 1488

# --- USB device identity ----------------------------------------------------
VID_REALTEK = 0x0BDA
PID_RTL8814AU = 0x8813  # ALFA AWUS1900 (4T4R) [WIRE] lsusb 0bda:8813
