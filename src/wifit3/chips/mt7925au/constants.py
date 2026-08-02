# MT7925AU (MediaTek MT7925U, Wi-Fi 7 / connac3) constants.
#
# Every value is grepped verbatim from the kernel source at
# driver_sources/mt76-source-v6.19.14/ (Linux stable v6.19.14, commit b9dbb45). The
# mt792x/connac register layer is shared with mt7921 (mt792x_regs.h, mt792x_usb.c);
# the chip-id, firmware container and connac3 MCU/descriptor formats are
# mt7925-specific. file:line citations point into that tree.

# ===========================================================================
# USB transport
# ===========================================================================

# Vendor request opcodes (mt76.h:615-625 enum mt76_vendor_req).
MT_VEND_DEV_MODE  = 0x01   # UHW-bus read bRequest
MT_VEND_WRITE     = 0x02   # UHW-bus write bRequest
MT_VEND_POWER_ON  = 0x04   # mt792xu_mcu_power_on
MT_VEND_READ_EXT  = 0x63   # unified-bus register read
MT_VEND_WRITE_EXT = 0x66   # unified-bus register write

# bmRequestType = USB_DIR | recipient nibble (mt792x.h:479-480):
#   MT_USB_TYPE_VENDOR     = 0x40 | 0x1f = 0x5f
#   MT_USB_TYPE_UHW_VENDOR = 0x40 | 0x1e = 0x5e
# USB_DIR_IN = 0x80, USB_DIR_OUT = 0x00.
MT_REQ_IN_VENDOR      = 0xDF   # unified read
MT_REQ_OUT_VENDOR     = 0x5F   # unified write, power-on
MT_REQ_IN_UHW_VENDOR  = 0xDE   # UHW read
MT_REQ_OUT_UHW_VENDOR = 0x5E   # UHW write

# USB endpoints (mt76.h:630-641; physical bEndpointAddress from the vendor
# interface descriptor, confirmed against the mt7925u capture: bulk-OUT 0x04/0x08).
EP_OUT_MCU  = 0x08   # MT_EP_OUT_INBAND_CMD: MCU commands (non-FW_SCATTER)
EP_OUT_FW   = 0x04   # MT_EP_OUT_AC_BE: FW_SCATTER chunks + 802.11 data TX
EP_OUT_DATA = 0x04   # alias — same physical EP as EP_OUT_FW
EP_OUT_HCCA = 0x09   # MT_EP_OUT_HCCA: mgmt/PSD TX (deauth)
EP_IN_BULK  = 0x84   # MT_EP_IN_PKT_RX: RX 802.11 frames (+ MCU resp once RXEVT_EP4_EN set)
EP_IN_MCU   = 0x85   # MT_EP_IN_CMD_RESP: MCU responses (RXEVT_EP4_EN cleared)

# ===========================================================================
# Chip identity + power-on (mt792x_regs.h)
# ===========================================================================
MT_HW_CHIPID = 0x70010200   # :394 — lower 16 bits = chip id (0x7925)
MT_HW_REV    = 0x70010204   # :395 — ASIC revision; probe reads it after the id
MT_CHIP_ID_EXPECTED = 0x7925

# MT_UMAC(ofs) = 0x74000000 + ofs (:431)
MT_UDMA_TX_QSEL = 0x74000008   # MT_UMAC(0x008), :432
MT_FW_DL_EN     = 0x08         # BIT(3), :433

# MT_SWDEF(ofs) = MT_SWDEF_BASE + ofs (:367); MT_SWDEF_BASE = 0x41f200.
MT_SWDEF_MODE        = 0x0041f23c   # MT_SWDEF(0x3c), :368
MT_SWDEF_NORMAL_MODE = 0            # :369

MT_CONN_ON_MISC        = 0x7c0600f0   # :474
MT_TOP_MISC2_FW_PWR_ON = 0x1          # BIT(0), :475
MT_TOP_MISC2_FW_N9_RDY = 0x3          # GENMASK(1,0), :477

# ===========================================================================
# mt792xu_dma_init (mt792x_usb.c) — pre-firmware WFDMA bring-up.
# ===========================================================================

