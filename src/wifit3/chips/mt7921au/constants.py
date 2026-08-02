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

# USB Interface & Endpoints. The vendor-specific (class 0xFF) interface owns all
# the bulk endpoints below; it is detected at runtime, not hardcoded, because its
# number differs per unit: the Panda PAU0F is a single-interface device (interface
# 0); the ALFA AWUS036AXML is a composite device whose wifi function is interface 3.
EP_OUT_MCU  = 0x08   # MT_EP_OUT_INBAND_CMD: 64+ byte MCU commands
EP_OUT_FW   = 0x04   # MT_EP_OUT_AC_BE: FW_SCATTER chunks + 802.11 TX data
EP_OUT_DATA = 0x04   # alias — same physical EP as EP_OUT_FW
EP_IN_BULK  = 0x84   # MT_EP_IN_PKT_RX: RX 802.11 frames (+ MCU resp once RXEVT_EP4_EN set)
EP_IN_MCU   = 0x85   # MT_EP_IN_CMD_RESP: MCU command responses (RXEVT_EP4_EN cleared)

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

# --- RX descriptor (connac2, mt7921_mac_fill_rx + mt7921_queue_rx_skb demux) ---
# rxd0: PKT_TYPE GENMASK(31,27), PKT_FLAG GENMASK(19,16), LENGTH GENMASK(15,0).
# MT_RXD0_LENGTH is the RX byte count (RXD + MPDU; the HW has already stripped the
# FCS) — the rest of the USB buffer is alignment padding. Truncate the delivered
# frame to it so frame_end == MPDU_end.
MT_RXD0_LENGTH      = 0x0000FFFF   # GENMASK(15, 0)
PKT_TYPE_NORMAL     = 2    # 802.11 frame
PKT_TYPE_RX_EVENT   = 7    # MCU response/event
PKT_TYPE_NORMAL_MCU = 8    # RX_EVENT with flag 0x1 -> treated as a normal frame
# rxd1
MT_RXD1_NORMAL_WLAN_IDX = 0x000003FF   # GENMASK(9, 0)
MT_RXD1_NORMAL_GROUP_1  = 1 << 11
MT_RXD1_NORMAL_GROUP_2  = 1 << 12
MT_RXD1_NORMAL_GROUP_3  = 1 << 13
MT_RXD1_NORMAL_GROUP_4  = 1 << 14
MT_RXD1_NORMAL_GROUP_5  = 1 << 15
MT_RXD1_NORMAL_FCS_ERR  = 1 << 27
MT_RXD1_NORMAL_BAND_IDX = 1 << 28
# rxd2: HDR_OFFSET GENMASK(15,14) -> 2*remove_pad bytes of header padding.
MT_RXD2_NORMAL_HDR_OFFSET_SHIFT = 14
MT_RXD2_NORMAL_HDR_OFFSET_MASK  = 0x3

# ===========================================================================
# connac2 TX descriptor (mt76_connac2_mac_write_txwi) — for inject/TX (tx.py).
# Masks grepped verbatim from mt76_connac2_mac.h. The USB TX wire frame is
# [SDIO hdr 4B][TXD 64B][802.11 frame][pad]; 9 dwords (txwi[0..8]) are written.
# ===========================================================================
# MT_SDIO_TXD_SIZE = MT_TXD_SIZE(8*4) + 8*4 (mt76_connac.h).
MT_SDIO_TXD_SIZE = 64

MT_TXD0_Q_IDX     = 0xFE000000   # GENMASK(31, 25)
MT_TXD0_PKT_FMT   = 0x01800000   # GENMASK(24, 23)
MT_TXD0_TX_BYTES  = 0x0000FFFF   # GENMASK(15, 0)

MT_TXD1_LONG_FORMAT = 0x80000000  # BIT(31)
MT_TXD1_TGID        = 0x40000000  # BIT(30)
MT_TXD1_OWN_MAC     = 0x3F000000  # GENMASK(29, 24)
MT_TXD1_TID         = 0x00700000  # GENMASK(22, 20)
MT_TXD1_HDR_FORMAT  = 0x00030000  # GENMASK(17, 16)
MT_TXD1_HDR_INFO    = 0x0000F800  # GENMASK(15, 11)
MT_TXD1_VTA         = 0x00000400  # BIT(10)  — set only on !connac2
MT_TXD1_WLAN_IDX    = 0x000003FF  # GENMASK(9, 0)

