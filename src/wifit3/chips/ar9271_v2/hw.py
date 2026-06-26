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
        self.tsf2_enabled = False                 # set only when a tsf2 gen-timer is allocated
        self.gpio_mask = 0                         # no GPIO override on the STA bring-up path
        self.htc_reset_init = True                 # AR9271 RF-reset pulse fires once [SRC] hw.c:1923
        self.curchan = None                        # set after the first reset (gates getnf)
        # TX-power regulatory state (ath_regulatory + ieee80211_channel):
        self.chan_max_power = 20                    # channel->max_power [SRC] common-init.c:26
        self.reg_power_limit = 254                  # reg->power_limit = MAX_COMBINED_POWER
        self.max_power_level = 0                    # reg->max_power_level (set by apply_txpower)
        # MAC/operating-mode state (ath_common) — htc cold-start defaults:
        self.macaddr = bytearray(6)               # latched from eeprom at init
        self.bssidmask = bytearray(b"\xff" * 6)   # listen to all (set_bssid_mask default)
        self.curbssid = bytearray(6)              # 00:00:00:00:00:00 until associated
        self.curaid = 0
        self.opmode = R.IFTYPE_STATION             # ath9k_htc default interface type
        self.is_monitoring = False
        self.nvifs = 1                             # active vifs (affects the RX filter)
        self.conf_is_ht = False                    # mac80211 HT conf (affects the RX filter)
        self.rxfilter_flags = None                 # persisted mac80211 FIF_* flags (rx.FilterFlags)
        self.sta_id1_defaults = R.AR_STA_ID1_DEFAULTS
        self.sw_mgmt_crypto_tx = True
        self.sw_mgmt_crypto_rx = True
        # TX-queue subsystem (ath9k_tx_queue_info[]) + interrupt-mask shadows:
        self.txq: list = []                        # populated by mac_queue.init_tx_queues
        self.txok_interrupt_mask = 0
        self.txerr_interrupt_mask = 0
        self.txdesc_interrupt_mask = 0
        self.txeol_interrupt_mask = 0
        self.txurn_interrupt_mask = 0
        self.imrs2_reg = 0
        self.intr_txqs = 0
        self.sw_beacon_response_time = 6           # [SRC] hw.c:400
        self.dma_beacon_response_time = 1          # [SRC] hw.c:399
        self.rx_intr_mitigation = True             # [SRC] hw.c:404
        self.tx_intr_mitigation = False
        self.clockrate = R.ATH9K_CLOCK_RATE_2GHZ_OFDM   # set_clockrate (2 GHz OFDM)
        self.slottime = 9                          # [SRC] hw.c:470
        self.coverage_class = 0
        self.globaltxtimeout = 0xFFFFFFFF          # (u32)-1 -> set_global_txtimeout skipped
        self.misc_mode = R.AR_PCU_MIC_NEW_LOC_ENA   # [SRC] hw.c:2558 (no KEYSEARCH cap)
        self.dynack_enabled = False
        self.tx_trig_level = R.AR_FTRIG_256B >> R.AR_FTRIG_S   # [SRC] hw.c:483 (9271 = 256B)
        self.rimt_last = 250                       # [SRC] hw.c:410 (non-9300)
        self.rimt_first = 700

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

    def rmw_field(self, reg: int, mask: int, value: int) -> None:
        """REG_RMW_FIELD [SRC] reg.h — set the bits ``mask`` covers to ``value``."""
        self.rmw(reg, R.SM(value, mask), mask)

    def enable_write_buffer(self) -> None:
        self.wmi.enable_write_buffer()

    def write_flush(self) -> None:
        self.wmi.write_flush()

    def enable_rmw_buffer(self) -> None:
        self.wmi.enable_rmw_buffer()

    def rmw_buffer_flush(self) -> None:
        self.wmi.rmw_flush()

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
        # AR9271 + htc_reset_init: pulse the radio RF reset around the chip reset, but only on
        # the first (cold) reset; the channel-change resets skip it [SRC] hw.c:1923-1943.
        first = self.is_9271() and self.htc_reset_init
        if first:
            self.write(R.AR9271_RESET_POWER_DOWN_CONTROL, R.AR9271_RADIO_RF_RST)
        self.chip_reset(chan)
        if first:
            self.htc_reset_init = False
            self.write(R.AR9271_RESET_POWER_DOWN_CONTROL, R.AR9271_GATE_MAC_CTL)
        # Restore TSF (low word value-excepted) [SRC] hw.c:1946-1947.
        self.settsf64(self.tsf)
        # Disable JTAG so GPIO 0-3 are usable [SRC] hw.c:1949-1950.
        if self.is_9280_20_or_later():
            self.rmw(R.AR_GPIO_INPUT_EN_VAL, R.AR_GPIO_JTAG_DISABLE, 0)

    def getnf(self, chan) -> bool:
        """ath9k_hw_getnf [SRC] calib.c:397 — at the top of a channel-change reset, peek the AGC
        noise-floor status. If the NF measurement is still pending (AR_PHY_AGC_CONTROL_NF set,
        as on the cold→warm boundary) it returns early after the single read."""
        if self.read(R.AR_PHY_AGC_CONTROL) & R.AR_PHY_AGC_CONTROL_NF:
            return False                          # NF did not complete in the cal window
        raise NotImplementedError("ar9271_v2: NF history update not ported (unseen on this wire)")

    def _setbssidmask(self) -> None:
        """ath_hw_setbssidmask [SRC] ath/hw.c — program the MAC into STA_ID0/1 (preserving the
        upper STA_ID1 bits) and the listen-to-all BSSID mask."""
        self.write(R.AR_STA_ID0, int.from_bytes(self.macaddr[0:4], "little"))
        id1 = self.read(R.AR_STA_ID1) & ~R.AR_STA_ID1_SADH_MASK
        id1 |= int.from_bytes(self.macaddr[4:6], "little")
        self.write(R.AR_STA_ID1, id1)
        self.write(R.AR_BSSMSKL, int.from_bytes(self.bssidmask[0:4], "little"))
        self.write(R.AR_BSSMSKU, int.from_bytes(self.bssidmask[4:6], "little"))

    def _write_associd(self) -> None:
        """ath9k_hw_write_associd [SRC] hw.c — the current BSSID / association id (both 0 at
        cold start)."""
        self.write(R.AR_BSS_ID0, int.from_bytes(self.curbssid[0:4], "little"))
        self.write(R.AR_BSS_ID1, int.from_bytes(self.curbssid[4:6], "little")
                   | ((self.curaid & 0x3fff) << R.AR_BSS_ID1_AID_S))

    def set_operating_mode(self, opmode: int) -> None:
        """ath9k_hw_set_operating_mode [SRC] hw.c:1267 — STATION clears the AP/adhoc indication
        and enables key search (KSRCH_MODE); the AP/adhoc/monitor branches are ported but
        unused here."""
        mask = R.AR_STA_ID1_STA_AP | R.AR_STA_ID1_ADHOC
        bit_set = R.AR_STA_ID1_KSRCH_MODE
        self.enable_rmw_buffer()
        if opmode == R.IFTYPE_STATION:
            self.rmw(R.AR_CFG, 0, R.AR_CFG_AP_ADHOC_INDICATION)   # REG_CLR_BIT
        elif not self.is_monitoring:                              # untested (AP/adhoc/mesh)
            bit_set = 0
        self.rmw(R.AR_STA_ID1, bit_set, mask)
        self.rmw_buffer_flush()

    def reset_opmode(self, macStaId1: int, saveDefAntenna: int) -> None:
        """ath9k_hw_reset_opmode [SRC] hw.c:1700 — re-apply the STA id/defaults, BSSID mask,
        saved antenna, association id, then clear ISR and seed the RSSI threshold."""
        self.enable_write_buffer()
        self.rmw(R.AR_STA_ID1, macStaId1 | R.AR_STA_ID1_RTS_USE_DEF | self.sta_id1_defaults,
                 (~R.AR_STA_ID1_SADH_MASK) & 0xFFFFFFFF)
        self._setbssidmask()
        self.write(R.AR_DEF_ANTENNA, saveDefAntenna)
        self._write_associd()
        self.write(R.AR_ISR, 0xFFFFFFFF)
        self.write(R.AR_RSSI_THR, R.INIT_RSSI_THR)
        self.write_flush()
        self.set_operating_mode(self.opmode)

    def init_interrupt_masks(self) -> None:
        """ath9k_hw_init_interrupt_masks [SRC] hw.c:932 — seed AR_IMR / AR_IMR_S2 and the PCIe
        interrupt-sync registers. The 9271 (not 9300, rx-mitigation on, tx-mitigation off)
        uses the RXINTM/RXMINTR + TXOK combination."""
        imr_reg = (R.AR_IMR_TXERR | R.AR_IMR_TXURN | R.AR_IMR_RXERR | R.AR_IMR_RXORN
                   | R.AR_IMR_BCNMISC)
        if self.rx_intr_mitigation:
            imr_reg |= R.AR_IMR_RXINTM | R.AR_IMR_RXMINTR
        else:
            imr_reg |= R.AR_IMR_RXOK
        if self.tx_intr_mitigation:
            imr_reg |= R.AR_IMR_TXINTM | R.AR_IMR_TXMINTR
        else:
            imr_reg |= R.AR_IMR_TXOK
        self.enable_write_buffer()
        self.write(R.AR_IMR, imr_reg)
        self.imrs2_reg |= R.AR_IMR_S2_GTT
        self.write(R.AR_IMR_S2, self.imrs2_reg)
        self.write(R.AR_INTR_SYNC_CAUSE, 0xFFFFFFFF)
        self.write(R.AR_INTR_SYNC_ENABLE, R.AR_INTR_SYNC_DEFAULT)
        self.write(R.AR_INTR_SYNC_MASK, 0)
        self.write_flush()

    def ani_cache_ini_regs(self) -> None:
        """ar5008_hw_ani_cache_ini_regs [SRC] ar5008_phy.c:1169 — read the ANI baseline regs the
        INI just programmed so the runtime ANI has reference values. Read-only on the wire."""
        for reg in (R.AR_PHY_SFCORR, R.AR_PHY_SFCORR_LOW, R.AR_PHY_SFCORR_EXT,
                    R.AR_PHY_FIND_SIG, R.AR_PHY_FIND_SIG_LOW, R.AR_PHY_TIMING5, R.AR_PHY_EXT_CCA):
            self.read(reg)

    def init_qos(self) -> None:
        """ath9k_hw_init_qos [SRC] hw.c:715 — the QoS / no-ack / TXOP-limit defaults."""
        self.enable_write_buffer()
        self.write(R.AR_MIC_QOS_CONTROL, 0x100AA)
        self.write(R.AR_MIC_QOS_SELECT, 0x3210)
        self.write(R.AR_QOS_NO_ACK,
                   R.SM(2, R.AR_QOS_NO_ACK_TWO_BIT) | R.SM(5, R.AR_QOS_NO_ACK_BIT_OFF)
                   | R.SM(0, R.AR_QOS_NO_ACK_BYTE_OFF))
        self.write(R.AR_TXOP_X, R.AR_TXOP_X_VAL)
        for reg in (R.AR_TXOP_0_3, R.AR_TXOP_4_7, R.AR_TXOP_8_11, R.AR_TXOP_12_15):
            self.write(reg, 0xFFFFFFFF)
        self.write_flush()

    def _mac_to_clks(self, usecs: int) -> int:
        return usecs * self.clockrate

    def init_global_settings(self, chan) -> None:
        """ath9k_hw_init_global_settings [SRC] hw.c:1051 — SIFS/slot/ACK/CTS/EIFS timing and
        the AR_USEC latencies. 2.4 GHz / 20 MHz path (no half/quarter rate)."""
        if self.misc_mode != 0:
            self.rmw(R.AR_PCU_MISC, self.misc_mode, 0)        # REG_SET_BIT

        sifstime = 10                                          # 2.4 GHz
        eifs = self.read(R.AR_D_GBL_IFS_EIFS) // self.clockrate
        reg = self.read(R.AR_USEC)
        rx_lat = R.MS(reg, R.AR_USEC_RX_LAT)
        tx_lat = R.MS(reg, R.AR_USEC_TX_LAT)
        slottime = self.slottime

        slottime += 3 * self.coverage_class
        acktimeout = slottime + sifstime                       # ack_offset 0 (full rate)
        ctstimeout = acktimeout
        # 2.4 GHz early-ACK workaround.
        acktimeout += 64 - sifstime - self.slottime
        ctstimeout += 48 - sifstime - self.slottime
        if self.dynack_enabled:                                # untested (off at cold boot)
            acktimeout = ctstimeout = self.dynack_ackto
            slottime = (acktimeout - 3) // 2

        self.write(R.AR_D_GBL_IFS_SIFS, min(self._mac_to_clks(sifstime - 2), 0xFFFF))
        self.write(R.AR_D_GBL_IFS_SLOT, min(self._mac_to_clks(slottime), 0xFFFF))
        self.rmw_field(R.AR_TIME_OUT, R.AR_TIME_OUT_ACK,
                       min(self._mac_to_clks(acktimeout), R.MS(0xFFFFFFFF, R.AR_TIME_OUT_ACK)))
        self.rmw_field(R.AR_TIME_OUT, R.AR_TIME_OUT_CTS,
                       min(self._mac_to_clks(ctstimeout), R.MS(0xFFFFFFFF, R.AR_TIME_OUT_CTS)))
        # globaltxtimeout == (u32)-1 -> set_global_txtimeout skipped.
        self.write(R.AR_D_GBL_IFS_EIFS, self._mac_to_clks(eifs))
        self.rmw(R.AR_USEC,
                 (self.clockrate - 1) | R.SM(rx_lat, R.AR_USEC_RX_LAT)
                 | R.SM(tx_lat, R.AR_USEC_TX_LAT),
                 R.AR_USEC_TX_LAT | R.AR_USEC_RX_LAT | R.AR_USEC_USEC)

    def set_dma(self) -> None:
        """ath9k_hw_set_dma [SRC] hw.c:1193 — AHB prefetch, 128-byte TX/RX DMA bursts, the TX
        trigger level, and the RX-FIFO threshold. The 9271 skips the PCU TXBUF-CTRL write."""
        self.enable_write_buffer()
        self.rmw(R.AR_AHB_MODE, R.AR_AHB_PREFETCH_RD_EN, 0)     # not 9300
        self.rmw(R.AR_TXCFG, R.AR_TXCFG_DMASZ_128B, R.AR_TXCFG_DMASZ_MASK)
        self.write_flush()
        self.rmw_field(R.AR_TXCFG, R.AR_FTRIG, self.tx_trig_level)   # not 9300
        self.enable_write_buffer()
        self.rmw(R.AR_RXCFG, R.AR_RXCFG_DMASZ_128B, R.AR_RXCFG_DMASZ_MASK)
        self.write(R.AR_RXFIFO_CFG, 0x200)
        # AR_SREV_9271 -> skip AR_PCU_TXBUF_CTRL.
        self.write_flush()

    def reset_dma_and_intr(self) -> None:
        """The ath9k_hw_reset tail after init_global_settings [SRC] hw.c:2006-2024: preserve the
        sequence number, program DMA, enable the observation bus, and apply RX interrupt
        mitigation (rx_intr_mitigation is on for the 9271)."""
        self.rmw(R.AR_STA_ID1, R.AR_STA_ID1_PRESERVE_SEQNUM, 0)   # REG_SET_BIT
        self.set_dma()
        self.write(R.AR_OBS, 8)                                   # not MCI
        self.enable_rmw_buffer()
        if self.rx_intr_mitigation:
            self.rmw_field(R.AR_RIMT, R.AR_RIMT_LAST, self.rimt_last)
            self.rmw_field(R.AR_RIMT, R.AR_RIMT_FIRST, self.rimt_first)
        if self.tx_intr_mitigation:                              # off on the 9271
            pass
        self.rmw_buffer_flush()

    def reset_tail(self) -> None:
        """ath9k_hw_reset close-out after init_cal [SRC] hw.c:2038-2066: restore_chainmask
        (an ar9003-only private op, absent on ar9002), write the saved LED state OR'd with the
        32 kHz sleep clock, arm the TSF2 gen-timer (disabled here), set the AR9271 USB
        descriptor byte-swap (init_desc), then apply any GPIO override (none on this path)."""
        self.enable_write_buffer()
        # restore_chainmask: ar9002 has no restore_chainmask private op -> nothing buffered.
        self.write(R.AR_CFG_LED, self.saveLedState | R.AR_CFG_SCLK_32KHZ)
        self.write_flush()

        self.gen_timer_start_tsf2()
        self.init_desc()
        self.apply_gpio_override()

    def gen_timer_start_tsf2(self) -> None:
        """ath9k_hw_gen_timer_start_tsf2 [SRC] hw.c:3104 — a no-op until a tsf2 gen-timer is
        allocated (not on the cold bring-up path)."""
        if self.tsf2_enabled:                     # untested — no tsf2 timer here
            self.rmw(R.AR_DIRECT_CONNECT, R.AR_DC_AP_STA_EN, 0)
            self.rmw(R.AR_RESET_TSF, R.AR_RESET_TSF2_ONCE, 0)

    def init_desc(self) -> None:
        """ath9k_hw_init_desc [SRC] hw.c:1749 — USB descriptor byte-swap. The AR9271 target
        wants SWRB|SWTB."""
        self.write(R.AR_CFG, R.AR_CFG_SWRB | R.AR_CFG_SWTB)

    def apply_gpio_override(self) -> None:
        """ath9k_hw_apply_gpio_override [SRC] hw.c:1613 — drive any overridden GPIOs. gpio_mask
        is 0 on the STA bring-up path, so no wire ops are issued."""
        if self.gpio_mask:                        # untested — GPIO override not ported
            raise NotImplementedError("ar9271_v2: GPIO override not ported")

    def init_mfp(self) -> None:
        """ath9k_hw_init_mfp [SRC] hw.c — CCMP management-frame protection. On 9280_20+ mask
        Retry/PwrMgt/MoreData out of the CCMP AAD; the 9271 also does mgmt-crypto TX in sw."""
        if self.is_9280_20_or_later():
            self.rmw_field(R.AR_AES_MUTE_MASK1, R.AR_AES_MUTE_MASK1_FC_MGMT,
                           R.AR_AES_MUTE_MASK1_FC_MGMT_VAL)
            self.sw_mgmt_crypto_tx = self.is_9271()       # AR_DEVID_7010 never matches here
            self.sw_mgmt_crypto_rx = False
        else:                                             # untested (pre-9280 silicon)
            self.sw_mgmt_crypto_tx = True
            self.sw_mgmt_crypto_rx = True


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