# mt792xu_dma_prefetch — TX-ring prefetch. MT_UWFDMA0_TX_RING_EXT_CTRL(n) =
# 0x7c024600 + (n<<2) (:464). value = FIELD_PREP(BASE_PTR, base) | FIELD_PREP(MAX_CNT, 4).
def MT_UWFDMA0_TX_RING_EXT_CTRL(n: int) -> int: return 0x7c024600 + (n << 2)
MT_WPDMA0_MAX_CNT_MASK  = 0x000000FF   # GENMASK(7, 0),  :345
MT_WPDMA0_BASE_PTR_MASK = 0xFFFF0000   # GENMASK(31, 16), :346
# (ring_idx, max_cnt, base_ptr), mt792x_usb.c:130-136.
MT_DMA_PREFETCH_CONF = [(0, 4, 0x080), (1, 4, 0x0c0), (2, 4, 0x100), (3, 4, 0x140),
                        (4, 4, 0x180), (16, 4, 0x280), (17, 4, 0x2c0)]

# MT_UWFDMA0_GLO_CFG = MT_UWFDMA0(0x208) = 0x7c024208 (:461).
MT_UWFDMA0_GLO_CFG                       = 0x7c024208
MT_WFDMA0_GLO_CFG_TX_DMA_EN              = 0x00000001   # BIT(0),  :291
MT_WFDMA0_GLO_CFG_RX_DMA_EN              = 0x00000004   # BIT(2),  :293
MT_WFDMA0_GLO_CFG_RX_DMA_BUSY            = 0x00000008   # BIT(3),  :294
MT_WFDMA0_GLO_CFG_FW_DWLD_BYPASS_DMASHDL = 0x00000200   # BIT(9),  :297
MT_WFDMA0_GLO_CFG_OMIT_RX_INFO_PFET2     = 0x00200000   # BIT(21), :302
MT_WFDMA0_GLO_CFG_OMIT_RX_INFO           = 0x08000000   # BIT(27), :303
MT_WFDMA0_GLO_CFG_OMIT_TX_INFO           = 0x10000000   # BIT(28), :304

# MT_WFDMA_HOST_CONFIG (:428) — route USB RX events (MCU resp + FW-up signal) to EP 0x84.
MT_WFDMA_HOST_CONFIG                  = 0x7c027030
MT_WFDMA_HOST_CONFIG_USB_RXEVT_EP4_EN = 0x00000040   # BIT(6), :429

# DMA scheduler (MT_DMA_SHDL(ofs) = 0x7c026000 + ofs; :406).
def MT_DMASHDL_GROUP_QUOTA(n: int) -> int: return 0x7c026020 + (n << 2)   # :418
def MT_DMASHDL_Q_MAP(n: int)       -> int: return 0x7c026060 + (n << 2)   # :422
def MT_DMASHDL_SCHED_SET(n: int)   -> int: return 0x7c026070 + (n << 2)   # :426
MT_DMASHDL_PAGE             = 0x7c02600c   # :410
MT_DMASHDL_GROUP_SEQ_ORDER = 0x00010000   # BIT(16), :411
MT_DMASHDL_REFILL          = 0x7c026010   # :412
MT_DMASHDL_REFILL_MASK     = 0xFFFF0000   # GENMASK(31,16), :413
MT_DMASHDL_PKT_MAX_SIZE     = 0x7c02601c   # :414
MT_DMASHDL_PKT_MAX_SIZE_PLE = 0x00000FFF   # GENMASK(11,0),  :415
MT_DMASHDL_PKT_MAX_SIZE_PSE = 0x0FFF0000   # GENMASK(27,16), :416
# GROUP_QUOTA(0..4) = FIELD_PREP(MAX=GENMASK(27,16),0xfff) | FIELD_PREP(MIN=GENMASK(11,0),0x3).
MT_DMASHDL_GROUP_QUOTA_VAL = 0x0FFF0003
MT_DMASHDL_Q_MAP_VALS    = [0x32013201, 0x32013201, 0x55555444, 0x55555444]   # :167-170
MT_DMASHDL_SCHED_SET_VALS = [0x76540132, 0xFEDCBA98]                          # :172-173

# MT_WFDMA_DUMMY_CR = MT_MCU_WPDMA0(0x120) = 0x54000120 (:386); NEED_REINIT BIT(1) (:387).
MT_WFDMA_DUMMY_CR    = 0x54000120
MT_WFDMA_NEED_REINIT = 0x00000002

# MT_UDMA_WLCFG_0 = MT_UMAC(0x18) = 0x74000018 (:439); _1 = MT_UMAC(0x0c) = 0x7400000c (:435).
MT_UDMA_WLCFG_0      = 0x74000018
MT_UDMA_WLCFG_1      = 0x7400000c
MT_WL_RX_AGG_PKT_LMT = 0x000000FF   # GENMASK(7,0),  :436
MT_WL_RX_AGG_TO      = 0x000000FF   # GENMASK(7,0),  :440
MT_WL_RX_AGG_LMT     = 0x0000FF00   # GENMASK(15,8), :441
MT_WL_RX_MPSZ_PAD0   = 0x00040000   # BIT(18), :444
MT_WL_RX_FLUSH       = 0x00080000   # BIT(19), :445
MT_TICK_1US_EN       = 0x00100000   # BIT(20), :446
MT_WL_RX_EN          = 0x00400000   # BIT(22), :448
MT_WL_TX_EN          = 0x00800000   # BIT(23), :449

