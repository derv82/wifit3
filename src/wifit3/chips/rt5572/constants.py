# =============================================================================
# Ralink rt5572 (rt2800usb) Protocol Constants
# =============================================================================

# --- USB Endpoints (Physical rt5572) ---
USB_EP_BULK_OUT            = 0x01
USB_EP_BULK_IN             = 0x81
USB_EP_INT_IN              = 0x82

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

# --- Register Offsets ---
MAC_CSR0                   = 0x1000
MAC_SYS_CTRL               = 0x1004
PBF_SYS_CTRL               = 0x1008
ASIC_VER_ID                = 0x1010
MAC_ADDR_DW0               = 0x1018
MAC_ADDR_DW1               = 0x101c

# MCU related
MCU_INT_SOURCE             = 0x0580
GPIO_CTRL                  = 0x0228
HSC_CTRL                   = 0x0208

# Memory regions
MCU_CODE_BASE              = 0x3000

# --- Register Bits ---
# To be expanded
