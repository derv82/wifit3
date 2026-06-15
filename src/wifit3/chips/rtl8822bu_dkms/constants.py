"""RTL8822BU (morrownr rtl88x2bu / HALMAC+PHYDM) — register + protocol constants.

Cleanroom: every value here is pasted verbatim from the vendor source, cited
``[SRC] <file>:<line>``. Do NOT type a constant from memory — grep it out.

Scope so far: M0 (enumerate + chip-version probe + transport) and M1 (HALMAC
power sequence + the warm-reboot reset workaround). Later milestones append here.
"""
from __future__ import annotations

# --- USB identity ---------------------------------------------------------
# TP-Link Archer T3U Plus v1 (the dev card); in the DKMS supported-device-IDs.
# [SRC] usb-topology.log: "2357:0138 TP-Link 802.11ac NIC"
USB_VID_TPLINK = 0x2357
USB_PID_ARCHER_T3U_PLUS = 0x0138

# --- Vendor control-transfer convention (Realtek rtw88-family) ------------
# [SRC] include/usb_ops.h:19-22
REALTEK_USB_VENQT_READ = 0xC0          # bmRequestType for a register read
REALTEK_USB_VENQT_WRITE = 0x40         # bmRequestType for a register write
REALTEK_USB_VENQT_CMD_REQ = 0x05       # bRequest — the vendor register access
REALTEK_USB_VENQT_CMD_IDX = 0x00       # wIndex
MAX_VENDOR_REQ_CMD_SIZE = 254          # [SRC] include/usb_ops.h:30
FW_START_ADDRESS = 0x1000              # [SRC] include/usb_ops_linux.h:19

# --- 8822b/8821c/8822c USB register-page-switch workaround ----------------
# usbctrl_vendorreq() emits, after EVERY vendor access to an "ON-section"
# register, an extra 1-byte bRequest=0x05 write to 0x4E0 carrying the low byte
# of the IO buffer (read-back value for reads, written value for writes). The
# ON-section is reg addr <= 0xFF or 0x1000..0x10FF; everything else (OFF/LOCAL)
# gets no mirror. This is the chip's banked-register confirm; it must be
# reproduced for byte-faithfulness. [SRC] os_dep/linux/usb_ops_linux.c:171-201
REG_PAGE_SWITCH_CONFIRM = 0x04E0       # REG_NULL_PKT_STATUS_V1 [SRC] halmac_reg_8822b.h:379
ON_SEC_RANGES = ((0x0000, 0x00FF), (0x1000, 0x10FF))

# --- M0/M1 registers (8822b) ----------------------------------------------
# [SRC] hal/halmac/halmac_reg_8822b.h, halmac_reg2.h
REG_SYS_FUNC_EN = 0x0002               # :20  (+1 SYS_FUNC_EN in init_system_cfg)
REG_RSV_CTRL = 0x001C                  # reg2.h:149  (pre_init clears this first)
REG_RF_CTRL = 0x001F                   # reg2.h:166  (enable_bb_rf BIT0/1/2)
REG_GPIO_MUXCFG = 0x0040               # reg2.h:328  (pre_init sets BIT2; init_system_cfg FSPI)
REG_LED_CFG = 0x004C                   # reg2.h:365  (pre_init clears BIT25/26)
REG_PAD_CTRL1 = 0x0064                 # reg2.h:388  (pre_init sets BIT28/29 PIN-mux)
REG_WL_BT_PWR_CTRL = 0x0068            # :49
REG_MCUFW_CTRL = 0x0080                # :55  (FW-ready / boot-from-flash)
REG_WLRF1 = 0x00EC                     # reg2.h:798  (enable_bb_rf BIT24/25/26)
REG_SYS_CFG1 = 0x00F0                  # :82  (chip version / cut / vendor; +2 test-mode BIT4)
REG_SYS_STATUS1 = 0x00F4               # :83  (+1 BIT0 = power state probe)
REG_SYS_CFG2 = 0x00FC                  # :85  (+3 == 0x20 => USB3 link)
REG_CR = 0x0100                        # :109 (0xEA marks the chip disabled)
REG_CPU_DMEM_CON = 0x1080              # reg2.h:6204 (init_system_cfg WL_PLATFORM_RST)
REG_SW_MDIO = 0x10C0                   # :96  (+3 BIT0 = post-power-on read-twice probe)
REG_PRE_INIT_FE5B = 0xFE5B             # pre_init USB3-only |= BIT(4) [SRC] halmac_init_8822b.c:963

