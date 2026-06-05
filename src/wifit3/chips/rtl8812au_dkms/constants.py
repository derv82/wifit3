"""RTL8812AU (DKMS/PHYDM) chip-specific constants.

Only the values that DIFFER from the shared 88xxA family base
(``chips/rtl88xxau_base/registers.py``) live here: the USB PID, the TX page boundary
(computed differently than the 8821's), and the EEPROM logical-map offsets that the
2T2R 8812a places differently. Everything else (MAC/FW/EFUSE register addresses, bits)
is imported from the base. [SRC] cites are vendor `file:line`.
"""
from __future__ import annotations

from ..rtl88xxau_base.registers import USB_VID_REALTEK  # noqa: F401  (re-exported for callers)

# --- USB IDs [SRC] os_dep/linux/usb_intf.c:157 ({0x0bda, 0x8812} = RTL8812 default) ---
USB_PID_AWUS036ACH = 0x8812

# --- Path-B RF control [SRC] rtl8812a_spec.h:42. The 2T2R 8812a resets BOTH RF paths at
# hal_init start (REG_RF_CTRL=0x1F path A + this for path B); the 1T1R 8821a skips it. ---
REG_RF_B_CTRL_8812 = 0x0076

# --- TX page boundary [SRC] include/rtl8812a_hal.h:142-168.
#   BCNQ_PAGE_NUM_8812 = MAX_BEACON_LEN/PAGE_SIZE_TX_8812A + 6 = 512/512 + 6 = 0x07
#   The morrownr cold-boot capture's LLT proves the build also reserves FW_NDPA_PAGE_NUM
#   (0x02) — i.e. CONFIG_BEAMFORMER_FW_NDPA is ON — while WOWLAN/DBG stay 0:
#     TX_TOTAL_PAGE_NUMBER_8812 = 0xFF - 0x07 - 0x02 = 0xF6
#     TX_PAGE_BOUNDARY_8812 = 0xF6 + 1 = 0xF7  (LLT: 0x00..0xF6 chain, 0xF6->0xFF end,
#                                               0xF7..0xFF public-queue ring 0xFF->0xF7)
BCNQ_PAGE_NUM_8812 = 0x07
FW_NDPA_PAGE_NUM_8812 = 0x02
TX_TOTAL_PAGE_NUMBER_8812 = 0xFF - BCNQ_PAGE_NUM_8812 - FW_NDPA_PAGE_NUM_8812
TX_PAGE_BOUNDARY_8812 = TX_TOTAL_PAGE_NUMBER_8812 + 1

# --- EEPROM logical-map offsets [SRC] include/hal_pg.h (8812AU). The MAC address sits
# at a different offset than the 8821au's 0x107; the rest are confirmed at M-TXPWR when
# the 2-path PG TX-power block is decoded. ---
EEPROM_MAC_ADDR_8812AU = 0xD7   # [SRC] hal_pg.h:141
EEPROM_XTAL_8812 = 0xB9         # crystal_cap (AFE trim); confirm at M3/M-TXPWR
EEPROM_DEFAULT_CRYSTAL_CAP = 0x20