MT_TXD2_FIX_RATE   = 0x80000000   # BIT(31)
MT_TXD2_FRAG       = 0x0000C000   # GENMASK(15, 14)
MT_TXD2_HTC_VLD    = 0x00002000   # BIT(13)
MT_TXD2_MULTICAST  = 0x00000400   # BIT(10)
MT_TXD2_FRAME_TYPE = 0x00000030   # GENMASK(5, 4)
MT_TXD2_SUB_TYPE   = 0x0000000F   # GENMASK(3, 0)

MT_TXD3_SN_VALID      = 0x80000000  # BIT(31)
MT_TXD3_SW_POWER_MGMT = 0x20000000  # BIT(29) — set only on !connac2
MT_TXD3_BA_DISABLE    = 0x10000000  # BIT(28)
MT_TXD3_SEQ           = 0x0FFF0000  # GENMASK(27, 16)
MT_TXD3_REM_TX_COUNT  = 0x0000F800  # GENMASK(15, 11)
MT_TXD3_PROTECT_FRAME = 0x00000002  # BIT(1)
MT_TXD3_NO_ACK        = 0x00000001  # BIT(0)

MT_TXD5_TX_STATUS_HOST = 0x00000400  # BIT(10)
MT_TXD5_PID            = 0x000000FF  # GENMASK(7, 0)
# mt76.h: a pktid >= MT_PACKET_ID_FIRST requests host TX status. Injection uses
# MT_PACKET_ID_NO_ACK (0) — no status tracking.
MT_PACKET_ID_NO_ACK = 0
MT_PACKET_ID_FIRST  = 3

MT_TXD6_TX_RATE  = 0x3FFF0000   # GENMASK(29, 16)
MT_TXD6_FIXED_BW = 0x00000004   # BIT(2)

MT_TXD7_HW_AMSDU = 0x00000400   # BIT(10)

MT_TXD8_L_TYPE     = 0x00000030  # GENMASK(5, 4)
MT_TXD8_L_SUB_TYPE = 0x0000000F  # GENMASK(3, 0)

# mt76_connac2_mac_tx_rate_val packed-rate fields.
MT_TX_RATE_IDX  = 0x0000003F   # GENMASK(5, 0)
MT_TX_RATE_MODE = 0x000003C0   # GENMASK(9, 6)
MT_TX_RATE_NSS  = 0x00001C00   # GENMASK(12, 10)

# enum values used by write_txwi.
MT_TX_TYPE_SF        = 1     # enum tx_pkt_type (USB packet format)
MT_HDR_FORMAT_802_11 = 2     # enum tx_header_format
MT_LMAC_ALTX0        = 0x10  # mgmt/PSD LMAC queue
MT_TXQ_PSD           = 4     # enum mt76_txq_id
MT_PHY_TYPE_OFDM     = 1     # mt76_rates hw_value high byte for 5 GHz
MT76_CONNAC_MAX_WMM_SETS = 4   # q_idx = wmm_idx * this + lmac_mapping(ac)

# mt792x_skb_add_usb_sdio_hdr (USB: pkt_type 0).
MT792x_SDIO_HDR_TX_BYTES = 0x0000FFFF   # GENMASK(15, 0)
MT792x_SDIO_HDR_PKT_TYPE = 0x00030000   # GENMASK(17, 16)

# USB bulk-OUT endpoints chosen by qid (mt76u q->ep): mgmt/PSD -> HCCA, data ->
# its AC endpoint. EP_OUT_DATA (0x04 = AC_BE) is defined above.
EP_OUT_HCCA = 0x09   # MT_EP_OUT_HCCA: qid == MT_TXQ_PSD (mgmt/deauth)

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

