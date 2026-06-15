"""RTL8822BU (morrownr rtl88x2bu / HALMAC+PHYDM) — register + protocol constants.

Cleanroom: every value here is pasted verbatim from the vendor source, cited
``[SRC] <file>:<line>``. Do NOT type a constant from memory — grep it out.

Scope so far: M0 (enumerate + chip-version probe + transport) and M1 (HALMAC
power sequence + the warm-reboot reset workaround). Later milestones append here.
"""
from __future__ import annotations

# --- USB identity ---------------------------------------------------------
# TP-Link Archer T3U Plus v1 (the dev card); in the DKMS supported-device-IDs.
# [SRC] usb-topology.log: "2357:0138 TP-Link 802.11ac NIC"
USB_VID_TPLINK = 0x2357
USB_PID_ARCHER_T3U_PLUS = 0x0138

# --- Vendor control-transfer convention (Realtek rtw88-family) ------------
# [SRC] include/usb_ops.h:19-22
REALTEK_USB_VENQT_READ = 0xC0          # bmRequestType for a register read
REALTEK_USB_VENQT_WRITE = 0x40         # bmRequestType for a register write
REALTEK_USB_VENQT_CMD_REQ = 0x05       # bRequest — the vendor register access
REALTEK_USB_VENQT_CMD_IDX = 0x00       # wIndex
MAX_VENDOR_REQ_CMD_SIZE = 254          # [SRC] include/usb_ops.h:30
FW_START_ADDRESS = 0x1000              # [SRC] include/usb_ops_linux.h:19

# --- 8822b/8821c/8822c USB register-page-switch workaround ----------------
# usbctrl_vendorreq() emits, after EVERY vendor access to an "ON-section"
# register, an extra 1-byte bRequest=0x05 write to 0x4E0 carrying the low byte
# of the IO buffer (read-back value for reads, written value for writes). The
# ON-section is reg addr <= 0xFF or 0x1000..0x10FF; everything else (OFF/LOCAL)
# gets no mirror. This is the chip's banked-register confirm; it must be
# reproduced for byte-faithfulness. [SRC] os_dep/linux/usb_ops_linux.c:171-201
REG_PAGE_SWITCH_CONFIRM = 0x04E0       # REG_NULL_PKT_STATUS_V1 [SRC] halmac_reg_8822b.h:379
ON_SEC_RANGES = ((0x0000, 0x00FF), (0x1000, 0x10FF))

# --- M0/M1 registers (8822b) ----------------------------------------------
# [SRC] hal/halmac/halmac_reg_8822b.h
REG_SYS_FUNC_EN = 0x0002               # :20
REG_WL_BT_PWR_CTRL = 0x0068            # :49
REG_MCUFW_CTRL = 0x0080                # :55  (FW-ready / boot-from-flash)
REG_SYS_CFG1 = 0x00F0                  # :82  (chip version / cut / vendor)
REG_SYS_STATUS1 = 0x00F4               # :83  (+1 BIT0 = power state probe)
REG_SYS_CFG2 = 0x00FC                  # :85
REG_SW_MDIO = 0x10C0                   # :96
REG_CR = 0x0100                        # :109 (0xEA marks the chip disabled)

# --- power-state detection markers (mac_pwr_switch_usb_8822b) --------------
# [SRC] hal/halmac/halmac_88xx/halmac_8822b/halmac_usb_8822b.c:44-92
REG_RPWM = 0xFE58                      # :44  (RPWM — leave-32K toggle)
MCUFW_CTRL_FW_EXIST = 0xC078           # :47  REG_MCUFW_CTRL value == FW still loaded
REG_CR_DISABLED = 0xEA                 # :54  REG_CR value == chip already disabled
