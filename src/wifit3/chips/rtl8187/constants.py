# =============================================================================
# Realtek RTL8187L Protocol Constants
# =============================================================================

# --- USB Endpoints (Physical RTL8187L) ---
# Verified for ALFA AWUS036H
USB_EP_BULK_IN             = 0x81
USB_EP_BULK_OUT            = 0x02

# --- Register Offsets (Partial) ---
MAC0                       = 0x0000  # MAC Address bytes 0-3
MAC4                       = 0x0004  # MAC Address bytes 4-5
TCR                        = 0x0040  # Transmit Configuration Register
RCR                        = 0x0044  # Receive Configuration Register
CR                         = 0x0037  # Command Register
MSR                        = 0x0058  # Media Status Register
MSR_NO_LINK                = 0x00
MSR_ADHOC                  = 0x04
MSR_INFRA                  = 0x08
MSR_MASTER                 = 0x0C
MSR_ENEDCA                 = 0x10
EEPROM_CMD                 = 0x0001

# --- RF / Baseband Registers ---
RF_PINS_OUT                = 0x0080
RF_PINS_SELECT             = 0x0084
RF_PINS_STATUS             = 0x0086
BB_HOST_BANG_CLK           = 0x0090
BB_HOST_BANG_EN            = 0x0091
BB_HOST_BANG_DATA          = 0x0092

# --- RCR Bits ---
RCR_AAP                    = 0x00000001  # Accept All Packets (Promiscuous)
RCR_APM                    = 0x00000002  # Accept Physical Match
RCR_AM                     = 0x00000004  # Accept Multicast
RCR_AB                     = 0x00000008  # Accept Broadcast
RCR_AMF                    = 0x00000010  # Accept Management Frames
RCR_ACF                    = 0x00000020  # Accept Control Frames
RCR_AICV                   = 0x00000040  # Accept ICV Error
