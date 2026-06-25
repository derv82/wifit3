"""AR9271 MAC/RTC/PHY register addresses + bit values, ported from reg.h / phy.h / hw.h.

Addresses are resolved for the AR9271 (the only silicon this driver claims). Where the kernel
macro is chip-conditional, the comment records the resolved branch so the value is traceable
to the source. Citations: ``data_dumps/ath9k-source-v6.18/ath9k/<file>:line`` at v6.18.
"""
from __future__ import annotations


def _shift(mask: int) -> int:
    return (mask & -mask).bit_length() - 1


def SM(value: int, mask: int) -> int:
    """Set-field: place ``value`` into the bits ``mask`` covers (ath9k SM macro)."""
    return (value << _shift(mask)) & mask


def MS(value: int, mask: int) -> int:
    """Get-field: extract the bits ``mask`` covers from ``value`` (ath9k MS macro)."""
    return (value & mask) >> _shift(mask)

# ---- SREV (silicon revision) [SRC] reg.h:753-795 ---------------------------
AR_SREV = 0x4020                       # AR_SREV(ah) for non-9100/9340
AR_SREV_ID = 0x000000FF                # non-9100 mask
AR_SREV_VERSION = 0x000000F0
AR_SREV_VERSION_S = 4
AR_SREV_REVISION = 0x00000007
AR_SREV_VERSION2 = 0xFFFC0000
AR_SREV_TYPE2_S = 12
AR_SREV_TYPE2_HOST_MODE = 0x00002000
AR_SREV_REVISION2 = 0x00000F00
AR_SREV_REVISION2_S = 8
AR_SREV_VERSION_9271 = 0x140           # [SRC] reg.h:795

# ---- reset / RTC [SRC] reg.h:697-702,1041-1063,1342-1406 -------------------
AR_WA = 0x4004                         # AR_WA(ah) non-9340 [SRC] reg.h:702 (9300+ only)
AR_WA_D3_L1_DISABLE = 0x00004000
AR_WA_ASPM_TIMER_BASED_DISABLE = 0x00020000

AR_RC = 0x4000                         # MAC DMA reset control
AR_RC_AHB = 0x00000001
AR_RC_HOSTIF = 0x00000100

AR_INTR_SYNC_CAUSE = 0x4028            # non-9340
AR_INTR_SYNC_ENABLE = 0x402c           # non-9340
AR_INTR_SYNC_RADM_CPL_TIMEOUT = 0x00001000
AR_INTR_SYNC_LOCAL_TIMEOUT = 0x00002000

AR_RTC_RC = 0x7000                     # non-9100
AR_RTC_RC_M = 0x00000003
AR_RTC_RC_MAC_WARM = 0x00000001
AR_RTC_RC_MAC_COLD = 0x00000002

AR_RTC_RESET = 0x7040                  # non-9100
AR_RTC_RESET_EN = 0x00000001

AR_RTC_STATUS = 0x7044                 # non-9100
AR_RTC_STATUS_M = 0x0000000f           # non-9100
AR_RTC_STATUS_ON = 0x00000002
AR_RTC_STATUS_SHUTDOWN = 0x00000001    # [SRC] reg.h:1393

AR_RTC_FORCE_WAKE = 0x704c             # non-9100
AR_RTC_FORCE_WAKE_EN = 0x00000001
AR_RTC_FORCE_WAKE_ON_INT = 0x00000002

# ---- station id / power save -----------------------------------------------
AR_STA_ID0 = 0x8000                    # [SRC] ath/reg.h:26 (shared ath common)
AR_STA_ID1 = 0x8004
AR_STA_ID1_PWR_SAV = 0x00040000        # [SRC] reg.h:1621
AR_STA_ID1_BASE_RATE_11B = 0x02000000  # [SRC] reg.h:1629

# ---- reset preamble (ath9k_hw_reset) [SRC] reg.h:395-1721 ------------------
AR_CR = 0x0008
AR_CR_RXE = 0x00000004                 # non-9300
AR_Q_TXE = 0x0840
AR_TSF_L32 = 0x804c
AR_TSF_U32 = 0x8050
AR_DEF_ANTENNA = 0x8058
AR_CFG_LED = 0x1f04
AR_CFG_LED_SAVE_MASK = 0x00000c00 | 0x00000380 | 0x00000070 | 0x00000008  # ASSOC|MODE|THRESH|SLOW
AR_PHY_ACTIVE = 0x981c                 # [SRC] ar9002_phy.h:50
AR_PHY_ACTIVE_DIS = 0x00000000
AR_GPIO_INPUT_EN_VAL = 0x4054          # non-9340/non-9300
AR_GPIO_JTAG_DISABLE = 0x00020000
AR9271_RESET_POWER_DOWN_CONTROL = 0x50044   # [SRC] reg.h:1615
AR9271_RADIO_RF_RST = 0x20
AR9271_GATE_MAC_CTL = 0x4000

# ---- EEPROM access (USB) [SRC] eeprom.h:66-68,177 / reg.h:1250-1256 --------
AR5416_EEPROM_OFFSET = 0x2000          # EEPROM word window base
AR5416_EEPROM_S = 2                    # word -> byte-address shift (<<2)
AR_EEPROM_STATUS_DATA = 0x407c         # non-9340
AR_EEPROM_STATUS_DATA_VAL = 0x0000ffff
AR_EEPROM_STATUS_DATA_BUSY = 0x00010000
AR_EEPROM_STATUS_DATA_PROT_ACCESS = 0x00040000

