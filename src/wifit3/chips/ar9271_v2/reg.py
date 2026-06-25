"""AR9271 MAC/RTC/PHY register addresses + bit values, ported from reg.h / phy.h / hw.h.

Addresses are resolved for the AR9271 (the only silicon this driver claims). Where the kernel
macro is chip-conditional, the comment records the resolved branch so the value is traceable
to the source. Citations: ``data_dumps/ath9k-source-v6.18/ath9k/<file>:line`` at v6.18.
"""
from __future__ import annotations

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

# ---- PHY -------------------------------------------------------------------
AR_PHY_CHIP_ID = 0x9818                # [SRC] phy.h:43

# ---- timing [SRC] hw.h:177-181 ---------------------------------------------
AH_WAIT_TIMEOUT = 100000               # us
AH_TIME_QUANTUM = 10                   # us
POWER_UP_TIME = 10000                  # us

# ---- reset types [SRC] hw.h enum ath9k_reset_type --------------------------
ATH9K_RESET_POWER_ON = 1
ATH9K_RESET_WARM = 2
ATH9K_RESET_COLD = 3
