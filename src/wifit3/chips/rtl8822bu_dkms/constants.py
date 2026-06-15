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
# [SRC] hal/halmac/halmac_reg_8822b.h, halmac_reg2.h
REG_SYS_FUNC_EN = 0x0002               # :20  (+1 SYS_FUNC_EN in init_system_cfg)
REG_RSV_CTRL = 0x001C                  # reg2.h:149  (pre_init clears this first)
REG_RF_CTRL = 0x001F                   # reg2.h:166  (enable_bb_rf BIT0/1/2)
REG_GPIO_MUXCFG = 0x0040               # reg2.h:328  (pre_init sets BIT2; init_system_cfg FSPI)
REG_LED_CFG = 0x004C                   # reg2.h:365  (pre_init clears BIT25/26)
REG_PAD_CTRL1 = 0x0064                 # reg2.h:388  (pre_init sets BIT28/29 PIN-mux)
REG_WL_BT_PWR_CTRL = 0x0068            # :49
REG_MCUFW_CTRL = 0x0080                # :55  (FW-ready / boot-from-flash)
REG_WLRF1 = 0x00EC                     # reg2.h:798  (enable_bb_rf BIT24/25/26)
REG_SYS_CFG1 = 0x00F0                  # :82  (chip version / cut / vendor; +2 test-mode BIT4)
REG_SYS_STATUS1 = 0x00F4               # :83  (+1 BIT0 = power state probe)
REG_SYS_CFG2 = 0x00FC                  # :85  (+3 == 0x20 => USB3 link)
REG_CR = 0x0100                        # :109 (0xEA marks the chip disabled)
REG_CPU_DMEM_CON = 0x1080              # reg2.h:6204 (init_system_cfg WL_PLATFORM_RST)
REG_SW_MDIO = 0x10C0                   # :96  (+3 BIT0 = post-power-on read-twice probe)
REG_PRE_INIT_FE5B = 0xFE5B             # pre_init USB3-only |= BIT(4) [SRC] halmac_init_8822b.c:963

# init_system_cfg bit/value constants [SRC] halmac_init_8822b.c:36,724-735, halmac_bit2.h
SYS_FUNC_EN = 0xDC                     # OR'd into REG_SYS_FUNC_EN+1
BIT_WL_PLATFORM_RST = 1 << 16          # bit2.h:58085
BIT_BOOT_FSPI_EN = 1 << 20             # bit2.h:12788 (boot-from-flash; cleared for driver FW DL)
BIT_FSPI_EN = 1 << 19                  # bit2.h:7200

# --- Firmware download (HALMAC iDDMA) -------------------------------------
# FW blob: morrownr array_mp_8822b_fw_nic (v30.20, 161240 B) — NOT the linux-firmware
# rtw88 blob (161176 B, different version). The cold captures were taken with the morrownr
# driver, so the vendor array is the wire ground truth. [SRC] hal/rtl8822b/hal8822b_fw.c:13389
FW_BLOB_SIZE = 161240
# WLAN_FW header field offsets [SRC] hal/halmac/halmac_fw_info.h:22-40
WLAN_FW_HDR_SIZE = 64
WLAN_FW_HDR_CHKSUM_SIZE = 8
WLAN_FW_HDR_MEM_USAGE = 24             # BIT(4) => emem present
WLAN_FW_HDR_H2C_FMT_VER = 28
WLAN_FW_HDR_DMEM_ADDR = 32
WLAN_FW_HDR_DMEM_SIZE = 36
WLAN_FW_HDR_IMEM_SIZE = 48
WLAN_FW_HDR_EMEM_SIZE = 52
WLAN_FW_HDR_EMEM_ADDR = 56
WLAN_FW_HDR_IMEM_ADDR = 60
# DMA / packet sizing [SRC] halmac_88xx_cfg.h:29,38, halmac_init_88xx.c:60, h2c_extra_info_nic.h:25
TX_DESC_SIZE_88XX = 48
OCPBASE_TXBUF_88XX = 0x18780000
DLFW_PKT_MAX_SIZE = 8192
DLFW_RSVDPG_SIZE = 2048

