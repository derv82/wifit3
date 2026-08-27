"""RTL8814AU register map and magic numbers — vendor (morrownr 8814au 5.8.5.1) port.

Cleanroom: every value here is grepped verbatim from the vendor DKMS source
(``hal/rtl8814a/`` + ``include/``) and cross-checked against the cold-boot pcap
(``driver_captures/captures_rtl8814au/capture-1.pcap``). [SRC] cites the vendor
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
REG_SYS_CLKR = 0x0008         # +1 (0x09) bit3 = MAC-already-powered check
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
DIS_ATIM = BIT(0)
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
# crystal_cap (6-bit) is read from efuse (efuse.read_chip_params); for this card
# it is 0x23 — [WIRE] cap1 0x2c <- 0x4471d820, confirmed by verify_efuse_pcap.py.
rCCK0_FalseAlarmReport = 0x0A2C
rCCK_RX_Jaguar = 0x0A04        # CCK RX path selection

# --- M2c: PHY_RFConfig8814A (RF radio tables) -------------------------------
# Each RF register write rides the per-path LSSI write register as
# (addr << 20) | (data & RFREG_MASK). [SRC] rf_reg.h r{A,B,C,D}_LSSIWrite_Jaguar*.
RF_LSSI_WRITE = {"a": 0x0C90, "b": 0x0E90, "c": 0x1890, "d": 0x1A90}
RFREG_MASK = 0xFFFFF            # RFREGOFFSETMASK (20-bit RF data)
RF_WRITE_MASK = 0x0FFFFFFF      # phy_RFWrite_8814A: (offset<<20 | data) & 0x0FFFFFFF
RF_DELAY_ADDRS = (0xFE, 0xFFE)  # odm_config_rf_reg_8814a: 50 ms delay, not a write
RF_RCK1 = 0x1C                 # RF_RCK1_Jaguar — read on path A, copied to B/C/D
# phy_RFRead_8814A: RF regs are memory-mapped at base + addr*4 (per path).
RF_READ_BASE = {"a": 0x2800, "b": 0x2C00, "c": 0x3800, "d": 0x3C00}

# --- M2d: channel tune (PHY_ConfigBB + band switch + set_chnl_bw, 20 MHz) ----
# [SRC] rtl8814a_phycfg.c PHY_ConfigBB_8814A:555, PHY_SwitchWirelessBand8814A:1139,
# phy_SwChnl8814A / phy_SetBwMode8814A. 20 MHz primary only. [WIRE] cap1 11335+.
rOFDMCCKEN = 0x0808            # PHY_ConfigBB: bOFDMEN|bCCKEN = 0x3
bOFDMEN = BIT(29)
bCCKEN = BIT(28)
REG_SYS_CFG3_2 = 0x1002       # REG_SYS_CFG3_8814A+2; bit0 gates CCK/OFDM clock
rAGC_table_Jaguar2 = 0x0958   # 2.4G AGC-table select [4:0] = 0
RFE_PINMUX = (0x0CB0, 0x0EB0, 0x18B4, 0x1AB4)  # rA..D_RFE_Pinmux_Jaguar
REG_RFE_INV = 0x1ABC          # RFE inv (0x1ABC[27:20]), WiFi (BT-coex off)
# [SRC] PHY_SetRFEReg8814A rfe_type switch [rtl8814a_phycfg.c:1040/1070] — per band,
# (A/B/C pinmux, D pinmux, inv nibble). D=None => skip the D-path write (rfe 0/default 2.4G
# writes A/B/C only). Values keyed by rfe_type; RFE_PINMUX_*_DEFAULT is case 0 + the switch
# `default:`. The captured ALFA card is rfe_type=1.
RFE_PINMUX_2G = {2: (0x72707270, 0x77707770, 0x72), 1: (0x77777777, 0x77777777, 0x77)}
RFE_PINMUX_2G_DEFAULT = (0x77777777, None, 0x77)
RFE_PINMUX_5G = {2: (0x37173717, 0x77177717, 0x37), 1: (0x33173317, 0x77177717, 0x33)}
RFE_PINMUX_5G_DEFAULT = (0x54775477, 0x54775477, 0x54)
rTxPath = 0x080C              # 2.4G: [7:4] = 0x2
rCCK_RX = 0x0A04              # 2.4G: [27:24] = 0x5  (== rCCK_RX_Jaguar)
REG_CCK_CHECK = 0x0454        # 2.4G = 0 (bit7 selects 5G CCK)
REG_A80 = 0x0A80              # clear BIT18 on 5G->2.4G
TXSCALE = (0x0C1C, 0x0E1C, 0x181C, 0x1A1C)     # rA..D_TxScale; BB swing [31:21]
BBSWING_MASK = 0xFFE00000
BBSWING_DEFAULT = 0x200       # 0 dB; the per-path value is decoded from efuse 0xC6
                              # (efuse._parse_bb_swing_2g) — this is the index-0 default
rRFMOD = 0x08AC               # ADC bw: [1:0] = 0 for 20 MHz
rAGC_table_Jaguar = 0x082C    # AGC bw: [15:12] = 6 for 20 MHz
rFc_area = 0x0860             # 2.4G center-freq area: [28:17] = 0x96A
RF_CHNLBW = 0x18              # RF reg: channel [mask 0x703ff], bw [11:10]=3 (20 MHz)
RF_CHNLBW_CH_MASK = 0x703FF
RF_CHNLBW_BW_MASK = 0xC00
rCCK0_TxFilter1 = 0x0A20
rCCK0_TxFilter2 = 0x0A24
rCCK0_DebugPort = 0x0A28

# Tunable channels @ 20 MHz (the channel-tune logic handles these sub-bands). 2.4 GHz
# is 1-14; 5 GHz is the standard UNII-1/2/2e/3 20 MHz set.
CHANNELS_2G = tuple(range(1, 15))
CHANNELS_5G = (36, 40, 44, 48, 52, 56, 60, 64,
               100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
               149, 153, 157, 161, 165)
# Advertised/hopped set: UNII-1 + UNII-3 only. DFS (52-144) is radar-shared and usually
# empty; the chip still tunes it via set_channel, it's just not in SUPPORTED_CHANNELS.
CHANNELS_5G_NON_DFS = (36, 40, 44, 48, 149, 153, 157, 161, 165)

# Software band-type state [SRC] BAND_TYPE. The vendor tracks current_band_type and only
# updates it inside PHY_SwitchWirelessBand8814A (an *actual* band switch). The CCK txagc
# section is written only when current_band_type == BAND_ON_2_4G, so after init_hw_mlme_ext
# resets it to BAND_MAX a no-switch 2.4 GHz tune (chip already 2.4 GHz) skips CCK until a real
# 5G->2.4G crossing sets it back to 2.4 GHz.
BAND_ON_2_4G = 0
BAND_ON_5G = 1
BAND_MAX = 0xFF               # init_hw_mlme_ext reset value (no band committed yet)
REG_TRXPTCL_CTL = 0x0668      # MAC bw: clear BIT7|BIT8 for 20 MHz
REG_DATA_SC = 0x0483          # secondary-channel = 0 for 20 MHz
# phy_SpurCalibration NBI/CSI reset (2.4G has no spur -> reset)
rNBI_Setting = 0x087C
rCSI_Mask_Setting1 = 0x0874
rCSI_FIX_MASK = (0x0880, 0x0884, 0x0898, 0x089C)  # rCSI_Fix_Mask0,1,6,7
NBI_EN_BIT = BIT(13)          # phydm_nbi_enable: 0x87c[13] = 0 (disable)

# --- EFUSE (probe-phase chip-param read) ------------------------------------
# Physical efuse read via EFUSE_CTRL, then header-unpacked into a 512 B logical
# map. [SRC] hal_EfuseReadEFuse8814A + halmac per-byte protocol; [WIRE] cap1
# frames 51..5677 (device 51), 312 physical bytes.
REG_SYS_CFG1 = 0x00F0          # ReadChipVersion: chip-id / cut / package (4 B)
REG_9346CR = 0x000A            # boot/autoload status (BOOT_FROM_EEPROM | EEPROM_EN)
BOOT_FROM_EEPROM = BIT(4)
EEPROM_EN = BIT(5)             # autoload OK when set
REG_EFUSE_CTRL = 0x0030        # [7:0]=data, +1=addr[7:0], +2[1:0]=addr[9:8], +3 b7=flag
REG_EFUSE_TEST = 0x0034        # [9:8] = EFUSE_SEL bank (WIFI bank 0)
EFUSE_SEL_MASK = 0x0300
REG_EFUSE_ACCESS = 0x00CF      # access gate
EFUSE_ACCESS_ON = 0x69
EFUSE_ACCESS_OFF = 0x00
EFUSE_CTRL_VALID = BIT(7)      # REG_EFUSE_CTRL+3 bit7 — read-done flag

EFUSE_MAP_LEN = 512            # EFUSE_MAP_LEN_8814A
EFUSE_MAX_SECTION = 64         # EFUSE_MAX_SECTION_8814A
EFUSE_MAX_WORD_UNIT = 4        # EFUSE_MAX_WORD_UNIT_8814A
EFUSE_REAL_CONTENT_LEN = 1024  # EFUSE_REAL_CONTENT_LEN_8814A (addr ceiling)

# Logical-map offsets [SRC] include/hal_pg.h
EEPROM_MAC_ADDR = 0xD8         # EEPROM_MAC_ADDR_8814AU, 6 B
EEPROM_XTAL = 0xB9             # EEPROM_XTAL_8814 (crystal_cap)
EEPROM_RFE_OPTION = 0xCA       # EEPROM_RFE_OPTION_8814 (rfe_type, bit7 + [6:0])
EEPROM_TRX_ANTENNA_OPTION = 0xC9  # EEPROM_TRX_ANTENNA_OPTION_8814 [hal_pg.h:194] (rf_path decision)
EEPROM_TX_BBSWING_2G = 0xC6    # EEPROM_TX_BBSWING_2G_8814 (2-bit BB-swing index per path)
EEPROM_TX_BBSWING_5G = 0xC7    # EEPROM_TX_BBSWING_5G_8814 (2-bit BB-swing index per path)
EEPROM_THERMAL_METER_8814 = 0xBA   # EEPROM_THERMAL_METER_8814 [SRC] hal_pg.h:179 (thermal PG base)
EEPROM_DEFAULT_THERMAL_METER_8814A = 0x18  # [SRC] hal_pg.h:793 (used when the byte is unburned)
EEPROM_DEFAULT_CRYSTAL_CAP = 0x20  # EEPROM_Default_CrystalCap_8814
RFE_TYPE_8814AU_FALLBACK = 1   # hal_ReadRFEType_8814A 8814AU branch

# --- M3b: hal_init turn-on tail (after rtl8814_InitHalDm) --------------------
# [SRC] usb/usb_halinit.c rtl8814au_hal_init lines 1285..1305. [WIRE] cap1 14573+.
REG_GPIO_IO_SEL_8814A = 0x0042  # PHY_SetRFEReg8814A(TRUE): byte [23:20] |= 0xf
RFE_8814_REG = 0x1994           # PHY_SetRFEReg8814A(TRUE): [3:0] = 0xf (bare literal in vendor src)
REG_QUEUE_CTRL = 0x04C6         # RTS-BW: clear BIT3 (& 0xF7)
REG_NAV_UPPER = 0x0652          # HW_VAR_NAV_UPPER (also Nav-limit byte, set 0 in MISC11)
WIFI_NAV_UPPER_US = 30000       # WiFiNavUpperUs [SRC] include/wifi.h
HAL_NAV_UPPER_UNIT = 128        # [SRC] include/hal_com_reg.h (micro-second unit)
REG_SDIO_CTRL_8814A = 0x0070    # "Reset USB mode switch setting" = 0
REG_ACLK_MON = 0x003E           # = 0
REG_MACID = 0x0610              # MAC address, 6 B (HW_VAR_MAC_ADDR -> set_macaddr_port)
ETH_ALEN = 6

# --- M3b-2: monitor-mode entry (always-monitor deviation) -------------------
# wifit3 is always-monitor, so connect() runs the vendor's monitor opmode entry
# `hw_var_set_opmode(_HW_STATE_MONITOR_)` [SRC rtl8814a_hal_init.c:3222] directly
# after hal_init: Set_MSR(NOLINK) then hw_var_set_monitor [SRC :3155].
MSR = REG_CR + 2               # 0x0102 Media Status reg; [1:0] = port0 net-type
MSR_NETTYPE_MASK = 0x0C        # Set_MSR keeps [3:2] (port1), rewrites [1:0]
MSR_NOLINK = 0x00              # _HW_STATE_NOLINK_ net-type
MSR_STATION = 0x02             # _HW_STATE_STATION_ net-type (airmon's up-time opmode)
REG_RXFLTMAP0 = 0x06A0         # RX filter map: data subtypes
REG_RXFLTMAP2 = 0x06A4         # RX filter map: control subtypes
RXFLTMAP_ACCEPT_ALL = 0xFFFF   # monitor: accept all mgmt/ctrl/data subtypes
# Monitor RCR [SRC rtl8814a_hal_init.c:3168] — "receive all type" + append-FCS +
# accept CRC/ICV-error frames (visibility over validation in monitor).
RCR_AAP = BIT(0)               # accept all unicast (no addr match)
RCR_APWRMGT = BIT(5)           # accept power-management frames
RCR_ACRC32 = BIT(8)            # accept CRC32-error frames
RCR_AICV = BIT(9)              # accept ICV-error frames
RCR_ADF = BIT(11)              # accept data frames
RCR_ACF = BIT(12)              # accept control frames
RCR_MONITOR_VALUE = (
    RCR_AAP | RCR_APM | RCR_AM | RCR_AB | RCR_APWRMGT
    | RCR_ACRC32 | RCR_AICV | RCR_ADF | RCR_ACF | RCR_AMF
    | RCR_APP_PHYST_RXFF | RCR_APPFCS
)  # = 0x90003b2f

# --- USB device identity ----------------------------------------------------
VID_REALTEK = 0x0BDA
PID_RTL8814AU = 0x8813  # ALFA AWUS1900 (4T4R) [WIRE] lsusb 0bda:8813