# MT_SSUSB_EPCTL_CSR_EP_RST_OPT = MT_SSUSB_EPCTL_CSR(0x090) = 0x74011890 (:457-458).
# mt792xu_epctl_rst_opt(false) clears GENMASK(9,4) | GENMASK(22,20).
MT_SSUSB_EPCTL_CSR_EP_RST_OPT = 0x74011890
MT_EPCTL_EP_RST_OPT_MASK      = (0x3F << 4) | (0x7 << 20)   # 0x007003f0

# ===========================================================================
# Firmware download (mt7925_run_firmware -> mt792x_load_firmware ->
# mt76_connac2_load_patch / mt76_connac2_load_ram). Container = connac2 format.
# ===========================================================================
FIRMWARE_ROM_PATCH = "WIFI_MT7925_PATCH_MCU_1_1_hdr.bin"   # MT7925_ROM_PATCH, mt792x.h:52
FIRMWARE_WM        = "WIFI_RAM_CODE_MT7925_1_1.bin"        # MT7925_FIRMWARE_WM, mt792x.h:47

# Wire-format sizes.
SDIO_HDR_SIZE = 4     # mt792x_skb_add_usb_sdio_hdr (mt792x.h:492)
MCU_TXD_SIZE  = 64    # sizeof(struct mt76_connac2_mcu_txd)
MAX_FW_CHUNK  = 4096  # __mt76_mcu_send_firmware max_len (USB, non-SDIO)

# mt76_connac2 firmware container struct sizes (mt76_connac_mcu.h:139-194).
PATCH_HDR_SIZE  = 96   # mt76_connac2_patch_hdr  (BE)
PATCH_SEC_SIZE  = 64   # mt76_connac2_patch_sec  (BE)
FW_TRAILER_SIZE = 36   # mt76_connac2_fw_trailer (LE)
FW_REGION_SIZE  = 40   # mt76_connac2_fw_region  (LE)
PATCH_SEC_TYPE_MASK = 0x0000FFFF   # GENMASK(15,0), :28
PATCH_SEC_TYPE_INFO = 0x2          # :29

# MCU download command ids (mt76_connac_mcu.h:1316-1325 enum).
MCU_CMD_TARGET_ADDRESS_LEN_REQ = 0x01
MCU_CMD_FW_START_REQ           = 0x02
MCU_CMD_PATCH_START_REQ        = 0x05
MCU_CMD_PATCH_FINISH_REQ       = 0x07
MCU_CMD_PATCH_SEM_CONTROL      = 0x10
MCU_CMD_FW_SCATTER             = 0xee

# mt76_connac_mcu_init_download addr branch (mt76_connac_mcu.c:67-73): PATCH_START_REQ
# iff addr is one of these, else TARGET_ADDRESS_LEN_REQ. mt7925 adds 0xe0002800.
PATCH_START_ADDRS = (0x900000, 0xe0002800)

# PATCH_SEM_CONTROL op + response codes (mt76_connac_mcu.h:1090-1095, 1357-1359).
PATCH_SEM_RELEASE = 0x00
PATCH_SEM_GET     = 0x01
PATCH_IS_DL              = 0x01
PATCH_NOT_DL_SEM_SUCCESS = 0x02
PATCH_REL_SEM_SUCCESS    = 0x03

# DL_MODE / feature-set / patch-section encryption flags (mt76_connac_mcu.h:9-36).
DL_MODE_ENCRYPT          = 0x00000001   # BIT(0)
DL_MODE_KEY_IDX          = 0x00000006   # GENMASK(2, 1)
DL_MODE_RESET_SEC_IV     = 0x00000008   # BIT(3)
DL_CONFIG_ENCRY_MODE_SEL = 0x00000040   # BIT(6)
DL_MODE_NEED_RSP         = 0x80000000   # BIT(31)
FW_FEATURE_SET_ENCRYPT   = 0x01         # BIT(0)
FW_FEATURE_SET_KEY_IDX   = 0x06         # GENMASK(2, 1)
FW_FEATURE_ENCRY_MODE    = 0x10         # BIT(4)
FW_FEATURE_OVERRIDE_ADDR = 0x20         # BIT(5)
FW_FEATURE_NON_DL        = 0x40         # BIT(6)
FW_START_OVERRIDE        = 0x01         # BIT(0)
# mt76_connac2_patch_sec info encryption (mt76_connac_mcu.h:27-36).
PATCH_SEC_NOT_SUPPORT       = 0xFFFFFFFF   # GENMASK(31, 0)
PATCH_SEC_ENC_TYPE_MASK     = 0xFF000000   # GENMASK(31, 24)
PATCH_SEC_ENC_TYPE_PLAIN    = 0x00
PATCH_SEC_ENC_TYPE_AES      = 0x01
PATCH_SEC_ENC_TYPE_SCRAMBLE = 0x02
PATCH_SEC_ENC_AES_KEY_MASK  = 0xFF         # GENMASK(7, 0)

