"""ath9k_hw bring-up over WMI — silicon revision + chip reset.

Ported from hw.c (``__ath9k_hw_init`` and the reset path). Register access goes through the
WMI layer; the write-buffer enable/flush calls map ENABLE_REGWRITE_BUFFER / REGWRITE_BUFFER_FLUSH
so the kernel's batched multi-writes are reproduced exactly. Branches for other ath9k silicon
are ported behind their real macVersion checks but never run for the AR9271 (the only chip
claimed here); they are marked untested.
"""
from __future__ import annotations

import logging

from . import reg as R
from .wmi import WMI

logger = logging.getLogger(__name__)


class AthHw:
    """Mirror of the slice of ``struct ath_hw`` the bring-up touches. Holds the WMI channel
    it drives all register access through."""

    def __init__(self, wmi: WMI):
        self.wmi = wmi
        self.macVersion = 0
        self.macRev = 0
        self.reset_power_on = False
        self.WARegVal = 0                         # 9300+ AR_WA shadow; unused on 9271
        self.phyRev = 0
        self.analog5GhzRev = 0
        self.chip_fullsleep = False               # cleared by chip_reset; reset-type gate
        self.need_an_top2_fixup = False            # set only by the def-eeprom path (not 4k/9271)
        self.rxchainmask = 1                       # from eeprom rx/tx mask (fill_cap_info)
        self.txchainmask = 1
        self.eeprom = bytearray()                 # raw map4k bytes (LE u16 words), filled at post_init
        # saved across a reset, restored by reset_opmode (later milestone):
        self.saveDefAntenna = 0
        self.macStaId1 = 0
        self.saveLedState = 0
        self.tsf = 0

    # ---- silicon-revision predicates [SRC] reg.h:837-928 ------------------
    def is_9271(self) -> bool:
        return self.macVersion == R.AR_SREV_VERSION_9271

    def is_9100(self) -> bool:
        return self.macVersion == 0x14            # AR_SREV_VERSION_9100

    def is_9340(self) -> bool:
        return self.macVersion == 0x300           # AR_SREV_VERSION_9340

    def is_9300_20_or_later(self) -> bool:
        return self.macVersion >= 0x1c0           # AR_SREV_VERSION_9300

    def is_9280_20_or_later(self) -> bool:
        return self.macVersion >= 0x80            # AR_SREV_VERSION_9280 (true for 9271)

    # ---- register access (over WMI) ---------------------------------------
    def read(self, reg: int) -> int:
        return self.wmi.reg_read(reg)

    def multi_read(self, addrs: list[int]) -> list[int]:
        return self.wmi.multi_reg_read(addrs)

    def write(self, reg: int, val: int) -> None:
        self.wmi.reg_write(reg, val)

    def rmw(self, reg: int, set_bits: int, clr_bits: int) -> None:
        self.wmi.reg_rmw(reg, set_bits, clr_bits)

    def enable_write_buffer(self) -> None:
        self.wmi.enable_write_buffer()

    def write_flush(self) -> None:
        self.wmi.write_flush()

    def wait(self, reg: int, mask: int, val: int, timeout: int = R.AH_WAIT_TIMEOUT) -> bool:
        """ath9k_hw_wait: poll ``reg`` until ``(read & mask) == val`` [SRC] hw.c:77."""
        for _ in range(timeout // R.AH_TIME_QUANTUM):
            if (self.read(reg) & mask) == val:
                return True
        self.read(reg)                            # the timeout-path ath_dbg read [SRC] hw.c:90
        return False

    # ---- revision read [SRC] hw.c:255-320 ---------------------------------
    def read_revisions(self) -> bool:
        srev = self.read(R.AR_SREV)
        if srev == 0xFFFFFFFF:
            logger.error("ar9271_v2: failed to read SREV")
            return False
        val = srev & R.AR_SREV_ID
        if val == 0xFF:
            val = srev
            self.macVersion = (val & R.AR_SREV_VERSION2) >> R.AR_SREV_TYPE2_S
            self.macRev = (val & R.AR_SREV_REVISION2) >> R.AR_SREV_REVISION2_S
        else:
            # 5416-style id (other ath9k silicon) — untested here.
            self.macVersion = (val & R.AR_SREV_VERSION) >> R.AR_SREV_VERSION_S
            self.macRev = val & R.AR_SREV_REVISION
        logger.debug("ar9271_v2: macVersion=0x%x macRev=%d", self.macVersion, self.macRev)
        return True

    # ---- chip reset [SRC] hw.c:1351-1530 ----------------------------------
    def set_reset_reg(self, reset_type: int) -> bool:
        if self.is_9300_20_or_later():               # untested here
            self.write(R.AR_WA, self.WARegVal)
        self.write(R.AR_RTC_FORCE_WAKE, R.AR_RTC_FORCE_WAKE_EN | R.AR_RTC_FORCE_WAKE_ON_INT)
        if not self.reset_power_on:
            reset_type = R.ATH9K_RESET_POWER_ON
        if reset_type == R.ATH9K_RESET_POWER_ON:
            ok = self.set_reset_power_on()
            if ok:
                self.reset_power_on = True
            return ok
        return self.set_reset(reset_type)            # WARM / COLD

    def set_reset_power_on(self) -> bool:
        self.enable_write_buffer()
        if self.is_9300_20_or_later():               # untested here
            self.write(R.AR_WA, self.WARegVal)
        self.write(R.AR_RTC_FORCE_WAKE, R.AR_RTC_FORCE_WAKE_EN | R.AR_RTC_FORCE_WAKE_ON_INT)
        if not self.is_9100() and not self.is_9300_20_or_later():
            self.write(R.AR_RC, R.AR_RC_AHB)
        self.write(R.AR_RTC_RESET, 0)
        self.write_flush()

        if not self.is_9100() and not self.is_9300_20_or_later():
            self.write(R.AR_RC, 0)
        self.write(R.AR_RTC_RESET, R.AR_RTC_RESET_EN)

        if not self.wait(R.AR_RTC_STATUS, R.AR_RTC_STATUS_M, R.AR_RTC_STATUS_ON):
            logger.error("ar9271_v2: RTC not waking up")
            return False
        return self.set_reset(R.ATH9K_RESET_WARM)

    def set_reset(self, reset_type: int) -> bool:
        self.enable_write_buffer()
        if self.is_9300_20_or_later():               # untested here
            self.write(R.AR_WA, self.WARegVal)
        self.write(R.AR_RTC_FORCE_WAKE, R.AR_RTC_FORCE_WAKE_EN | R.AR_RTC_FORCE_WAKE_ON_INT)

        if self.is_9100():                           # untested here
            rst_flags = (R.AR_RTC_RC_MAC_WARM | R.AR_RTC_RC_MAC_COLD)
        else:
            tmp = self.read(R.AR_INTR_SYNC_CAUSE)
            tmp &= (R.AR_INTR_SYNC_LOCAL_TIMEOUT | R.AR_INTR_SYNC_RADM_CPL_TIMEOUT)
            if tmp:
                self.write(R.AR_INTR_SYNC_ENABLE, 0)
                val = R.AR_RC_HOSTIF
                if not self.is_9300_20_or_later():
                    val |= R.AR_RC_AHB
                self.write(R.AR_RC, val)
            elif not self.is_9300_20_or_later():
                self.write(R.AR_RC, R.AR_RC_AHB)
            rst_flags = R.AR_RTC_RC_MAC_WARM
            if reset_type == R.ATH9K_RESET_COLD:
                rst_flags |= R.AR_RTC_RC_MAC_COLD

        self.write(R.AR_RTC_RC, rst_flags)
        self.write_flush()

        # udelay (50/10ms/100us depending on silicon) — no wire effect.
        self.write(R.AR_RTC_RC, 0)
        if not self.wait(R.AR_RTC_RC, R.AR_RTC_RC_M, 0):
            logger.error("ar9271_v2: RTC stuck in MAC reset")
            return False
        if not self.is_9100():
            self.write(R.AR_RC, 0)
        return True


    # ---- PLL [SRC] hw.c:761-930 -------------------------------------------
    def init_pll(self, chan) -> None:
        """ath9k_hw_init_pll for the AR9271: write the computed PLL control, switch the core
        clock to 117 MHz, and force the derived sleep clock. (Other-silicon DPLL branches are
        not on the 9271 path.)"""
        from . import phy
        pll = phy.compute_pll_control(self, chan)
        self.write(R.AR_RTC_PLL_CONTROL, pll)
        self.write(R.AR9271_CORE_CLOCK, R.AR9271_CORE_CLOCK_VAL)   # [SRC] hw.c:922-924
        self.write(R.AR_RTC_SLEEP_CLK, R.AR_RTC_FORCE_DERIVED_CLK)

    # ---- power management [SRC] hw.c:2169-2218 ----------------------------
    def set_power_awake(self) -> bool:
        """ath9k_hw_set_power_awake: force the RTC awake and wait for it to come on."""
        if self.is_9300_20_or_later():               # untested here
            self.write(R.AR_WA, self.WARegVal)

        if (self.read(R.AR_RTC_STATUS) & R.AR_RTC_STATUS_M) == R.AR_RTC_STATUS_SHUTDOWN:
            if not self.set_reset_reg(R.ATH9K_RESET_POWER_ON):
                return False
            if not self.is_9300_20_or_later():
                pass                                  # ath9k_hw_init_pll(NULL) — ported with M3
        if self.is_9100():                            # untested here
            self.rmw(R.AR_RTC_RESET, R.AR_RTC_RESET_EN, 0)

        self.rmw(R.AR_RTC_FORCE_WAKE, R.AR_RTC_FORCE_WAKE_EN, 0)   # REG_SET_BIT

        for _ in range(R.POWER_UP_TIME // 50):
            if (self.read(R.AR_RTC_STATUS) & R.AR_RTC_STATUS_M) == R.AR_RTC_STATUS_ON:
                break
            self.rmw(R.AR_RTC_FORCE_WAKE, R.AR_RTC_FORCE_WAKE_EN, 0)
        else:
            logger.error("ar9271_v2: failed to wake up")
            return False

        self.rmw(R.AR_STA_ID1, 0, R.AR_STA_ID1_PWR_SAV)           # REG_CLR_BIT
        return True


    # ---- TSF + phy state [SRC] mac.c / hw.c -------------------------------
    def gettsf64(self) -> int:
        """ath9k_hw_gettsf64 [SRC] mac.c — read U32, then (L32, U32) until the high word is
        stable; here it settles on the first pass."""
        upper1 = self.read(R.AR_TSF_U32)
        lower = 0
        for _ in range(16):                       # ATH9K_MAX_TSF_READ
            lower = self.read(R.AR_TSF_L32)
            upper2 = self.read(R.AR_TSF_U32)
            if upper2 == upper1:
                break
            upper1 = upper2
        return (upper1 << 32) | lower

    def mark_phy_inactive(self) -> None:
        self.write(R.AR_PHY_ACTIVE, R.AR_PHY_ACTIVE_DIS)   # [SRC] hw.c ath9k_hw_mark_phy_inactive

    def settsf64(self, tsf: int) -> None:
        """ath9k_hw_settsf64 [SRC] mac.c — write the low then high TSF word. The caller passes
        tsf + a wall-clock offset; in replay the offset is 0 (the low word is value-excepted in
        the gate, since it can never be byte-reproduced)."""
        self.write(R.AR_TSF_L32, tsf & 0xffffffff)
        self.write(R.AR_TSF_U32, (tsf >> 32) & 0xffffffff)

    # ---- chip reset (within ath9k_hw_reset) [SRC] hw.c:1519-1541 ----------
    def chip_reset(self, chan) -> None:
        """ath9k_hw_chip_reset: pick WARM unless TX/RX is pending (or the chip is full-asleep),
        reset, then re-init the PLL. chip_fullsleep is False by this second reset, so the
        AR_Q_TXE / AR_CR probes run."""
        reset_type = R.ATH9K_RESET_WARM
        if self.chip_fullsleep or self.read(R.AR_Q_TXE) or (self.read(R.AR_CR) & R.AR_CR_RXE):
            reset_type = R.ATH9K_RESET_COLD
        self.set_reset_reg(reset_type)
        # ath9k_hw_setpower(AWAKE) here is a no-op — power_mode is already AWAKE.
        self.chip_fullsleep = False
        self.init_pll(chan)

    def reset_begin(self, chan) -> None:
        """The opening of ath9k_hw_reset [SRC] hw.c:1859-1943, through chip_reset: save the
        antenna/sta-id/TSF/LED state, mark the PHY inactive, the AR9271 RF reset, chip_reset,
        then the AR9271 MAC-gate write. (TSF restore, JTAG-disable and process_ini follow.)"""
        self.saveDefAntenna = self.read(R.AR_DEF_ANTENNA) or 1
        self.macStaId1 = self.read(R.AR_STA_ID1) & R.AR_STA_ID1_BASE_RATE_11B
        self.tsf = self.gettsf64()
        self.saveLedState = self.read(R.AR_CFG_LED) & R.AR_CFG_LED_SAVE_MASK
        self.mark_phy_inactive()
        # AR9271 + htc_reset_init: pulse the radio RF reset before the chip reset [SRC] hw.c:1924.
        self.write(R.AR9271_RESET_POWER_DOWN_CONTROL, R.AR9271_RADIO_RF_RST)
        self.chip_reset(chan)
        # ... and gate the MAC clock after [SRC] hw.c:1937.
        self.write(R.AR9271_RESET_POWER_DOWN_CONTROL, R.AR9271_GATE_MAC_CTL)
        # Restore TSF (low word value-excepted) [SRC] hw.c:1946-1947.
        self.settsf64(self.tsf)
        # Disable JTAG so GPIO 0-3 are usable [SRC] hw.c:1949-1950.
        if self.is_9280_20_or_later():
            self.rmw(R.AR_GPIO_INPUT_EN_VAL, R.AR_GPIO_JTAG_DISABLE, 0)


def init_reset(wmi: WMI) -> AthHw:
    """The opening of __ath9k_hw_init: read the silicon revision, power-on reset the chip, wake
    it, and read the PHY revision [SRC] hw.c:573-641. Returns the AthHw so later milestones keep
    the same register channel."""
    hw = AthHw(wmi)
    if not hw.read_revisions():
        raise RuntimeError("ar9271_v2: read_revisions failed")
    if not hw.set_reset_reg(R.ATH9K_RESET_POWER_ON):
        raise RuntimeError("ar9271_v2: chip power-on reset failed")
    if not hw.set_power_awake():
        raise RuntimeError("ar9271_v2: failed to wake chip")
    hw.phyRev = hw.read(R.AR_PHY_CHIP_ID)             # [SRC] hw.c:641
    return hw
