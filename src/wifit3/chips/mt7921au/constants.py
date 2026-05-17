# MT7921AU Constants

# Vendor Requests (Control Transfers)
MT_VEND_REQ_IN           = 0xc0
MT_VEND_REQ_OUT          = 0x40
MT_VEND_REQ_BOOT_STATUS  = 0x01

# CONNAC2 Standard Register Bus (Direct link, bmRequestType device=0)
MT_VEND_READ_REG_REQ    = 0x63
MT_VEND_WRITE_REG_REQ   = 0x66

# CONNAC2 Unified Register Bus (recipient=31, verified from pcap)
# 0x5F = Host-to-Device OUT, vendor, recipient=31
# 0xDF = Device-to-Host IN,  vendor, recipient=31
MT_VEND_WRITE_RECIPIENT = 0x5F
MT_VEND_READ_RECIPIENT  = 0xDF

# CONNAC2 UHW Bus (USB-Host-Wrapper, for low-level USB controller registers).
# Uses bmRequestType=0x5E (OUT) / 0xDE (IN), bRequest=0x02 (write) / 0x01 (read).
MT_UHW_WRITE_RECIPIENT  = 0x5E
MT_UHW_READ_RECIPIENT   = 0xDE
MT_VEND_DEV_MODE        = 0x01   # UHW read bRequest
MT_VEND_WRITE           = 0x02   # UHW write bRequest

# MT_SSUSB_EPCTL_CSR_EP_RST_OPT — USB endpoint reset-option register.
# Bits 4-9: out-bulk EP 4-9 reset; bits 20-22: in-bulk EP 4-5 + in-int EP 6.
# Linux's mt792xu_epctl_rst_opt(false) CLEARS these to release the endpoints
# from reset state. Without it, OUT endpoints may accept the first burst of
# packets but stall on subsequent ones.
MT_SSUSB_EPCTL_CSR_EP_RST_OPT       = 0x74011890
MT_EPCTL_RST_OPT_OUT_BLK_EP_4_9     = 0x000003F0   # GENMASK(9, 4)
MT_EPCTL_RST_OPT_IN_EP_4_5_6        = 0x00700000   # GENMASK(22, 20)

# Firmware filenames
FIRMWARE_ROM_PATCH = "WIFI_MT7961_patch_mcu_1_2_hdr.bin"
FIRMWARE_WM        = "WIFI_RAM_CODE_MT7961_1.bin"

# USB Interface & Endpoints (verified from usb_dumps/captures_mt7921u/capture-3.pcap)
# The MT7921AU is a composite device. Interface 3 (vendor-specific) is the one
# the mt76 driver actually uses; interfaces 0-2 are standard-wireless-class.
# All bulk endpoints below live on Interface 3.
INTERFACE_NUM = 3

EP_OUT_MCU  = 0x08   # MT_EP_OUT_INBAND_CMD: 64+ byte MCU commands
EP_OUT_FW   = 0x04   # MT_EP_OUT_AC_BE: FW_SCATTER chunks + 802.11 TX data
EP_OUT_DATA = 0x04   # alias — same physical EP as EP_OUT_FW
EP_IN_BULK  = 0x84   # MT_EP_IN_PKT_RX: RX 802.11 frames
EP_IN_MCU   = 0x85   # MT_EP_IN_CMD_RESP: MCU command responses

# MCU Command IDs (mt76_connac_mcu.h enum)
MCU_CMD_TARGET_ADDRESS_LEN_REQ = 0x01   # per RAM region: addr/len/mode
MCU_CMD_FW_START_REQ           = 0x02   # boot the loaded firmware
MCU_CMD_PATCH_START_REQ        = 0x05   # per patch section: addr/len/mode
MCU_CMD_PATCH_FINISH_REQ       = 0x07   # close patch session
MCU_CMD_PATCH_SEM_CONTROL      = 0x10   # acquire/release patch download semaphore
MCU_CMD_FW_SCATTER             = 0x0c   # bulk-data marker (no MCU TXD on wire)
MCU_CMD_TX_MGMT                = 0x20
MCU_UNI_CMD_SNIFFER            = 0x24
MCU_UNI_CMD_CH_SWITCH          = 0x34

# PATCH_SEM_CONTROL operations
PATCH_SEM_GET     = 0x01
PATCH_SEM_RELEASE = 0x00

# Response codes for PATCH_SEM_CONTROL
PATCH_NOT_DL_SEM_FAIL    = 0x00
PATCH_IS_DL              = 0x01
PATCH_NOT_DL_SEM_SUCCESS = 0x02
PATCH_REL_SEM_SUCCESS    = 0x03