# ===========================================================================
# connac3 MCU txd (mt7925_mcu_fill_message) + mt7925_mcu_rxd response header.
# ===========================================================================
# txd[0] = Q_IDX(0x20)<<25 | PKT_FMT(MT_TX_TYPE_CMD=2)<<23 | TX_BYTES(len).
TXD0_BASE = (0x20 << 25) | (0x02 << 23)   # 0x41000000  (mt76_connac3_mac.h:177,216)
# txd[1] = FIELD_PREP(MT_TXD1_HDR_FORMAT=GENMASK(15,14), MT_HDR_FORMAT_CMD=1) = 0x00004000.
# DIFFERS FROM connac2 (mt7921), which uses LONG_FORMAT|HDR_FORMAT<<16 = 0x80010000.
TXD1_CMD = 0x00004000                     # mt7925/mcu.c:3487, mt76_connac3_mac.h:227
# mcu_txd fields (mt76_connac2_mcu_txd, offsets within the 64B txd).
MCU_PKT_ID  = 0xA0     # pkt_type
MCU_PQ_ID   = 0x8000   # MCU_PQ_ID(MT_TX_PORT_IDX_MCU=1, MT_TX_MCU_PORT_RX_Q0=0x20)
MCU_Q_NA    = 3        # set_query for a plain (no ext/CE) command
MCU_S2D_H2N = 0        # s2d_index host-to-N9

# mt7925_mcu_rxd (mt7925/mcu.h:26-42): rxd[8] (bytes 0-31), len@32, pkt_type_id@34,
# then eid@36, seq@37. Header is 44 bytes (connac2 was 28B header, seq@29).
MT7925_RXD_HDR_SIZE = 44
MT7925_RXD_EID_OFF  = 36
MT7925_RXD_SEQ_OFF  = 37
# PATCH_SEM_CONTROL / PATCH_FINISH_REQ pull sizeof(rxd)-4, so the status byte is at 40.
MT7925_RXD_STATUS_OFF = 40

# RX descriptor (connac3, mt76_connac3_mac.h). rxd0 PKT_TYPE GENMASK(31,27), LENGTH GENMASK(15,0).
MT_RXD0_LENGTH    = 0x0000FFFF   # GENMASK(15, 0) — total RX bytes (RXD + MPDU, FCS stripped)
PKT_TYPE_RX_EVENT = 7            # MCU response / firmware event
PKT_TYPE_NORMAL   = 2            # 802.11 frame
# rxd1 group-present bits (each adds descriptor words before the MPDU), :38-42.
MT_RXD1_NORMAL_GROUP_1 = 1 << 16
MT_RXD1_NORMAL_GROUP_2 = 1 << 17
MT_RXD1_NORMAL_GROUP_3 = 1 << 18
MT_RXD1_NORMAL_GROUP_4 = 1 << 19
MT_RXD1_NORMAL_GROUP_5 = 1 << 20
# rxd2 HDR_OFFSET GENMASK(15,13): remove_pad = 2*this bytes of header padding, :57.
MT_RXD2_NORMAL_HDR_OFFSET_SHIFT = 13
MT_RXD2_NORMAL_HDR_OFFSET_MASK  = 0x7
MT_RXD3_NORMAL_FCS_ERR = 1 << 24   # BIT(24), :80
MT_PRXV_RCPI0 = 0xFF               # GENMASK(7,0) in rxv[3], connac3_mac.h:112 (chain-0 RCPI)
MT_PRXV_RCPI1 = 0xFF00             # GENMASK(15,8),  :111 (chain-1 RCPI)
MT_PRXV_RCPI2 = 0xFF0000           # GENMASK(23,16), :110 (chain-2 RCPI)
MT_PRXV_RCPI3 = 0xFF000000         # GENMASK(31,24), :109 (chain-3 RCPI)