# init_system_cfg bit/value constants [SRC] halmac_init_8822b.c:36,724-735, halmac_bit2.h
SYS_FUNC_EN = 0xDC                     # OR'd into REG_SYS_FUNC_EN+1
BIT_WL_PLATFORM_RST = 1 << 16          # bit2.h:58085
BIT_BOOT_FSPI_EN = 1 << 20             # bit2.h:12788 (boot-from-flash; cleared for driver FW DL)
BIT_FSPI_EN = 1 << 19                  # bit2.h:7200

# --- Firmware download (HALMAC iDDMA) -------------------------------------
# FW blob: morrownr array_mp_8822b_fw_nic (v30.20, 161240 B) — NOT the linux-firmware
# rtw88 blob (161176 B, different version). The cold captures were taken with the morrownr
# driver, so the vendor array is the wire ground truth. [SRC] hal/rtl8822b/hal8822b_fw.c:13389
FW_BLOB_SIZE = 161240
# WLAN_FW header field offsets [SRC] hal/halmac/halmac_fw_info.h:22-40
WLAN_FW_HDR_SIZE = 64
WLAN_FW_HDR_CHKSUM_SIZE = 8
WLAN_FW_HDR_MEM_USAGE = 24             # BIT(4) => emem present
WLAN_FW_HDR_H2C_FMT_VER = 28
WLAN_FW_HDR_DMEM_ADDR = 32
WLAN_FW_HDR_DMEM_SIZE = 36
WLAN_FW_HDR_IMEM_SIZE = 48
WLAN_FW_HDR_EMEM_SIZE = 52
WLAN_FW_HDR_EMEM_ADDR = 56
WLAN_FW_HDR_IMEM_ADDR = 60
# DMA / packet sizing [SRC] halmac_88xx_cfg.h:29,38, halmac_init_88xx.c:60, h2c_extra_info_nic.h:25
TX_DESC_SIZE_88XX = 48
OCPBASE_TXBUF_88XX = 0x18780000
OCPBASE_DMEM_88XX = 0x00200000          # mem_addr < this => IMEM, else DMEM [SRC] halmac_88xx_cfg.h:39
DLFW_PKT_MAX_SIZE = 8192
DLFW_RSVDPG_SIZE = 2048
DLFW_USB_PKT_SIZE = 0x1000              # USB caps the per-packet FW chunk [SRC] hal_halmac.c:1099
DLFW_RESTORE_REG_NUM = 6               # [SRC] hal_halmac.c:23

# TX descriptor (the FW packet is a BEACON-qsel rsvd-page TX) [SRC] halmac_tx_desc_nic.h:131-435,
# rtl8822bu_halmac.c:127-196 usb_write_data_not_xmitframe, halmac_common_8822b.c fill_txdesc_check_sum
TXDESC_QSEL_BEACON = 0x10               # [SRC] halmac_type.h:634
TXDESC_QSEL_H2C_CMD = 0x13             # [SRC] halmac_type.h:637 (H2C uses no OFFSET)
PACKET_OFFSET_SZ = 8

