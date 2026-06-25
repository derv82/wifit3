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

    # ---- silicon-revision predicates [SRC] reg.h:837-928 ------------------
    def is_9271(self) -> bool:
        return self.macVersion == R.AR_SREV_VERSION_9271

    def is_9100(self) -> bool:
        return self.macVersion == 0x14            # AR_SREV_VERSION_9100

    def is_9340(self) -> bool:
        return self.macVersion == 0x300           # AR_SREV_VERSION_9340

    def is_9300_20_or_later(self) -> bool:
        return self.macVersion >= 0x1c0           # AR_SREV_VERSION_9300

    # ---- register access (over WMI) ---------------------------------------
    def read(self, reg: int) -> int:
        return self.wmi.reg_read(reg)

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


def init_reset(wmi: WMI) -> AthHw:
    """The opening of __ath9k_hw_init: read the silicon revision, then power-on reset the chip
    [SRC] hw.c:573,615. Returns the AthHw so later milestones keep the same register channel."""
    hw = AthHw(wmi)
    if not hw.read_revisions():
        raise RuntimeError("ar9271_v2: read_revisions failed")
    if not hw.set_reset_reg(R.ATH9K_RESET_POWER_ON):
        raise RuntimeError("ar9271_v2: chip power-on reset failed")
    return hw
