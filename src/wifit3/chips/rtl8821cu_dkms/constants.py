"""RTL8821CU (vendor rtl8821cu-5.12.0.4 / HALMAC) — register + protocol constants.

Cleanroom: every value here is pasted verbatim from the vendor source, cited
``[SRC] <file>:<line>`` against ``usb_dumps_new2/captures_rtl8821cu/driver-source/``.
Do NOT type a constant from memory — grep it out.

Scope so far: milestone 1 (USB transport + the HALMAC card-enable power sequence).
Later milestones (chip-id/EFUSE, firmware download, MAC/BB/RF init) append here.
"""
from __future__ import annotations

# --- USB identity ---------------------------------------------------------
# [SRC] os_dep/linux/usb_intf.c:142 (VID) + :263 id_table:
#   {USB_DEVICE_AND_INTERFACE_INFO(USB_VENDER_ID_REALTEK, 0xC820, 0xff,0xff,0xff),
#    .driver_info = RTL8821C}, /* 8821CU */
USB_VID_REALTEK = 0x0BDA
USB_PID_8821CU = 0xC820

# --- Vendor control-transfer convention (Realtek) -------------------------
# [SRC] include/usb_ops.h:19-22,30
REALTEK_USB_VENQT_READ = 0xC0          # bmRequestType for a register read
REALTEK_USB_VENQT_WRITE = 0x40         # bmRequestType for a register write
REALTEK_USB_VENQT_CMD_REQ = 0x05       # bRequest — the vendor register access
REALTEK_USB_VENQT_CMD_IDX = 0x00       # wIndex
MAX_VENDOR_REQ_CMD_SIZE = 254          # [SRC] include/usb_ops.h:30
FW_START_ADDRESS = 0x1000              # [SRC] include/usb_ops_linux.h (FW DL window; no mirror retry)

# --- 8821c USB register-page-switch confirm (the "ON-section mirror") ------
# usbctrl_vendorreq() follows EVERY vendor access to an ON-section register with
# an extra 1-byte bRequest=0x05 write to 0x4E0 carrying the low byte of the IO
# buffer (read-back value for a read, written value for a write). ON-section is
# reg addr <= 0xFF or 0x1000..0x10FF (all under 0xFE00); OFF/LOCAL get no mirror.
# Gated by CONFIG_RTL8821C, so 8821c does it; reproduce it for byte-for-byte parity.
# [SRC] os_dep/linux/usb_ops_linux.c:171-201 (t_reg = 0x4e0 at :191)
REG_PAGE_SWITCH_CONFIRM = 0x04E0
ON_SEC_RANGES = ((0x0000, 0x00FF), (0x1000, 0x10FF))