# --- _send_general_info: the two FW-offload H2C packets ----------------------
# [SRC] hal_halmac.c _send_general_info + halmac_fw_88xx.c:1115,1142 proc_send_*_info_88xx +
# halmac_common_88xx.c:614 set_h2c_pkt_hdr_88xx + halmac_fw_offload_h2c_nic.h field setters.
H2C_PKT_SIZE = 32                      # [SRC] halmac_fw_88xx.h:32 (only 32-byte H2C supported)
H2C_PKT_HDR_SIZE = 8                   # [SRC] :33
H2C_CATEGORY = 0x01                    # FW_OFFLOAD_H2C_SET_CATEGORY [SRC] set_h2c_pkt_hdr_88xx:625
H2C_CMD_ID = 0xFF                      # FW_OFFLOAD_H2C_SET_CMD_ID :626
SUB_CMD_ID_GENERAL_INFO = 0x0D
SUB_CMD_ID_PHYDM_INFO = 0x11
# general_info struct fields. rfe_type / cut_ver are computed (efuse / chip_ver); the rest come
# from the driver's get_trx_path + PackageType for this card (rf_type 2T2R->4, single-path ant
# status, no ext-PA, package 0, non-MP) — [WIRE]-confirmed by the two H2C packets.
GENINFO_RF_TYPE = 0x04                 # _rf_type_drv2halmac(RF_2T2R)
GENINFO_TX_ANT_STATUS = 0x1
GENINFO_RX_ANT_STATUS = 0x1
GENINFO_EXT_PA = 0x0
GENINFO_PACKAGE_TYPE = 0x0
GENINFO_MP_MODE = 0x0

# Pre-download TX-FIFO-empty gate [SRC] halmac_common_88xx.c:3271 txfifo_is_empty_88xx (chk=10)
REG_TXPKT_EMPTY = 0x041A

# download_firmware reg-backup + setup [SRC] halmac_fw_88xx.c:115-192
REG_TXDMA_PQ_MAP = 0x010C              # +1 = HALMAC_DMA_MAPPING_HIGH<<6 = 0xC0
HALMAC_DMA_MAPPING_HIGH = 3
BIT_HCI_TXDMA_EN = 1 << 0              # REG_CR
BIT_TXDMA_EN = 1 << 2
REG_H2CQ_CSR = 0x1330                  # set BIT(31)
REG_FIFOPAGE_INFO_1 = 0x0230           # set 0x200
REG_RQPN_CTRL_2 = 0x022C               # set BIT(31)
REG_BCN_CTRL = 0x0550                  # clear BIT(3), set BIT(4)
REG_TXDMA_STATUS = 0x0210              # dlfw_end_flow: write BIT(2)
REG_SYS_CLK_CTRL = 0x0008              # pltfm_reset clock-sync (8822b): +1 BIT(6)
REG_FW_DBG7 = 0x10FC

# dl_rsvd_page (the bulk send bracket) [SRC] halmac_common_88xx.c:314
REG_FIFOPAGE_CTRL_2 = 0x0204           # bcn-head | BIT(15); +1 BIT(7) = bcn-valid poll
BIT_MASK_BCN_HEAD_1_V1 = 0xFFF
REG_FWHW_TXQ_CTRL = 0x0420             # +2 BIT(6)
RSVD_PG_BOUNDARY_FWDL = 0              # txff_alloc.rsvd_boundary is still 0 at FW-download time [WIRE]

# iDDMA copy engine [SRC] halmac_fw_88xx.c:754,783, halmac_reg2.h:6657-6659, halmac_bit2.h
REG_DDMA_CH0SA = 0x1200
REG_DDMA_CH0DA = 0x1204
REG_DDMA_CH0CTRL = 0x1208
BIT_DDMACH0_OWN = 1 << 31
BIT_DDMACH0_CHKSUM_EN = 1 << 29
BIT_DDMACH0_CHKSUM_STS = 1 << 27       # set after a copy => checksum mismatch
BIT_DDMACH0_RESET_CHKSUM_STS = 1 << 25
BIT_DDMACH0_CHKSUM_CONT = 1 << 24
BIT_MASK_DDMACH0_DLEN = 0x3FFFF