SIZE_EEPROM_4K = 188                   # sizeof(ar5416_eeprom_4k)/2 [SRC] eeprom_4k.c:36
AR5416_EEP4K_START_LOC = 64            # [SRC] eeprom_4k.c:56
AR5416_EEPROM_MAGIC = 0xa55a           # [SRC] eeprom.h:36-41 (#else = little-endian host)
AR5416_EEPROM_MAGIC_OFFSET = 0x0       # [SRC] eeprom.h:66
AR5416_EEPMISC_BIG_ENDIAN = 0x01       # [SRC] eeprom.h:177
AR5416_EEP_VER = 0xE                   # [SRC] eeprom.h:133
AR5416_EEP_NO_BACK_VER = 0x1           # [SRC] eeprom.h:132
AR5416_EEP_VER_MAJOR_SHIFT = 12        # [SRC] eeprom.h:134-136
AR5416_EEP_VER_MAJOR_MASK = 0xF000
AR5416_EEP_VER_MINOR_MASK = 0x0FFF

# ---- radio revision (ar9002) [SRC] reg.h:1014-1018 -------------------------
AR_RADIO_SREV_MAJOR = 0xf0
AR_RAD5133_SREV_MAJOR = 0xc0
AR_RAD2133_SREV_MAJOR = 0xd0
AR_RAD5122_SREV_MAJOR = 0xe0
AR_RAD2122_SREV_MAJOR = 0xf0

# ---- ANI / MIB counters [SRC] reg.h:1767-1862 + ath/reg.h:20-24 ------------
AR_MIBC = 0x0040                       # [SRC] ath/reg.h:20 (shared ath common)
AR_MIBC_COW = 0x00000001
AR_MIBC_FMC = 0x00000002
AR_MIBC_CMC = 0x00000004
AR_MIBC_MCS = 0x00000008

AR_RTS_OK = 0x8088
AR_RTS_FAIL = 0x808c
AR_ACK_FAIL = 0x8090
AR_FCS_FAIL = 0x8094
AR_BEACON_CNT = 0x8098

AR_FILT_OFDM = 0x8124
AR_FILT_CCK = 0x8128
AR_PHY_ERR_1 = 0x812c
AR_PHY_ERR_MASK_1 = 0x8130
AR_PHY_ERR_2 = 0x8134
AR_PHY_ERR_MASK_2 = 0x8138
AR_PHY_ERR_OFDM_TIMING = 0x00020000    # [SRC] reg.h:1820
AR_PHY_ERR_CCK_TIMING = 0x02000000     # [SRC] reg.h:1821

# ---- GPIO / LED [SRC] reg.h:1159-1244 + hw.h:144 + htc.h:397 ---------------
AR_GPIO_IN_OUT = 0x4048                # non-9340
AR_GPIO_OE_OUT = 0x404c                # non-9340/non-9300
AR_GPIO_OE_OUT_DRV_NO = 0x0
AR_GPIO_OE_OUT_DRV_ALL = 0x3
AR_GPIO_OE_OUT_DRV = 0x3
AR_GPIO_OUTPUT_MUX3 = 0x4068           # non-9340/non-9300 (gpio > 11, e.g. the led pin)
AR_GPIO_OUTPUT_MUX_AS_OUTPUT = 0       # [SRC] hw.h:144
ATH_LED_PIN_9271 = 15                  # [SRC] htc.h:397

# ---- key cache [SRC] ath/reg.h:42-62 + hw.h:180 ----------------------------
AR_KEYTABLE_0 = 0x8800
AR_KEYTABLE_SIZE = 128
AR_KEYTABLE_TYPE_CLR = 0x00000007
AR_KEYTABLE_TYPE_TKIP = 0x00000004
def AR_KEYTABLE(n: int) -> int:        # AR_KEYTABLE(_n) = AR_KEYTABLE_0 + (_n * 32)
    return AR_KEYTABLE_0 + (n * 32)

# ---- PHY [SRC] phy.h:24-25,43 ----------------------------------------------
AR_PHY_BASE = 0x9800
def AR_PHY(n: int) -> int:             # AR_PHY(_n) = AR_PHY_BASE + (_n << 2)
    return AR_PHY_BASE + (n << 2)
AR_PHY_CHIP_ID = 0x9818                # [SRC] phy.h:43

# ---- PLL / clock [SRC] reg.h:1334-1400 -------------------------------------
AR_RTC_PLL_CONTROL = 0x7014            # non-9100/soc
AR_RTC_9160_PLL_DIV = 0x000003ff
AR_RTC_9160_PLL_REFDIV = 0x00003c00
AR_RTC_9160_PLL_CLKSEL = 0x0000c000
AR_RTC_SLEEP_CLK = 0x7048              # non-9100
AR_RTC_FORCE_DERIVED_CLK = 0x2
AR9271_CORE_CLOCK = 0x50040            # [SRC] hw.c:924 "switch core clock to 117MHz"
AR9271_CORE_CLOCK_VAL = 0x304

# ---- timing [SRC] hw.h:177-181 ---------------------------------------------
AH_WAIT_TIMEOUT = 100000               # us
AH_TIME_QUANTUM = 10                   # us
POWER_UP_TIME = 10000                  # us

# ---- reset types [SRC] hw.h enum ath9k_reset_type --------------------------
ATH9K_RESET_POWER_ON = 1
ATH9K_RESET_WARM = 2
ATH9K_RESET_COLD = 3