# DL_MODE flag set in PATCH_START_REQ / TARGET_ADDRESS_LEN_REQ payloads.
# mt76_connac_mcu.h:21 — DL_MODE_NEED_RSP = BIT(31). Tells the boot ROM /
# patch firmware to send a response on EP 0x85 after consuming the region.
# Verified against capture-3.pcap frame 14186 byte 76 (00 00 00 80 LE → 0x80000000).
DL_MODE_NEED_RSP = 0x80000000

# RAM region feature_set bits
FW_FEATURE_OVERRIDE_ADDR = 0x20   # BIT(5)
FW_FEATURE_NON_DL        = 0x40   # BIT(6)

# FW_START_REQ option flags
FW_START_OVERRIDE        = 0x01   # BIT(0): use override addr to start
FW_START_WORKING_PDA_CR4 = 0x04   # BIT(2): is_wa

# MCU TXD field constants
MCU_PKT_ID   = 0xA0   # mt76_connac2_mcu_txd.pkt_type
MCU_Q_QUERY  = 0
MCU_Q_SET    = 1
MCU_Q_NA     = 3      # set_query when cmd has no ext_cid / CE flag
MCU_S2D_H2N  = 0      # host-to-N9 direction

# Wire-format sizes
SDIO_HDR_SIZE    = 4    # mt792x_skb_add_usb_sdio_hdr prepends this
MCU_TXD_SIZE     = 64   # mt76_connac2_mcu_txd
MAX_FW_CHUNK     = 4096 # matches Linux's mt76u USB chunk size

# txd[0]/txd[1] constants for connac2 MCU commands (frame 14182, 14186 etc.)
# txd[0] = MT_TXD0_TX_BYTES(skb_len) | MT_TXD0_PKT_FMT(2)<<23 | MT_TXD0_Q_IDX(0x20)<<25
TXD0_BASE = (0x20 << 25) | (0x02 << 23)   # = 0x41000000
# txd[1] = MT_TXD1_LONG_FORMAT(BIT 31) | MT_TXD1_HDR_FORMAT(MT_HDR_FORMAT_CMD=1)<<16
TXD1_CMD  = (1 << 31) | (1 << 16)         # = 0x80010000
# pq_id = MCU_PQ_ID(MT_TX_PORT_IDX_MCU=1, MT_TX_MCU_PORT_RX_Q0=0x20)
#       = (1<<15) | (0x20<<10) = 0x8000
MCU_PQ_ID = 0x8000

# Patch / RAM file structure sizes (from mt76_connac_mcu.h)
PATCH_HDR_SIZE     = 96     # mt76_connac2_patch_hdr
PATCH_SEC_SIZE     = 64     # mt76_connac2_patch_sec
FW_TRAILER_SIZE    = 36     # mt76_connac2_fw_trailer (at end of WM file)
FW_REGION_SIZE     = 40     # mt76_connac2_fw_region (one per region, before trailer)
PATCH_SEC_TYPE_INFO = 0x02  # PATCH_SEC_TYPE_MASK match for info-section

# Descriptor Sizes
TXD_SIZE = 80
RXD_SIZE = 32

# TXD DW0 Fields
TXD_DW0_OWNER_NIC = 0x80000000

# TXD DW1 Fields
TXD_DW1_WLAN_IDX_MASK = 0x3FF
TXD_DW1_Q_IDX_SHIFT = 12

# TXD DW3 Fields
TXD_DW3_FIX_RATE = 0x8000

# RXD Fields
RXD_DW0_LEN_MASK = 0x3FFF
RXD_DW1_FCS_ERR  = 0x00010000

# Vendor request opcodes (bRequest field)
MT_VEND_POWER_ON       = 0x04   # mt792xu_mcu_power_on: wValue=0, wIndex=1

# Registers
# MT_HW_CHIPID: lower 16 bits contain chip ID (0x7921 for MT7921)
MT_CHIP_ID_ADDR        = 0x70010200  # confirmed from pcap frame 112 (standard bus)
MT_CHIP_ID_EXPECTED    = 0x7961  # MT7921AU (USB variant); PCI variant would be 0x7921
MT_USB_SCRATCH_ADDR    = 0x00000410
# MT_UMAC(0x008) — accessed via unified bus (0x5F/0xDF), confirmed from pcap
MT_UDMA_TX_QSEL        = 0x74000008
MT_FW_DL_EN            = 0x08       # BIT(3)