# MCUFW_CTRL DL-OK / ready bits [SRC] halmac_bit2.h:12842,12993-13096, halmac_fw_88xx.c:648-661
BIT_FW_DW_RDY = 1 << 14
BIT_IMEM_DW_OK = 1 << 3
BIT_IMEM_CHKSUM_OK = 1 << 4
BIT_DMEM_DW_OK = 1 << 5
BIT_DMEM_CHKSUM_OK = 1 << 6
MCUFW_CTRL_IDMEM_CHKSUM = 0x50         # (IMEM_CHKSUM_OK | DMEM_CHKSUM_OK) — end-flow gate

# LTECOEX indirect access (backed up/restored around DL) [SRC] halmac_common_88xx.c:3338, reg2.h:8232
LTECOEX_ACCESS_CTRL = 0x1700           # +3 BIT(5) = ready
REG_LTECOEX_WRITE_DATA = 0x1704
REG_LTECOEX_READ_DATA = 0x1708
LTECOEX_REG_OFFSET_DL = 0x38

# --- MAC init for RX: init_trx_cfg (queue/FIFO/TRX) + init_h2c ----------------
# [SRC] halmac_init_8822b.c:477,521,643,792 + halmac_init_88xx.c:812,867
# TX-FIFO page allocation [SRC] halmac_8822b_cfg.h:24-25,38-44, halmac_88xx_cfg.h:24
TX_FIFO_SIZE_8822B = 262144            # >> TX_PAGE_SIZE_SHIFT (7) = 2048 pages
RX_FIFO_SIZE_8822B = 24576
TX_PAGE_SIZE_SHIFT = 7
C2H_PKT_BUF = 256                      # RXFF_BNDY = rx_fifo_size - this - 1
RX_DESC_DUMMY_SIZE_8822B = 72
RSVD_PG_H2C_EXTRAINFO_NUM = 24
RSVD_PG_H2C_STATICINFO_NUM = 8
RSVD_PG_H2CQ_NUM = 8
RSVD_PG_CPU_INSTRUCTION_NUM = 0
RSVD_PG_FW_TXBUF_NUM = 4
RSVD_PG_CSIBUF_NUM = 0
# rsvd_drv_pg_num: the morrownr driver's reserved-page need rounds up to HALMAC_RSVD_PG_NUM8
# (8 pages) via _cfg_drv_rsvd_pg_num before init_mac_cfg. [SRC] hal_halmac.c:3132,2861-2940;
# [WIRE] confirmed by rsvd_boundary=0x07CC (1996) and h2cq_addr=0x3FA00.
RSVD_PG_DRV_NUM_8822BU = 8

# DMA-channel ids [SRC] halmac_type.h:596-599 (EXTRA=0, LOW=1, NORMAL=2, HIGH=3)
DMA_MAPPING_EXTRA, DMA_MAPPING_LOW, DMA_MAPPING_NORMAL, DMA_MAPPING_HIGH = 0, 1, 2, 3
# REG_TXDMA_PQ_MAP field shifts (each AC -> a 2-bit DMA channel) [SRC] halmac_bit2.h:18939-19075
TXDMA_MAP_SHIFTS = {                    # voq, viq, beq, bkq, mgq, hiq
    "vo": 4, "vi": 6, "be": 8, "bk": 10, "mg": 12, "hi": 14,
}
# This card: 3 bulk-OUT, NORMAL mode. RQPN_3BULKOUT[NORMAL] / PG_NUM_3BULKOUT[NORMAL].
# [SRC] halmac_init_8822b.c:203-205,295. Other modes/bulkout-counts are not ported (wifit3 is
# monitor/NORMAL-only and the T3U Plus is 3-bulkout — verified by the 0xF5A0 queue map).
BULKOUT_NUM_8822BU = 3
RQPN_NORMAL_3BULKOUT = {               # AC -> DMA channel
    "vo": DMA_MAPPING_NORMAL, "vi": DMA_MAPPING_NORMAL,
    "be": DMA_MAPPING_LOW, "bk": DMA_MAPPING_LOW,
    "mg": DMA_MAPPING_HIGH, "hi": DMA_MAPPING_HIGH,
}
PG_NUM_NORMAL_3BULKOUT = {"hq": 64, "nq": 64, "lq": 64, "exq": 0, "gap": 1}

