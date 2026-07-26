# MT7925AU (MediaTek MT7925U, Wi-Fi 7 / connac3) constants.
#
# Every value is grepped verbatim from the kernel source at
# data_dumps/mt76-source-v6.19.14/ (Linux stable v6.19.14, commit b9dbb45). The
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

# RX descriptor (connac3, mt76_connac3_mac). rxd0 PKT_TYPE GENMASK(31,27), LENGTH GENMASK(15,0).
MT_RXD0_LENGTH    = 0x0000FFFF   # GENMASK(15, 0)
PKT_TYPE_RX_EVENT = 7            # MCU response / firmware event
PKT_TYPE_NORMAL   = 2            # 802.11 frame
