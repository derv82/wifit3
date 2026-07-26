"""RTL8922AU (rtw89 8922A over USB) register-access constants, ported from rtw89-7.2.
Values are pasted verbatim from the vendor source; each carries its file:line.
"""

# USB vendor control transfer (rtw89_usb_vendorreq). [SRC] usb.h:10-12
RTW89_USB_VENQT = 0x05           # bRequest for register access
RTW89_USB_VENQT_READ = 0xc0      # bmRequestType: vendor + device-to-host (IN)
RTW89_USB_VENQT_WRITE = 0x40     # bmRequestType: vendor + host-to-device (OUT)

# A register address rides the setup packet split as:
#   wValue = addr & 0xFFFF, wIndex = (addr >> 16) & 0xFF   [SRC] usb.c:31-32
_ADDR_VALUE_MASK = 0xFFFF
_ADDR_INDEX_SHIFT = 16
_ADDR_INDEX_MASK = 0xFF

RTW89_USB_VENDORREQ_ATTEMPTS = 10    # retry budget per op. [SRC] usb.c:34
RTW89_USB_VENDORREQ_TIMEOUT_MS = 500  # [SRC] usb.c:47
RTW89_USB_MAX_IO_ERROR = 4           # continual_io_error cap. [SRC] usb.c:70

# CMAC register window. A read here can return R32_DEAD until its clock is on, so
# rtw89_usb_read_cmac re-enables the clock and re-reads. [SRC] mac.h:586-589, reg.h.
R_AX_CMAC_REG_START = 0xC000     # [SRC] reg.h:2141
R_AX_CMAC_REG_END = 0xFFFF       # [SRC] reg.h:3769
RTW89_R32_DEAD = 0xDEADBEEF      # [SRC] core.h:208
MAC_REG_POOL_COUNT = 10          # read_cmac retry budget. [SRC] mac.h:586
R_AX_CK_EN = 0xC004              # [SRC] reg.h:2157
B_AX_CMAC_ALLCKEN = 0xFFFFFFFF   # GENMASK(31, 0). [SRC] reg.h:2159

# Chip power-on / info registers the USB probe touches first. [SRC] reg.h.
R_BE_PAD_CTRL2 = 0x00C4          # USB pad control (rtw89_usb_switch_mode_be). reg.h:4251
R_AX_SYS_CFG1 = 0x00F0           # reg.h:195
R_BE_SYS_CHIPINFO = 0x00FC       # HW id / chip info. reg.h:4314
R_BE_WLAN_XTAL_SI_CTRL = 0x0270  # crystal SI control. reg.h:4670

# rtw89_usb_switch_mode_be: USB2/3 mode switch on R_BE_PAD_CTRL2. [SRC] usb.c:1143-1170, reg.h.
_LIBUSB_SPEED_SUPER = 4          # libusb/pyusb Device.speed for SuperSpeed (USB 3)
USB_SWITCH_DELAY = 0xF           # reg.h:178
B_BE_MATCH_CNT = 0xFF00          # GENMASK(15, 8). reg.h:4263
B_BE_RSM_EN_V1 = 1 << 16         # reg.h:4262
B_BE_NO_PDN_CHIPOFF_V1 = 1 << 17  # reg.h:4261
B_BE_USB3_FORCE = 1 << 21        # reg.h:4260
B_BE_USB2_FORCE = 1 << 22        # reg.h:4259
B_BE_FORCE_U3_CK = 1 << 23       # reg.h:4258
B_BE_FORCE_U2_CK = 1 << 24       # reg.h:4257
B_BE_FORCE_CLK_U2 = 1 << 25      # reg.h:4256
B_BE_USB_AUTO_INSTALL_MASK = 1 << 28  # reg.h:4255
B_BE_USB3_LANE_MODE = 1 << 29    # reg.h:4254
B_BE_USB3_GEN_MODE = 1 << 30     # reg.h:4253
B_BE_USB23_SW_MODE = 1 << 31     # reg.h:4252

# rtw89_read_chip_ver: chip version / hw id. [SRC] core.c:7091, reg.h/mac.h.
B_AX_CHIP_VER_MASK = 0xF000      # GENMASK(15, 12). reg.h:196
B_BE_HW_ID_MASK = 0xFF           # GENMASK(7, 0). reg.h:4319

# XTAL_SI indirect register access. [SRC] mac.c:7179-7233, reg.h/mac.h.
R_AX_WLAN_XTAL_SI_CTRL = 0x0270  # reg.h:268 (same address as the BE name)
B_AX_WL_XTAL_SI_ADDR_MASK = 0x000000FF     # GENMASK(7, 0). reg.h:278
B_AX_WL_XTAL_SI_DATA_MASK = 0x0000FF00     # GENMASK(15, 8). reg.h:277
B_AX_WL_XTAL_SI_MODE_MASK = 0x03000000     # GENMASK(25, 24). reg.h:273
B_AX_WL_XTAL_SI_CMD_POLL = 1 << 31         # BIT(31). reg.h:269
XTAL_SI_NORMAL_READ = 0x01       # reg.h:275
XTAL_SI_CV = 0x41                # mac.h:1666
XTAL_SI_ACV_MASK = 0x0F          # GENMASK(3, 0). mac.h:1667
XTAL_SI_CHIP_ID_L = 0xFD         # mac.h:1696
XTAL_SI_CHIP_ID_H = 0xFE         # mac.h:1697
XTAL_SI_POLL_ATTEMPTS = 1000     # read_poll_timeout(50us, 50ms). [SRC] mac.c:7221
