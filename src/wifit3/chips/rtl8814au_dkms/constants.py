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
REG_HMETFR = 0x01CC          # H2C command trigger; InitializeFirmwareVars8814 seeds 0x0f

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

# --- M2b: post-MAC-table hal_init MISC stage --------------------------------
# The hal_init block between PHY_MACConfig8814 and PHY_BBConfig8814
# [SRC] usb/usb_halinit.c rtl8814au_hal_init lines 1168..1198. [WIRE] cap1 7003..7101.
REG_TRXDMA_CTRL = 0x010C       # TX/RX DMA queue-priority + agg-enable word

# Out-EP queue priority [SRC] include/hal_com_reg.h _TXDMA_*Q_MAP + QUEUE_*
QUEUE_LOW = 1
QUEUE_NORMAL = 2
QUEUE_HIGH = 3


def _txdma_map(q: int, shift: int) -> int:
    return (q & 0x3) << shift


# _InitPageBoundary: REG_RXFF_PTR <- RX_DMA_BOUNDARY_8814A
# = MAX_RX_DMA_BUFFER_SIZE_8814A(0x5C00) - RX_DMA_RESERVED_SIZE_8814A(0) - 1.
# [SRC] rtl8814a_spec.h:164; reserved-size 0 in this build (wire = 0x5BFF, not 0x5AFF).
RX_DMA_BOUNDARY = 0x5BFF

REG_RX_DRVINFO_SZ = 0x060F     # _InitDriverInfoSize; DRVINFO_SZ = 4 (unit 8 B)
DRVINFO_SZ = 4
REG_HIMR0 = 0x00B0             # _InitInterrupt; IntrMask[0] = 0 on USB
REG_HIMR1 = 0x00B8             # IntrMask[1] = 0

# _InitNetworkType: REG_CR[17:16] = NT_LINK_AP [SRC] hal_com_reg.h
MASK_NETTYPE = 0x30000
NT_LINK_AP = 0x2


def NETTYPE(x: int) -> int:    # _NETTYPE(x)
    return (x & 0x3) << 16


# _InitMacConfigure_8814A
REG_RRSR = 0x0440
RRSR_RATE_MASK = 0xFFFFF       # phydm_rrsr_set_register: odm_set_mac_reg(0x440, 0xfffff)
RATE_ALL_CCK = 0x0000000F      # RATR_1M|2M|55M|11M
RATE_ALL_OFDM_AG = 0x00000FF0  # RATR_6M..54M
REG_RETRY_LIMIT = 0x042A
RL_VAL_STA = 0x30              # BIT_LRL(RL_VAL_STA)|BIT_SRL(RL_VAL_STA) = 0x3030
REG_RCR = 0x0608
REG_RXFLTMAP1 = 0x06A2
RXFLTMAP1_VAL = BIT(10) | BIT(5)  # mask ps-poll (BIT10); NDPA for beamforming (BIT5)
REG_MAX_AGGR_NUM = 0x04CA
REG_RTS_MAX_AGGR_NUM = 0x04CB
MAX_AGGR_NUM = 0x36

# RCR (STA-mode init value); monitor-mode rewrite is a later (RX) milestone.
# [SRC] hal_com_reg.h RCR_*; [WIRE] cap1 REG_RCR <- 0xf40060ce.
RCR_APM = BIT(1)
RCR_AM = BIT(2)
RCR_AB = BIT(3)
RCR_CBSSID_DATA = BIT(6)
RCR_CBSSID_BCN = BIT(7)
RCR_AMF = BIT(13)
RCR_HTC_LOC_CTRL = BIT(14)
RCR_APP_PHYST_RXFF = BIT(28)
RCR_APP_ICV = BIT(29)
RCR_APP_MIC = BIT(30)
RCR_APPFCS = BIT(31)           # CONFIG_RX_PACKET_APPEND_FCS (defined in this build)
FORCEACK = BIT(26)
RCR_INIT_VALUE = (
    RCR_APM | RCR_AM | RCR_AB | RCR_CBSSID_DATA | RCR_CBSSID_BCN
    | RCR_APP_ICV | RCR_AMF | RCR_HTC_LOC_CTRL | RCR_APP_MIC
    | RCR_APP_PHYST_RXFF | FORCEACK | RCR_APPFCS
)  # = 0xf40060ce