# ===========================================================================
# TX descriptor (connac3 TXWI, mt7925_mac_write_txwi). USB layout per frame:
# [4B SDIO hdr][64B TXD][802.11 MPDU][pad]. The TXD is MT_SDIO_TXD_SIZE and the
# writer fills txwi[0..7] (32 B); the trailing 32 B stay zero. Masks verbatim
# from mt76_connac3_mac.h; sizes from mt76_connac.h.
# ===========================================================================
MT_TXD_SIZE      = 8 * 4              # mt76_connac.h:34 — base TXD (8 dwords)
MT_SDIO_TXD_SIZE = MT_TXD_SIZE + 8 * 4  # mt76_connac.h:40 — USB/SDIO TXD (64 B)

# mt792x_skb_add_usb_sdio_hdr (mt792x.h:499): tx_bytes = skb->len (USB), pkt_type.
MT792x_SDIO_HDR_TX_BYTES = 0x0000FFFF   # GENMASK(15,0), mt792x.h:54

# txwi[0] (mt76_connac3_mac.h:216-219).
MT_TXD0_Q_IDX    = 0xFE000000   # GENMASK(31,25)
MT_TXD0_PKT_FMT  = 0x01800000   # GENMASK(24,23)
MT_TXD0_TX_BYTES = 0x0000FFFF   # GENMASK(15,0)
# tx_pkt_type (mt76_connac3_mac.h:174-178) + lmac queue idx (:11-18).
MT_TX_TYPE_SF  = 1              # USB/SDIO short-format (mmio would be CT)
MT_LMAC_ALTX0  = 0x10          # alt-TX queue for mgmt/PSD

# txwi[1] (mt76_connac3_mac.h:221-229).
MT_TXD1_FIXED_RATE = 1 << 31   # BIT(31)
MT_TXD1_OWN_MAC    = 0x7E000000  # GENMASK(30,25)
MT_TXD1_TID        = 0x01E00000  # GENMASK(24,21)
MT_TXD1_HDR_INFO   = 0x001F0000  # GENMASK(20,16)
MT_TXD1_HDR_FORMAT = 0x0000C000  # GENMASK(15,14)
MT_TXD1_TGID       = 0x00003000  # GENMASK(13,12)
MT_TXD1_WLAN_IDX   = 0x00000FFF  # GENMASK(11,0)
# hdr_format enum (mt76_connac3_mac.h:167-171).
MT_HDR_FORMAT_802_3  = 0
MT_HDR_FORMAT_802_11 = 2

# txwi[2] frame type/subtype (mt76_connac3_mac.h:240-241).
MT_TXD2_FRAME_TYPE = 0x00000030  # GENMASK(5,4)
MT_TXD2_SUB_TYPE   = 0x0000000F  # GENMASK(3,0)

# txwi[3] (mt76_connac3_mac.h:243-255).
MT_TXD3_SN_VALID      = 1 << 31  # BIT(31)
MT_TXD3_BA_DISABLE    = 1 << 28  # BIT(28)
MT_TXD3_SEQ           = 0x0FFF0000  # GENMASK(27,16)
MT_TXD3_REM_TX_COUNT  = 0x0000F800  # GENMASK(15,11)
MT_TXD3_HW_AMSDU      = 1 << 5   # BIT(5)
MT_TXD3_BCM           = 1 << 4   # BIT(4)
MT_TXD3_PROTECT_FRAME = 1 << 1   # BIT(1)
MT_TXD3_NO_ACK        = 1 << 0   # BIT(0)
TXD3_REM_TX_COUNT_UNLTD = 15     # write_txwi seeds REM_TX_COUNT with 15

# txwi[5] (mt76_connac3_mac.h:267).
MT_TXD5_PID = 0x000000FF   # GENMASK(7,0)

# txwi[6] (mt76_connac3_mac.h:273-280).
MT_TXD6_TX_RATE  = 0x003F0000  # GENMASK(21,16)
MT_TXD6_MSDU_CNT = 0x000003F0  # GENMASK(9,4)
MT_TXD6_DIS_MAT  = 1 << 3      # BIT(3)
MT_TXD6_DAS      = 1 << 2      # BIT(2)

# tx_mgnt_type (mt76_connac3_mac.h:194-198): mgmt frames carry MT_TX_NORMAL.
MT_TX_NORMAL = 0

# Monitor-vif TX context (mt7925/main.c:375-392, mt792x_mac.c). The monitor link is
# vif idx 0: omac_idx 0, band_idx 0xff (masked to TGID=3), wcid = MT792x_WTBL_RESERVED
# (19). basic_rates_idx = MT792x_BASIC_RATES_TBL + 4 because the phy's default chandef
# lands on 5 GHz (the last band with world-enabled channels; mac80211.c:395-418), so
# main.c:382 takes the non-2.4GHz branch. Constant for the monitor vif's lifetime.
MON_TX_OMAC_IDX  = 0
MON_TX_BAND_IDX  = 0xff
MON_TX_RATE_IDX  = 11 + 4   # MT792x_BASIC_RATES_TBL(mt792x.h:36) + 4 = 15

