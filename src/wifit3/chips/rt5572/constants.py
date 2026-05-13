# =============================================================================
# Ralink rt5572 (rt2800usb) Protocol Constants
# =============================================================================

# --- USB Endpoints (Physical rt5572) ---
USB_EP_BULK_OUT            = 0x01
USB_EP_BULK_IN             = 0x81  # Corrected from 0x82 based on hardware test
USB_EP_INT_IN              = 0x82  # Placeholder, not seen on PAU09

# --- Vendor Requests (bRequest) ---
USB_DEVICE_MODE            = 0x01
USB_SINGLE_WRITE           = 0x02
USB_SINGLE_READ            = 0x03
USB_MULTI_WRITE            = 0x06
USB_MULTI_READ             = 0x07
USB_EEPROM_WRITE           = 0x08
USB_EEPROM_READ            = 0x09
USB_LED_CONTROL            = 0x0a
USB_RX_CONTROL             = 0x0c

# --- MCU Commands ---
MCU_BOOT_SIGNAL            = 0x72
MCU_WAKEUP                 = 0x30
MCU_SLEEP                  = 0x31

# --- Register Offsets ---
# MAC
MAC_CSR0                   = 0x1000
MAC_SYS_CTRL               = 0x1004
MAC_ADDR_DW0               = 0x1008
MAC_ADDR_DW1               = 0x100c
ASIC_VER_ID                = 0x1010

# PBF
PBF_SYS_CTRL               = 0x0400
HOST_CMD_CSR               = 0x0404

# Mailbox / MCU
H2M_MAILBOX_CSR            = 0x7010
H2M_MAILBOX_CID            = 0x7014
H2M_MAILBOX_STATUS         = 0x701c
H2M_INT_SRC                = 0x7024

# Power
AUTOWAKEUP_CFG             = 0x1208
WPDMA_GLO_CFG              = 0x0208
USB_DMA_CFG                = 0x02a0

# GPIO
GPIO_CTRL                  = 0x0228
WPDMA_GLO_CFG              = 0x0208

# Memory regions
MCU_CODE_BASE              = 0x3000

# Register Offsets
RF_CSR_CFG                 = 0x0500
LDO_CFG0                   = 0x05d4
BBP_CSR_CFG                 = 0x101c
RX_FILTER_CFG              = 0x1400

# TX Config
TX_PWR_CFG_0               = 0x1314
TX_PWR_CFG_1               = 0x1318
TX_PWR_CFG_2               = 0x131c
TX_PWR_CFG_3               = 0x1320
TX_PWR_CFG_4               = 0x1324
TX_PIN_CFG                 = 0x1328
TX_BAND_CFG                = 0x132c

# Register Bits
PBF_SYS_CTRL_READY         = (1 << 7)
MAC_SYS_CTRL_ENABLE_RX     = (1 << 3)
MAC_SYS_CTRL_ENABLE_TX     = (1 << 2)
WPDMA_GLO_CFG_ENABLE_TX_DMA = (1 << 0)
WPDMA_GLO_CFG_ENABLE_RX_DMA = (1 << 2)

USB_DMA_CFG_RX_BULK_EN     = (1 << 22)
USB_DMA_CFG_TX_BULK_EN     = (1 << 23)

RF_CSR_CFG_WRITE           = (1 << 16)
RF_CSR_CFG_BUSY            = (1 << 17)
BBP_CSR_CFG_BUSY           = (1 << 17)
BBP_CSR_CFG_READ_CONTROL   = (1 << 16)
BBP_CSR_CFG_RW_MODE        = (1 << 19)

# TX Descriptor bits
TXINFO_W0_WIV              = (1 << 24)
TXINFO_W0_QSEL_EDCA        = (2 << 25)
TXWI_W0_ACK                = (1 << 0)
TXWI_W0_TS                 = (1 << 3)