# _InitEDCA_8814AUsb
REG_SPEC_SIFS = 0x0428
REG_MAC_SPEC_SIFS = 0x063A
REG_SIFS_CTX = 0x0514
REG_SIFS_TRX = 0x0516
SIFS_VAL = 0x100A
REG_EDCA_VO_PARAM = 0x0500
REG_EDCA_VI_PARAM = 0x0504
REG_EDCA_BE_PARAM = 0x0508
REG_EDCA_BK_PARAM = 0x050C
EDCA_BE_VAL = 0x005EA42B
EDCA_BK_VAL = 0x0000A44F
EDCA_VI_VAL = 0x005EA324
EDCA_VO_VAL = 0x002FA226

# _InitRetryFunction_8814A
EN_AMPDU_RTY_NEW = BIT(7)       # REG_FWHW_TXQ_CTRL(0x420)
REG_ACKTO = 0x0640
ACKTO_VAL = 0x80

# init_UsbAggregationSetting_8814A
REG_TDECTRL = 0x0208           # aliases REG_FIFOPAGE_CTRL_2; here the TX-agg desc-num word
BLK_DESC_NUM_SHIFT = 4
BLK_DESC_NUM_MASK = 0xF
USB_TX_AGG_DESC_NUM = 3
REG_RXDMA_AGG_PG_TH = 0x0280
RXDMA_AGG_EN = BIT(2)          # REG_TRXDMA_CTRL; already set on cold boot
USB_AGG_EN = BIT(7)            # REG_RXDMA_AGG_PG_TH+3 (0x283)

# _InitBeaconParameters_8814A / _InitBeaconMaxError_8814A
REG_TBTT_PROHIBIT = 0x0540
TBTT_PROHIBIT_SETUP_TIME = 0x04
TBTT_PROHIBIT_HOLD_TIME_STOP_BCN = 0x64
REG_DRVERLYINT = 0x0558
DRIVER_EARLY_INT_TIME = 0x05
REG_BCNDMATIM = 0x0559
BCN_DMA_ATIME_INT_TIME = 0x02
REG_BCNTCFG = 0x0510
BCNTCFG_VAL = 0x4413
REG_BCN_MAX_ERR = 0x055D       # CONFIG_ADHOC_WORKAROUND_SETTING -> 0xFF

# _InitBurstPktLen
REG_FAST_EDCA_VOVI_SETTING = 0x1448
REG_FAST_EDCA_BEBK_SETTING = 0x144C
FAST_EDCA_VAL = 0x08070807
REG_USB_SPEED = 0x00FF         # bit7 set => USB2/1.1 mode
REG_RXDMA_MODE = 0x0290
RXDMA_MODE_BURST_512 = 0x1E    # USB2 + 512-B bulk-out
RXDMA_AGG_TH_USB2 = 0x2005     # REG_RXDMA_AGG_PG_TH, 20K agg threshold

# Init CR MACTXEN/MACRXEN after RxFF boundary [SRC] usb_halinit.c:1197.
MACTXEN = BIT(6)
MACRXEN = BIT(7)

# --- M2b: PHY_BBConfig8814 prefix [SRC] rtl8814a_phycfg.c:334 ----------------
FEN_USBA = BIT(2)              # REG_SYS_FUNC_EN(0x02): USB analog enable
REG_BB_GLB_RST = 0x1002        # 8814A BB global reset (literal in vendor src)
FEN_BB_GLB_RSTn = BIT(1)
FEN_BBRSTB = BIT(0)
REG_RF_CTRL0 = 0x001F          # PathA RF power-on  (0x07)
REG_RF_CTRL1 = 0x0020          # PathB+C RF power-on (0x0707, 2 B)
REG_RF_CTRL3 = 0x0076          # PathD RF power-on  (0x07)
RF_POWER_ON = 0x07

# PHY_BBConfig8814 suffix: crystal-cap + TRX-path [SRC] rtl8814a_phycfg.c:370,305.
REG_XTAL_CTRL = 0x002C         # crystal-cap field [26:15] (8814A) [SRC] R_0x2c
CRYSTAL_CAP_MASK = 0x07FF8000  # 0x2C[26:21] = 0x2C[20:15] = crystal_cap
# crystal_cap (6-bit) from efuse EEPROM_XTAL_8814A; [WIRE] cap1 0x2c <- 0x4471d820.
CRYSTAL_CAP = 0x23
rCCK0_FalseAlarmReport = 0x0A2C
rCCK_RX_Jaguar = 0x0A04        # CCK RX path selection

# --- USB device identity ----------------------------------------------------
VID_REALTEK = 0x0BDA
PID_RTL8814AU = 0x8813  # ALFA AWUS1900 (4T4R) [WIRE] lsusb 0bda:8813