# ===========================================================================
# Post-boot init (mt7925_mac_init, mt792x_mac_init_band). Addresses/bits verbatim
# from mt7925/regs.h + mt792x_regs.h. mt7925 MDP/WTBL bases DIFFER from mt7921.
# ===========================================================================

# mt7925_mac_init (mt7925/init.c:82-84): MDP de-agg + max RX len.
MT_MDP_DCR1            = 0x820cc804   # MT_MDP(0x004), mt7925/regs.h:16
MT_MDP_DCR1_MAX_RX_LEN = 0x0000FFF8  # GENMASK(15,3), :17
MDP_MAX_RX_LEN        = 1536
MT_MDP_DCR0           = 0x820cc800    # MT_MDP(0x000), :12
MT_MDP_DCR0_DAMSDU_EN = 0x00008000   # BIT(15), :13

# per-WCID admission-count clear (mt7925_mac_wtbl_update, mt7925/mac.c:13).
MT_WTBL_UPDATE                 = 0x820d4380   # MT_WTBLON_TOP(0x380), mt7925/regs.h:88
MT_WTBL_UPDATE_WLAN_IDX        = 0x00000FFF   # GENMASK(11,0), :89
MT_WTBL_UPDATE_ADM_COUNT_CLEAR = 0x00004000   # BIT(14), :90
MT_WTBL_UPDATE_BUSY            = 0x80000000   # BIT(31), mt792x_regs.h:160
MT792x_WTBL_SIZE     = 20   # mt792x.h:18
MT792x_WTBL_RESERVED = 19   # MT792x_WTBL_SIZE - 1, mt792x.h:19

# per-band bases (mt792x_regs.h): band0 unless b.
def MT_WF_TMAC_BASE(b):     return 0x820f4000 if b else 0x820e4000
def MT_WF_RMAC_BASE(b):     return 0x820f5000 if b else 0x820e5000
def MT_WF_MIB_BASE(b):      return 0x820fd000 if b else 0x820ed000
def MT_WF_DMA_BASE(b):      return 0x820f7000 if b else 0x820e7000
def MT_WTBLOFF_TOP_BASE(b): return 0x820f9000 if b else 0x820e9000

# mt792x_mac_init_band (mt792x_mac.c:285-311).
def MT_TMAC_CTCR0(b):        return MT_WF_TMAC_BASE(b) + 0x0f4   # :46
MT_TMAC_CTCR0_INS_DDLMT_REFTIME      = 0x0000003F   # GENMASK(5,0), :47
MT_TMAC_CTCR0_INS_DDLMT_EN           = 0x00020000   # BIT(17), :48
MT_TMAC_CTCR0_INS_DDLMT_VHT_SMPDU_EN = 0x00040000   # BIT(18), :49
TMAC_CTCR0_REFTIME_VAL = 0x3f
def MT_WF_RMAC_MIB_TIME0(b):    return MT_WF_RMAC_BASE(b) + 0x3c4   # :251
def MT_WF_RMAC_MIB_AIRTIME0(b): return MT_WF_RMAC_BASE(b) + 0x380   # :257
MT_WF_RMAC_MIB_RXTIME_EN = 0x40000000   # BIT(30), :253
def MT_MIB_SCR1(b):         return MT_WF_MIB_BASE(b) + 0x004   # :98
MT_MIB_TXDUR_EN = 0x00000100   # BIT(8), :99
MT_MIB_RXDUR_EN = 0x00000200   # BIT(9), :100
def MT_DMA_DCR0(b):         return MT_WF_DMA_BASE(b) + 0x000   # :57
MT_DMA_DCR0_MAX_RX_LEN = 0x0000FFF8   # GENMASK(15,3), :58
DMA_DCR0_MAX_RX_LEN_VAL = 1536
MT_DMA_DCR0_RXD_G5_EN  = 0x00800000   # BIT(23), :59
def MT_WTBLOFF_TOP_RSCR(b): return MT_WTBLOFF_TOP_BASE(b) + 0x008   # :65
MT_WTBLOFF_TOP_RSCR_RCPI_MODE  = 0xC0000000   # GENMASK(31,30), :66
MT_WTBLOFF_TOP_RSCR_RCPI_PARAM = 0x03000000   # GENMASK(25,24), :67
RSCR_RCPI_PARAM_VAL = 0x3

