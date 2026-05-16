# MT7921AU Constants

# Vendor Requests (Control Transfers)
# Typical Mediatek read/write bmRequestType and bRequest
MT_VEND_REQ_IN  = 0xc0
MT_VEND_REQ_OUT = 0x40 # Or 0x5e etc. (need refinement)

# Firmware filenames
FIRMWARE_WM = "WIFI_MT7961_patch_mcu_1_2_hdr.bin"
FIRMWARE_ROM_PATCH = "WIFI_RAM_CODE_MT7961_1.bin"

# Endpoints
# EP0: Control (Initial config, Vendor Requests)
EP_IN_BULK = 0x81  # Example placeholder
EP_OUT_BULK = 0x01 # Example placeholder