MAC_TRX_ENABLE = 0xFF                  # [SRC] halmac_8822b_cfg.h:48 (all 8 TRX/sched/MAC enable bits)
HALMAC_TRNSFER_NORMAL = 0x0

# init_trx_cfg / init_h2c registers [SRC] halmac_reg2.h
REG_TXDMA_PQ_MAP = 0x010C              # +1 = PQ priority map already set in FW DL; here the 16-bit map
REG_RXFF_BNDY = 0x011C
REG_AUTO_LLT_V1 = 0x0208               # BIT(0)=auto-init-LLT; +3 = blk-desc num
BIT_AUTO_INIT_LLT_V1 = 1 << 0
BLK_DESC_NUM = 3
BIT_SHIFT_BLK_DESC_NUM = 4
BIT_MASK_BLK_DESC_NUM = 0xF
REG_TXDMA_OFFSET_CHK = 0x020C          # +1 BIT(1); +1 BIT(7) in init_h2c
REG_FIFOPAGE_INFO_2 = 0x0234
REG_FIFOPAGE_INFO_3 = 0x0238
REG_FIFOPAGE_INFO_4 = 0x023C
REG_FIFOPAGE_INFO_5 = 0x0240
REG_H2C_HEAD = 0x0244
REG_H2C_TAIL = 0x0248
REG_H2C_READ_ADDR = 0x024C
REG_H2C_INFO = 0x0254
REG_FWFF_CTRL = 0x029C
REG_FWFF_PKT_INFO = 0x02A0
REG_BCNQ_BDNY_V1 = 0x0424
REG_BCNQ1_BDNY_V1 = 0x0456
REG_WMAC_FWPKT_CR = 0x0601             # BIT(7) = FWEN
BIT_FWEN = 1 << 7
REG_RX_DRVINFO_SZ = 0x060F
REG_H2C_PKT_READADDR = 0x10D0
REG_H2C_PKT_WRITEADDR = 0x10D4