# basic-rate fixed-rate table (mt7925_mac_set_fixed_rate_table, mac.c:157).
MT_WTBL_ITCR      = 0x820d43b0   # MT_WTBLON_TOP(0x3b0), mt792x_regs.h:162
MT_WTBL_ITCR_WR   = 0x00010000   # BIT(16), :163
MT_WTBL_ITCR_EXEC = 0x80000000   # BIT(31), :164
MT_WTBL_ITDR0     = 0x820d43b8   # :165
MT_WTBL_ITDR1     = 0x820d43bc   # :166
MT_WTBL_SPE_IDX_SEL = 0x00000040 # BIT(6), :167
MT792x_BASIC_RATES_TBL = 11      # mt792x.h:36
# rate_idx per mt76_rates entry: FIELD_PREP(MODE, hw>>8) | FIELD_PREP(IDX, hw&0xff),
# MODE=GENMASK(9,6), IDX=GENMASK(5,0) (mt76_connac3_mac.h). 4 CCK + 8 OFDM.
BASIC_RATE_IDX = [0x000, 0x001, 0x002, 0x003,
                  0x04b, 0x04f, 0x04a, 0x04e, 0x049, 0x04d, 0x048, 0x04c]

# ===========================================================================
# connac3 UNI / CE command ids + tags (mt76_connac_mcu.h, mt7925/mcu.h).
# ===========================================================================
MCU_UNI_CMD_DEV_INFO_UPDATE = 0x01
MCU_UNI_CMD_BSS_INFO_UPDATE = 0x02
MCU_UNI_CMD_HIF_CTRL        = 0x07
MCU_UNI_CMD_BAND_CONFIG     = 0x08
MCU_UNI_CMD_WSYS_CONFIG     = 0x0b
MCU_UNI_CMD_CHIP_CONFIG     = 0x0e
MCU_UNI_CMD_SET_DOMAIN_INFO = 0x15   # cfg80211 regulatory (world-"00" regdom)
MCU_UNI_CMD_SNIFFER         = 0x24
MCU_UNI_CMD_SET_POWER_LIMIT = 0x2c   # cfg80211 regulatory / CLC power tables (waived)
MCU_UNI_CMD_EFUSE_CTRL      = 0x2d
MCU_CE_CMD_SET_CHAN_DOMAIN  = 0x0f

# tags (mt7925/mcu.h; DEV_INFO_ACTIVE/UNI_BSS_INFO_BASIC in mt76_connac_mcu.h).
UNI_CHIP_CONFIG_CHIP_CFG = 0x2   # mt7925/mcu.h:118
UNI_CHIP_CONFIG_NIC_CAPA = 0x3   # :119
UNI_BAND_CONFIG_RTS_THRESHOLD          = 0x08   # :124
UNI_BAND_CONFIG_SET_MAC80211_RX_FILTER = 0x0C   # :125
UNI_WSYS_CONFIG_FW_LOG_CTRL = 0   # :129 (first in enum)
UNI_EFUSE_BUFFER_MODE       = 2   # :135 (UNI_EFUSE_ACCESS=1 first)
UNI_SNIFFER_ENABLE = 0            # :196
UNI_SNIFFER_CONFIG = 1            # :197
UNI_BSS_INFO_BASIC       = 0      # mt76_connac_mcu.h:1363
UNI_BSS_INFO_PM_DISABLE  = 27     # mt76_connac_mcu.h:1384
DEV_INFO_ACTIVE    = 0            # mt76_connac_mcu.h:1009 (first in enum)

# GET_NIC_CAPAB reply TLV tags (mt76_connac_mcu.h enum).
MT_NIC_CAP_MAC_ADDR = 0x07
MT_NIC_CAP_PHY      = 0x08
MT_NIC_CAP_6G       = 0x18

# EFUSE buffer mode (mt76_connac_mcu.h:1145,1151).
EE_MODE_EFUSE   = 0
EE_FORMAT_WHOLE = 1

# monitor BSS conn_type = STA_TYPE_AP | NETWORK_INFRA (mt76_connac_mcu.c:1201).
CONNECTION_INFRA_AP = (1 << 1) | (1 << 16)   # 0x00010002
# conn_type for active-monitor: the omac auto-ACK survives only under a MONITOR BSS.
# INFRA_AP + a peer bssid switches the FW to a peer-STA context that kills it.
CONNECTION_MONITOR = 0

# config_sniffer ch_band code (2.4->1, 5->2, 6->3).
CH_BAND_2GHZ = 1
CH_BAND_5GHZ = 2

MT_RTS_THRESH_DEFAULT = 0x92b   # __mt7925_start