# MT_UWFDMA0_GLO_CFG — WFDMA engine master enable. mt792xu_wfdma_init clears
# OMIT_RX_INFO, then sets TX_DMA_EN | RX_DMA_EN | FW_DWLD_BYPASS_DMASHDL
# (+ OMIT_TX_INFO | OMIT_RX_INFO_PFET2). RX_DMA_EN requires the IN URB pool to be
# posted first (kernel order: alloc_queues before dma_init) — otherwise the RX
# path backs up and stalls the firmware-download bulk OUT. The USB-3 4-packet
# FW_SCATTER stall is a separate SuperSpeed link-flow-control issue (the device
# NAKs at 4096 B and never sends ERDY to a userland sync transfer), unrelated to
# these bits — it does not reproduce on USB-2 HighSpeed.
MT_UWFDMA0_GLO_CFG                       = 0x7c024208
MT_WFDMA0_GLO_CFG_TX_DMA_EN              = 0x00000001   # BIT(0)
MT_WFDMA0_GLO_CFG_RX_DMA_EN              = 0x00000004   # BIT(2)
MT_WFDMA0_GLO_CFG_RX_DMA_BUSY            = 0x00000008   # BIT(3)
MT_WFDMA0_GLO_CFG_FW_DWLD_BYPASS_DMASHDL = 0x00000200   # BIT(9)
MT_WFDMA0_GLO_CFG_OMIT_RX_INFO_PFET2     = 0x00200000   # BIT(21)
MT_WFDMA0_GLO_CFG_OMIT_RX_INFO           = 0x08000000   # BIT(27)
MT_WFDMA0_GLO_CFG_OMIT_TX_INFO           = 0x10000000   # BIT(28)

# MT_WFDMA_HOST_CONFIG — routes USB RX events (incl. MCU responses + the
# firmware-up signal) to EP 0x84 when RXEVT_EP4_EN is set (mt792xu_dma_rx_evt_ep4).
MT_WFDMA_HOST_CONFIG                     = 0x7c027030
MT_WFDMA_HOST_CONFIG_USB_RXEVT_EP4_EN    = 0x00000040   # BIT(6)

# mt792xu_dma_prefetch — TX-ring extended control (prefetch depth + base ptr).
# MT_UWFDMA0(ofs) = 0x7c024000 + ofs; TX_RING_EXT_CTRL(n) = MT_UWFDMA0(0x600 + (n<<2)).
def MT_UWFDMA0_TX_RING_EXT_CTRL(n: int) -> int: return 0x7c024600 + (n << 2)
MT_WPDMA0_MAX_CNT_MASK   = 0x000000FF   # GENMASK(7, 0)
MT_WPDMA0_BASE_PTR_MASK  = 0xFFFF0000   # GENMASK(31, 16)
# (ring_idx, max_cnt, base_ptr) per DMA_PREFETCH_CONF.
MT_DMA_PREFETCH_CONF = [(0, 4, 0x080), (1, 4, 0x0c0), (2, 4, 0x100), (3, 4, 0x140),
                        (4, 4, 0x180), (16, 4, 0x280), (17, 4, 0x2c0)]

# MT_SSUSB_EPCTL_CSR_EP_RST_OPT = MT_SSUSB_EPCTL_CSR(0x090) = 0x74011800 + 0x090.
# mt792xu_epctl_rst_opt(false) clears GENMASK(9,4) | GENMASK(22,20) — the reset-option
# bits for out blk ep 4-9, in blk ep 4-5, in int ep 6. The kernel reaches it over the
# UHW bus (Errno 5 on WinUSB); the register is also reachable over the unified bus, which
# works on WinUSB. On a cold device these bits read SET (0x7003f0) — clearing them is NOT
# a no-op (verified live: read 0xfffeffff -> wrote 0xff8efc0f, stuck).
MT_SSUSB_EPCTL_CSR_EP_RST_OPT = 0x74011890
MT_EPCTL_EP_RST_OPT_MASK      = (0x3F << 4) | (0x7 << 20)   # GENMASK(9,4) | GENMASK(22,20)