# --- init_protocol_cfg / init_edca_cfg / init_wmac_cfg -----------------------
# [SRC] halmac_init_8822b.c:750,851,896 + the WLAN_* config values in halmac_init_88xx.h:54-93.
# init_protocol_cfg
REG_SW_AMPDU_BURST_MODE_CTRL = 0x04BC   # clear BIT(6)
REG_AMPDU_MAX_TIME_V1 = 0x0455
WLAN_AMPDU_MAX_TIME = 0x70
REG_TX_HANG_CTRL = 0x045E               # set BIT_EN_EOF_V1
BIT_EN_EOF_V1 = 1 << 2
REG_PROT_MODE_CTRL = 0x04C8
# WLAN_RTS_LEN_TH | TX_TIME_TH<<8 | MAX_AGG_PKT_LIMIT<<16 | RTS_MAX_AGG_PKT_LIMIT<<24
WLAN_PROT_MODE_CTRL = 0xFF | (0x08 << 8) | (0x20 << 16) | (0x20 << 24)   # 0x202008FF
REG_BAR_MODE_CTRL = 0x04CC              # +2: WLAN_BAR_RETRY_LIMIT | RA_TRY_RATE_AGG_LIMIT<<8
WLAN_BAR_MODE_CTRL_HI = 0x01 | (0x08 << 8)                               # 0x0801
REG_FAST_EDCA_VOVI_SETTING = 0x1448     # +0 VO, +2 VI
REG_FAST_EDCA_BEBK_SETTING = 0x144C     # +0 BE, +2 BK
WLAN_FAST_EDCA_TH = 0x06                # VO/VI/BE/BK all 0x06
REG_INIRTS_RATE_SEL = 0x0480            # set BIT(5)
# init_edca_cfg
REG_TIMER0_SRC_SEL = 0x05B4             # clear BIT(4|5|6)
REG_TXPAUSE = 0x0522
REG_SLOT = 0x051B
WLAN_SLOT_TIME = 0x09
REG_PIFS = 0x0512
WLAN_PIFS_TIME = 0x19
REG_SIFS = 0x0514
WLAN_SIFS_CFG = 0x10100E0A              # CCK/OFDM cont-tx + trx SIFS (composite) [WIRE]
REG_EDCA_VO_PARAM = 0x0500              # +2: WLAN_VO_TXOP_LIMIT
REG_EDCA_VI_PARAM = 0x0504              # +2: WLAN_VI_TXOP_LIMIT
WLAN_VO_TXOP_LIMIT = 0x0186
WLAN_VI_TXOP_LIMIT = 0x03BC
REG_RD_NAV_NXT = 0x0544
WLAN_NAV_CFG = 0x001B0005               # RDG_NAV | TXOP_NAV<<16
REG_RXTSF_OFFSET_CCK = 0x055E
WLAN_RX_TSF_CFG = 0x3030                # CCK_RX_TSF | OFDM_RX_TSF<<8
BIT_EN_BCN_FUNCTION = 1 << 3            # REG_BCN_CTRL
REG_TBTT_PROHIBIT = 0x0540
WLAN_TBTT_TIME = 0x00006404             # TBTT_PROHIBIT | TBTT_HOLD_TIME<<8
REG_DRVERLYINT = 0x0558
WLAN_DRV_EARLY_INT = 0x04
REG_BCNDMATIM = 0x0559
WLAN_BCN_DMA_TIME = 0x02
REG_TX_PTCL_CTRL = 0x0520               # +1: clear BIT(4)
# init_wmac_cfg
REG_RXFLTMAP0 = 0x06A0
WLAN_RX_FILTER0 = 0x0FFFFFFF
REG_RXFLTMAP2 = 0x06A4
WLAN_RX_FILTER2 = 0xFFFF
REG_RCR = 0x0608
WLAN_RCR_CFG = 0xE400220E
REG_RX_PKT_LIMIT = 0x060C
WLAN_RXPKT_MAX_SZ_512 = 12288 >> 9      # 24
REG_TCR = 0x0604                        # +1: TX_FUNC_CFG1, +2: TX_FUNC_CFG2
WLAN_TX_FUNC_CFG1 = 0x30
WLAN_TX_FUNC_CFG2 = 0x30
REG_WMAC_TRXPTCL_CTL = 0x0668           # +4: set BIT(1)
REG_SND_PTCL_CTRL = 0x0718              # set BIT_R_DISABLE_CHECK_VHTSIGB_CRC
BIT_R_DISABLE_CHECK_VHTSIGB_CRC = 1 << 6
REG_WMAC_OPTION_FUNCTION = 0x07D0       # +8: MAC_OPT_FUNC2, +4: MAC_OPT_NORM_FUNC1
WLAN_MAC_OPT_FUNC2 = 0x30810041
WLAN_MAC_OPT_NORM_FUNC1 = 0x98

# --- init_mac_flow driver tail: RTS-full-bw + USB RX aggregation -------------
# [SRC] hal_halmac.c init_mac_flow:3452 (HW_VAR_RCR sync, set_rts_full_bw, rx_agg_switch);
# cfg_usb_rx_agg_88xx [SRC] halmac_usb_88xx.c:88; cfg_operation_mode/init_low_pwr are no-ops.
REG_RXDMA_AGG_PG_TH = 0x0280            # +3 = DMA/USB agg select (BIT7); 16-bit = size|to<<8
BIT_RXDMA_AGG_EN = 1 << 2              # in REG_TXDMA_PQ_MAP
BIT_EN_PRE_CALC = 1 << 29             # size-limit pre-calc in REG_RXDMA_AGG_PG_TH
BIT_SHIFT_DMA_AGG_TO = 8
# USB RX-agg mode (morrownr default rxagg_mode = RX_AGG_USB). size/timeout from the link check:
# REG_SYS_CFG2+3 == 0x20 -> USB3 (5/0xA) else (5/0x20). [WIRE] reads 0x80 here -> the else branch.
RXAGG_USB_SIZE = 0x5
RXAGG_USB_TIMEOUT_USB3 = 0xA
RXAGG_USB_TIMEOUT_OTHER = 0x20