# SET_DOMAIN_INFO per-channel flag bits (enum ieee80211_channel_flags,
# include/net/cfg80211.h). Only the bits the world-"00" regdom sets are named here;
# mt7925_mcu_set_channel_domain serializes chan->flags verbatim. See mcu.py W00_* tables.
CHAN_NO_IR        = 0x00000002   # BIT(1)  no initiating radiation (passive scan only)
CHAN_RADAR        = 0x00000008   # BIT(3)  DFS: radar detection required
CHAN_NO_HT40PLUS  = 0x00000010   # BIT(4)  no HT40+ (no valid channel 20 MHz above)
CHAN_NO_HT40MINUS = 0x00000020   # BIT(5)  no HT40- (no valid channel 20 MHz below)
CHAN_NO_OFDM      = 0x00000040   # BIT(6)  DSSS only (2.4 GHz ch 14)
CHAN_NO_80MHZ     = 0x00000080   # BIT(7)
CHAN_NO_160MHZ    = 0x00000100   # BIT(8)
CHAN_NO_320MHZ    = 0x00080000   # BIT(19) — set on every channel (no 320 MHz in world "00")

# ===========================================================================
# mt792x_mac_work periodic reads (mt792x_mac.c). Band-0 addresses; bases already
# defined above. RXTIME_CLR = BIT(31). Register list verbatim from mt792x_regs.h.
# ===========================================================================
MT_WF_RMAC_MIB_RXTIME_CLR = 0x80000000   # BIT(31), mt792x_regs.h:252
def MT_WF_RMAC_MIB_AIRTIME14(b): return MT_WF_RMAC_BASE(b) + 0x3b8   # :255

# survey burst (mt792x_phy_update_channel): SDR9, SDR36, SDR37, AIRTIME14, set TIME0.
def MT_MIB_SDR9(b):  return MT_WF_MIB_BASE(b) + 0x02c   # :107
def MT_MIB_SDR36(b): return MT_WF_MIB_BASE(b) + 0x054   # :128
def MT_MIB_SDR37(b): return MT_WF_MIB_BASE(b) + 0x058   # :130

# MIB burst (mt792x_mac_update_mib_stats), in read order.
def MT_MIB_SDR3(b):     return MT_WF_MIB_BASE(b) + 0x698   # :102
def MT_MIB_MB_BSDR3(b): return MT_WF_MIB_BASE(b) + 0x520   # :146
def MT_MIB_MB_BSDR2(b): return MT_WF_MIB_BASE(b) + 0x518   # :144
def MT_MIB_MB_BSDR0(b): return MT_WF_MIB_BASE(b) + 0x688   # :140
def MT_MIB_MB_BSDR1(b): return MT_WF_MIB_BASE(b) + 0x690   # :142
def MT_MIB_SDR12(b):    return MT_WF_MIB_BASE(b) + 0x558   # :110
def MT_MIB_SDR14(b):    return MT_WF_MIB_BASE(b) + 0x564   # :111
def MT_MIB_SDR15(b):    return MT_WF_MIB_BASE(b) + 0x568   # :112
def MT_MIB_SDR32(b):    return MT_WF_MIB_BASE(b) + 0x7a8   # :121
def MT_MIB_SDR5(b):     return MT_WF_MIB_BASE(b) + 0x780   # :105
def MT_MIB_SDR22(b):    return MT_WF_MIB_BASE(b) + 0x770   # :117
def MT_MIB_SDR23(b):    return MT_WF_MIB_BASE(b) + 0x774   # :118
def MT_MIB_SDR31(b):    return MT_WF_MIB_BASE(b) + 0x55c   # :119
def MT_WF_ETBF_BASE(b):   return 0x820fa000 if b else 0x820ea000   # :81
def MT_ETBF_TX_APP_CNT(b): return MT_WF_ETBF_BASE(b) + 0x150   # :84
def MT_ETBF_RX_FB_CNT(b):  return MT_WF_ETBF_BASE(b) + 0x158   # :88
MT_PLE_BASE = 0x820c0000   # :17
def MT_PLE_AMSDU_PACK_MSDU_CNT(n): return MT_PLE_BASE + 0x10e0 + (n << 2)   # :26
def MT_TX_AGG_CNT(b, n):  return MT_WF_MIB_BASE(b) + 0x7dc + (n << 2)   # :152
def MT_TX_AGG_CNT2(b, n): return MT_WF_MIB_BASE(b) + 0x7ec + (n << 2)   # :153
MT792x_MIB_TX_AMSDU_LEN = 8   # ARRAY_SIZE(mib->tx_amsdu)