# MT_SWDEF_MODE = MT_SWDEF(0x3c) = 0x41f200 + 0x3c. The kernel writes NORMAL_MODE (0)
# after dma_init, before firmware download (mt7921/usb.c:118, init.c:187) — putting the
# chip in normal (non-debug) mode for boot. Reachable over the unified bus (WinUSB-OK).
MT_SWDEF_MODE        = 0x0041f23c
MT_SWDEF_NORMAL_MODE = 0

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

# ===========================================================================
# Post-boot device init. All reachable over the unified bus (0x5F/0xDF) like
# dma_init — verified from the capture. Addresses/masks grepped verbatim from
# driver_sources/mt76-source-v6.18/{mt7921/regs.h, mt792x_regs.h}.
# ===========================================================================

# EFUSE buffer mode (mt76_connac_mcu.h EE_MODE_* / EE_FORMAT_*).
EE_MODE_EFUSE   = 0
EE_FORMAT_WHOLE = 1

# --- mt7921_mac_init: MDP de-agg + rx-hdr-trans (mt7921/regs.h) ---
MT_MDP_BASE = 0x820cd000
def MT_MDP(ofs): return MT_MDP_BASE + ofs
MT_MDP_DCR0 = MT_MDP(0x000)
MT_MDP_DCR0_DAMSDU_EN       = 1 << 15   # BIT(15)
MT_MDP_DCR0_RX_HDR_TRANS_EN = 1 << 19   # BIT(19)
MT_MDP_DCR1 = MT_MDP(0x004)
MT_MDP_DCR1_MAX_RX_LEN = 0x0000FFF8     # GENMASK(15, 3)

# --- mt7921_mac_wtbl_update: per-WCID admission-count clear (mt7921/regs.h) ---
MT_WTBLON_TOP_BASE = 0x820d4000
def MT_WTBLON_TOP(ofs): return MT_WTBLON_TOP_BASE + ofs
MT_WTBL_UPDATE = MT_WTBLON_TOP(0x230)
MT_WTBL_UPDATE_WLAN_IDX        = 0x000003FF   # GENMASK(9, 0)
MT_WTBL_UPDATE_ADM_COUNT_CLEAR = 1 << 12      # BIT(12)
MT_WTBL_UPDATE_BUSY            = 1 << 31       # BIT(31)
MT792x_WTBL_SIZE = 20
MT792x_WTBL_RESERVED = MT792x_WTBL_SIZE - 1    # reserved wcid for the first vif

# --- mt792x_mac_init_band: per-band MAC/MIB/DMA setup (mt792x_regs.h) ---
def MT_WF_TMAC_BASE(b): return 0x820f4000 if b else 0x820e4000
def MT_TMAC_CTCR0(b): return MT_WF_TMAC_BASE(b) + 0x0F4
MT_TMAC_CTCR0_INS_DDLMT_REFTIME      = 0x0000003F   # GENMASK(5, 0)
MT_TMAC_CTCR0_INS_DDLMT_EN           = 1 << 17      # BIT(17)
MT_TMAC_CTCR0_INS_DDLMT_VHT_SMPDU_EN = 1 << 18      # BIT(18)

def MT_WF_RMAC_BASE(b): return 0x820f5000 if b else 0x820e5000
def MT_WF_RMAC_MIB_TIME0(b): return MT_WF_RMAC_BASE(b) + 0x3C4
def MT_WF_RMAC_MIB_AIRTIME0(b): return MT_WF_RMAC_BASE(b) + 0x380
MT_WF_RMAC_MIB_RXTIME_EN = 1 << 30    # BIT(30)

def MT_WF_MIB_BASE(b): return 0x820fd000 if b else 0x820ed000
def MT_MIB_SCR1(b): return MT_WF_MIB_BASE(b) + 0x004
MT_MIB_TXDUR_EN = 1 << 8   # BIT(8)
MT_MIB_RXDUR_EN = 1 << 9   # BIT(9)