# --- power-state detection markers (mac_pwr_switch_usb_8822b) --------------
# [SRC] hal/halmac/halmac_88xx/halmac_8822b/halmac_usb_8822b.c:44-92
REG_RPWM = 0xFE58                      # :44  (RPWM — leave-32K toggle)
MCUFW_CTRL_FW_EXIST = 0xC078           # :47  REG_MCUFW_CTRL value == FW still loaded
REG_CR_DISABLED = 0xEA                 # :54  REG_CR value == chip already disabled

# --- EFUSE read (HALMAC physical-map dump + 8822b logical parse) -----------
# The chip-info probe reads the EFUSE up front (before power-on): rtl8822b_read_efuse
# -> EFUSE_ShadowMapUpdate -> halmac dump_efuse_drv_88xx -> read_hw_efuse_88xx.
# [SRC] hal/rtl8822b/rtl8822b_ops.c:616, hal/halmac/halmac_88xx/halmac_efuse_88xx.c:1507,1089
REG_SYS_EEPROM_CTRL = 0x000A           # [SRC] halmac_reg_8822b.h:23 (autoload/eeprom-sel flags)
BIT_AUTOLOAD_SUS = 1 << 5              # [SRC] halmac_bit_8822b.h:129 — set => autoload OK
BIT_EERPOMSEL = 1 << 4                 # [SRC] halmac_bit_8822b.h:130 — set => EEPROM, else eFuse
REG_EFUSE_CTRL = 0x0030                # [SRC] halmac_reg_8822b.h:34 (32-bit access/data/addr)
REG_LDO_EFUSE_CTRL = 0x0034            # [SRC] halmac_reg_8822b.h:35 (+1 bank, +3 LDO25 enable)

# REG_EFUSE_CTRL (0x30) field layout [SRC] halmac_bit_8822b.h:688,726-738
BIT_EF_FLAG = 1 << 31                  # read/write-done strobe (poll until set on read)
BIT_SHIFT_EF_ADDR = 8
BIT_MASK_EF_ADDR = 0x3FF               # physical byte address [17:8]
BITS_EF_ADDR = BIT_MASK_EF_ADDR << BIT_SHIFT_EF_ADDR
BIT_MASK_EF_DATA = 0xFF                # data byte [7:0]

# Map sizes [SRC] halmac_88xx/halmac_8822b/halmac_8822b_cfg.h:55-58 + halmac_efuse_88xx.c:23-24
EFUSE_SIZE_8822B = 1024                # physical map (read addr 0..1023)
EEPROM_SIZE_8822B = 768                # logical map produced by the PG-header parser
PRTCT_EFUSE_SIZE_8822B = 96            # protected tail (bounds the parser walk)
HALMAC_EFUSE_BANK_WIFI = 0             # [SRC] halmac_type.h:1771

# Logical-map field offsets (8822BU) [SRC] include/hal_pg.h:453-479
EEPROM_CHANNEL_PLAN = 0x00B8           # :453
EEPROM_XTAL = 0x00B9                   # :454  crystal_cap
EEPROM_THERMAL_METER = 0x00BA          # :455
EEPROM_VERSION = 0x00C4                # :470
EEPROM_RFE_OPTION = 0x00CA             # :475  rfe_type (RF front-end variant)
EEPROM_MAC_ADDR = 0x0107               # :479  (the 8822bU MAC sits past the 256-byte page)
EFUSE_PA_BIAS = 0x03D7                  # physical efuse PA-bias pair [SRC] rtl8822b_ops.c:560
# Field defaults when the efuse byte is blank (0xFF) or the map is invalid.
EEPROM_DEFAULT_CRYSTAL_CAP = 0x00      # [SRC] hal_pg.h:841 EEPROM_Default_CrystalCap (8822b uses generic)
EEPROM_DEFAULT_THERMAL_METER = 0x12    # [SRC] hal_pg.h:827 EEPROM_Default_ThermalMeter