# MT_UWFDMA0_GLO_CFG — the DMA engine's master enable register.
# Without TX_DMA_EN, the device's TX FIFO fills with one chunk's worth of data
# (4096 bytes = 4 max-packet-size USB packets) and then NAKs subsequent bulk
# OUTs forever — host gets timeout/short-write.
MT_UWFDMA0_GLO_CFG                       = 0x7c024208
MT_WFDMA0_GLO_CFG_TX_DMA_EN              = 0x00000001   # BIT(0)
MT_WFDMA0_GLO_CFG_RX_DMA_EN              = 0x00000004   # BIT(2)
MT_WFDMA0_GLO_CFG_RX_DMA_BUSY            = 0x00000008   # BIT(3)
MT_WFDMA0_GLO_CFG_FW_DWLD_BYPASS_DMASHDL = 0x00000200   # BIT(9)
MT_WFDMA0_GLO_CFG_OMIT_RX_INFO_PFET2     = 0x00200000   # BIT(21)
MT_WFDMA0_GLO_CFG_OMIT_RX_INFO           = 0x08000000   # BIT(27)
MT_WFDMA0_GLO_CFG_OMIT_TX_INFO           = 0x10000000   # BIT(28)

# MT_WFDMA_HOST_CONFIG — routes USB RX events to EP 4.
MT_WFDMA_HOST_CONFIG                     = 0x7c027030
MT_WFDMA_HOST_CONFIG_USB_RXEVT_EP4_EN    = 0x00000040   # BIT(6)

# MT_UDMA_WLCFG_0/_1 — DMA TX/RX enables and timing.
# Linux's mt792xu_dma_init writes these before firmware download.
MT_UDMA_WLCFG_1        = 0x7400000c
MT_WL_RX_AGG_PKT_LMT   = 0x000000FF   # GENMASK(7, 0)

MT_UDMA_WLCFG_0        = 0x74000018
MT_WL_RX_AGG_TO        = 0x000000FF   # GENMASK(7, 0)
MT_WL_RX_AGG_LMT       = 0x0000FF00   # GENMASK(15, 8)
MT_WL_RX_MPSZ_PAD0     = 0x00040000   # BIT(18)
MT_WL_RX_FLUSH         = 0x00080000   # BIT(19)
MT_TICK_1US_EN         = 0x00100000   # BIT(20)
MT_WL_RX_AGG_EN        = 0x00200000   # BIT(21)
MT_WL_RX_EN            = 0x00400000   # BIT(22)
MT_WL_TX_EN            = 0x00800000   # BIT(23)

# DMA Scheduler (MT_DMA_SHDL block at 0x7c026000) — mt792xu_wfdma_init setup.
# Without this block, the WM firmware boots into a state where its TX scheduler
# can't dispatch work, so the chip never asserts FW_N9_RDY after FW_START_REQ.
# Verified pre-patch in capture-3 frames 14102-14136 (16 GROUP_QUOTA + 4 Q_MAP
# + 2 SCHED_SET writes plus 3 RMW header registers).
def MT_DMASHDL_GROUP_QUOTA(n: int) -> int: return 0x7c026020 + (n << 2)
def MT_DMASHDL_Q_MAP(n: int)       -> int: return 0x7c026060 + (n << 2)
def MT_DMASHDL_SCHED_SET(n: int)   -> int: return 0x7c026070 + (n << 2)
MT_DMASHDL_PAGE                 = 0x7c02600c
MT_DMASHDL_GROUP_SEQ_ORDER      = 0x00010000   # BIT(16)
MT_DMASHDL_REFILL               = 0x7c026010
MT_DMASHDL_REFILL_MASK          = 0xFFFF0000   # GENMASK(31, 16)
MT_DMASHDL_PKT_MAX_SIZE         = 0x7c02601c
MT_DMASHDL_PKT_MAX_SIZE_PLE     = 0x00000FFF   # GENMASK(11, 0)
MT_DMASHDL_PKT_MAX_SIZE_PSE     = 0x0FFF0000   # GENMASK(27, 16)
MT_DMASHDL_GROUP_QUOTA_MIN_SHIFT = 0
MT_DMASHDL_GROUP_QUOTA_MAX_SHIFT = 16

# MT_WFDMA_DUMMY_CR (MT_MCU_WPDMA0 base 0x54000000) — set NEED_REINIT after
# DMASHDL setup completes (pcap frames 14138/14140).
MT_WFDMA_DUMMY_CR               = 0x54000120
MT_WFDMA_NEED_REINIT            = 0x00000002   # BIT(1)
# mt792x_regs.h: MT_CONN_ON_MISC = 0x7c0600f0 — accessed via unified bus
MT_CONN_ON_MISC           = 0x7c0600f0
MT_TOP_MISC2_FW_PWR_ON    = 0x1     # BIT(0): MCU power-on done
MT_TOP_MISC2_FW_N9_RDY    = 0x3     # GENMASK(1,0): firmware fully ready