def MT_WF_DMA_BASE(b): return 0x820f7000 if b else 0x820e7000
def MT_DMA_DCR0(b): return MT_WF_DMA_BASE(b) + 0x000
MT_DMA_DCR0_MAX_RX_LEN = 0x0000FFF8   # GENMASK(15, 3)
MT_DMA_DCR0_RXD_G5_EN  = 1 << 23      # BIT(23)

def MT_WTBLOFF_TOP_BASE(b): return 0x820f9000 if b else 0x820e9000
def MT_WTBLOFF_TOP_RSCR(b): return MT_WTBLOFF_TOP_BASE(b) + 0x008
MT_WTBLOFF_TOP_RSCR_RCPI_MODE  = 0xC0000000   # GENMASK(31, 30)
MT_WTBLOFF_TOP_RSCR_RCPI_PARAM = 0x03000000   # GENMASK(25, 24)

# mt76_connac_mcu_set_rts_thresh value used by mt7921_mac_init.
MT_RTS_THRESH_DEFAULT = 0x92B

# --- mt792x_mac_reset_counters: TX-agg + MIB airtime counters (mt792x_regs.h) ---
def MT_TX_AGG_CNT(b, n):  return MT_WF_MIB_BASE(b) + 0x7DC + (n << 2)
def MT_TX_AGG_CNT2(b, n): return MT_WF_MIB_BASE(b) + 0x7EC + (n << 2)
def MT_MIB_SDR9(b):       return MT_WF_MIB_BASE(b) + 0x02C
def MT_MIB_SDR36(b):      return MT_WF_MIB_BASE(b) + 0x054
def MT_MIB_SDR37(b):      return MT_WF_MIB_BASE(b) + 0x058
MT_WF_RMAC_MIB_RXTIME_CLR = 1 << 31   # BIT(31)

# --- mt792x_mac_work survey + MIB stats register reads (mt792x_regs.h) ---
# mt792x_phy_update_channel reads the channel-busy / airtime counters; the rest
# are the per-MIB counters mt792x_mac_update_mib_stats accumulates. All over the
# unified bus; addresses grepped verbatim from mt792x_regs.h.
def MT_WF_RMAC_MIB_AIRTIME14(b): return MT_WF_RMAC_BASE(b) + 0x3B8
def MT_MIB_SDR3(b):       return MT_WF_MIB_BASE(b) + 0x698
def MT_MIB_SDR5(b):       return MT_WF_MIB_BASE(b) + 0x780
def MT_MIB_SDR12(b):      return MT_WF_MIB_BASE(b) + 0x558
def MT_MIB_SDR14(b):      return MT_WF_MIB_BASE(b) + 0x564
def MT_MIB_SDR15(b):      return MT_WF_MIB_BASE(b) + 0x568
def MT_MIB_SDR22(b):      return MT_WF_MIB_BASE(b) + 0x770
def MT_MIB_SDR23(b):      return MT_WF_MIB_BASE(b) + 0x774
def MT_MIB_SDR31(b):      return MT_WF_MIB_BASE(b) + 0x55C
def MT_MIB_SDR32(b):      return MT_WF_MIB_BASE(b) + 0x7A8
def MT_MIB_MB_BSDR0(b):   return MT_WF_MIB_BASE(b) + 0x688
def MT_MIB_MB_BSDR1(b):   return MT_WF_MIB_BASE(b) + 0x690
def MT_MIB_MB_BSDR2(b):   return MT_WF_MIB_BASE(b) + 0x518
def MT_MIB_MB_BSDR3(b):   return MT_WF_MIB_BASE(b) + 0x520
def MT_WF_ETBF_BASE(b):   return 0x820fa000 if b else 0x820ea000
def MT_ETBF_TX_APP_CNT(b): return MT_WF_ETBF_BASE(b) + 0x150
def MT_ETBF_RX_FB_CNT(b):  return MT_WF_ETBF_BASE(b) + 0x158
MT_PLE_BASE = 0x820c0000
def MT_PLE_AMSDU_PACK_MSDU_CNT(n): return MT_PLE_BASE + 0x10E0 + (n << 2)
MT792x_MIB_TX_AMSDU_LEN = 8     # ARRAY_SIZE(mt76_mib_stats.tx_amsdu)