# --- power-state detection markers (mac_pwr_switch_usb_8822b) --------------
# [SRC] hal/halmac/halmac_88xx/halmac_8822b/halmac_usb_8822b.c:44-92
REG_RPWM = 0xFE58                      # :44  (RPWM — leave-32K toggle)
MCUFW_CTRL_FW_EXIST = 0xC078           # :47  REG_MCUFW_CTRL value == FW still loaded
REG_CR_DISABLED = 0xEA                 # :54  REG_CR value == chip already disabled

# --- EFUSE read (HALMAC physical-map dump + 8822b logical parse) -----------
# The chip-info probe reads the EFUSE up front (before power-on): rtl8822b_read_efuse
# -> EFUSE_ShadowMapUpdate -> halmac dump_efuse_drv_88xx -> read_hw_efuse_88xx.
# [SRC] hal/rtl8822b/rtl8822b_ops.c:616, hal/halmac/halmac_88xx/halmac_efuse_88xx.c:1507,1089
REG_SYS_EEPROM_CTRL = 0x000A           # [SRC] halmac_reg_8822b.h:23 (autoload/eeprom-sel flags)
BIT_AUTOLOAD_SUS = 1 << 5              # [SRC] halmac_bit_8822b.h:129 — set => autoload OK
BIT_EERPOMSEL = 1 << 4                 # [SRC] halmac_bit_8822b.h:130 — set => EEPROM, else eFuse
REG_EFUSE_CTRL = 0x0030                # [SRC] halmac_reg_8822b.h:34 (32-bit access/data/addr)
REG_LDO_EFUSE_CTRL = 0x0034            # [SRC] halmac_reg_8822b.h:35 (+1 bank, +3 LDO25 enable)

# REG_EFUSE_CTRL (0x30) field layout [SRC] halmac_bit_8822b.h:688,726-738
BIT_EF_FLAG = 1 << 31                  # read/write-done strobe (poll until set on read)
BIT_SHIFT_EF_ADDR = 8
BIT_MASK_EF_ADDR = 0x3FF               # physical byte address [17:8]
BITS_EF_ADDR = BIT_MASK_EF_ADDR << BIT_SHIFT_EF_ADDR
BIT_MASK_EF_DATA = 0xFF                # data byte [7:0]

# Map sizes [SRC] halmac_88xx/halmac_8822b/halmac_8822b_cfg.h:55-58 + halmac_efuse_88xx.c:23-24
EFUSE_SIZE_8822B = 1024                # physical map (read addr 0..1023)
EEPROM_SIZE_8822B = 768                # logical map produced by the PG-header parser
PRTCT_EFUSE_SIZE_8822B = 96            # protected tail (bounds the parser walk)
HALMAC_EFUSE_BANK_WIFI = 0             # [SRC] halmac_type.h:1771

# Logical-map field offsets (8822BU) [SRC] include/hal_pg.h:453-479
EEPROM_CHANNEL_PLAN = 0x00B8           # :453
EEPROM_XTAL = 0x00B9                   # :454  crystal_cap
EEPROM_THERMAL_METER = 0x00BA          # :455
EEPROM_VERSION = 0x00C4                # :470
EEPROM_RFE_OPTION = 0x00CA             # :475  rfe_type (RF front-end variant)
EEPROM_MAC_ADDR = 0x0107               # :479  (the 8822bU MAC sits past the 256-byte page)
EFUSE_PA_BIAS = 0x03D7                  # physical efuse PA-bias pair [SRC] rtl8822b_ops.c:560
# Field defaults when the efuse byte is blank (0xFF) or the map is invalid.
EEPROM_DEFAULT_CRYSTAL_CAP = 0x00      # [SRC] hal_pg.h:841 EEPROM_Default_CrystalCap (8822b uses generic)
EEPROM_DEFAULT_THERMAL_METER = 0x12    # [SRC] hal_pg.h:827 EEPROM_Default_ThermalMeter
