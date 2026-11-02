"""Post-firmware hardware bring-up for MT7601U.

Ported from driver_sources/mt7601u-source-v7.2/mt7601u/init.c: mt7601u_chip_onoff,
mt7601u_reset_csr_bbp, mt7601u_init_usb_dma, mt7601u_write_mac_initvals,
mt7601u_init_bbp, mt7601u_reset_counters and the tail of mt7601u_init_hardware.

This runs after the firmware blob is resident. Everything here is register and
MCU-register-pair traffic, so it is replay-verified against the cold-boot captures.
"""
from __future__ import annotations

import logging
import time

from .constants import (
    MT_AUX_CLK_CFG,
    MT_BCN_OFFSET,
    MT_BEACON_BASE,
    MT_BEACON_TIME_CFG,
    MT_BEACON_TIME_CFG_BEACON_TX,
    MT_BEACON_TIME_CFG_SYNC_MODE,
    MT_BEACON_TIME_CFG_TBTT_EN,
    MT_BEACON_TIME_CFG_TIMER_EN,
    MT_BW_20,
    MT_CMB_CTRL,
    MT_MAC_CSR0,
    MT_CMB_CTRL_PLL_LD,
    MT_CMB_CTRL_XTAL_RDY,
    MT_MAC_STATUS,
    MT_MAC_STATUS_RX,
    MT_MAC_STATUS_TX,
    MT_MAC_SYS_CTRL,
    MT_MAC_SYS_CTRL_ENABLE_RX,
    MT_MAC_SYS_CTRL_ENABLE_TX,
    MT_MAC_SYS_CTRL_RESET_BBP,
    MT_MAC_SYS_CTRL_RESET_CSR,
    MT_MCU_MEMMAP_BBP,
    MT_MCU_MEMMAP_WLAN,
    MT_PCNT_0438,
    MT_PCNT_0A30,
    MT_PCNT_0A34,
    MT_RX_FILTR_CFG,
    MT_RX_FILTR_CFG_CRC_ERR,
    MT_RX_FILTR_CFG_DUP,
    MT_RX_FILTR_CFG_PHY_ERR,
    MT_RX_FILTR_CFG_RTS,
    MT_RX_FILTR_CFG_VER_ERR,
    MT_RXQ_STA,
    MT_RX_STA_CNT0,
    MT_RX_STA_CNT1,
    MT_RX_STA_CNT2,
    MT_TX_STA_CNT0,
    MT_TX_STA_CNT1,
    MT_TX_STA_CNT2,
    MT_TXOP_CTRL_CFG,
    MT_TXOP_EXT_CCA_DLY,
    MT_TXOP_TRUN_EN,
    MT_US_CYC_CFG,
    MT_US_CYC_CNT,
    MT_USB_DMA_CFG,
    MT_USB_DMA_CFG_RX_BUSY,
    MT_USB_DMA_CFG_TX_BUSY,
    MT_USB_DMA_CFG_RX_BULK_AGG_EN,
    MT_USB_DMA_CFG_RX_BULK_AGG_LMT,
    MT_USB_DMA_CFG_RX_BULK_AGG_TOUT,
    MT_USB_DMA_CFG_RX_BULK_EN,
    MT_USB_DMA_CFG_TX_BULK_EN,
    MT_USB_DMA_CFG_UDMA_RX_WL_DROP,
    MT_WLAN_FUN_CTRL,
    MT_WLAN_FUN_CTRL_FRC_WL_ANT_SEL,
    MT_WLAN_FUN_CTRL_GPIO_OUT_EN,
    MT_WLAN_FUN_CTRL_WLAN_CLK_EN,
    MT_WLAN_FUN_CTRL_WLAN_EN,
    MT_WLAN_FUN_CTRL_WLAN_RESET,
    MT_WLAN_FUN_CTRL_WLAN_RESET_RF,
    MT_WPDMA_GLO_CFG,
    MT_WPDMA_GLO_CFG_RX_DMA_BUSY,
    MT_WPDMA_GLO_CFG_TX_DMA_BUSY,
    _field_prep,
)
from .constants import Q_SELECT
from .mcu import MT7601UMcu
from .phy import MT7601UPhy
from .transport import MT7601UTransport

logger = logging.getLogger(__name__)

MT_USB_AGGR_TIMEOUT = 0x80
"""mt7601u.h:31 -- 0x80 * 33ns of RX bulk aggregation."""
MT_USB_AGGR_SIZE_LIMIT = 28
"""mt7601u.h:30 -- 28 * 1024B."""
USB_DMA_AGG_EN_MAX_PACKET = 512
"""init.c:108 -- aggregation only when the endpoint can carry full 512-byte packets."""

BEACON_OFFSETS = (0xC000, 0xC200, 0xC400, 0xC600, 0xC800, 0xCA00, 0xCC00, 0xCE00,
                  0xD000, 0xD200, 0xD400, 0xD600, 0xD800, 0xDA00, 0xDC00, 0xDE00)
"""init.c:319 -- 512 bytes per beacon slot, as absolute MT_BEACON_BASE offsets."""

RX_FILTER_MONITOR = (MT_RX_FILTR_CFG_CRC_ERR | MT_RX_FILTR_CFG_PHY_ERR
                     | MT_RX_FILTR_CFG_VER_ERR | MT_RX_FILTR_CFG_DUP
                     | MT_RX_FILTR_CFG_RTS)
"""main.c:116-125 applied to the init.c:238-244 default, for a monitor interface.

The register drops on set: main.c:107 sets a bit only when mac80211 did NOT ask for that
class. A monitor asks for OTHER_BSS, CONTROL and PSPOLL and not FCSFAIL or PLCPFAIL, so it
clears PROMISC, the main.c:119 control group and PSPOLL, and keeps CRC_ERR, PHY_ERR,
VER_ERR and DUP. RTS is outside that control group, so it stays set."""

DMA_BUSY = MT_WPDMA_GLO_CFG_TX_DMA_BUSY | MT_WPDMA_GLO_CFG_RX_DMA_BUSY
"""The pair init.c:234, :250 and :339 all poll for."""

MAC_BUSY = MT_MAC_STATUS_TX | MT_MAC_STATUS_RX
"""The pair init.c:364 and phy.c:1196 poll for."""

ASIC_READY_ATTEMPTS = 101          # core.c:11 do/while(i--) from i=100 runs 101 times
"""core.c:11."""

XTAL_POLL_ATTEMPTS = 200
"""init.c:42 -- the loop count; init.c:48 is the 20us it waits between reads."""

QUEUE_DRAIN_PASSES = 200
"""init.c:272 and init.c:286 -- `i = 200` on both teardown drains."""


class BringUpError(RuntimeError):
    """A bring-up step did not reach the state the kernel driver waits for."""

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


class MT7601UInit:
    """The register-level bring-up that runs once the firmware is resident."""

    def __init__(self, tp: MT7601UTransport, mcu: MT7601UMcu, phy: MT7601UPhy) -> None:
        self.tp = tp
        self.mcu = mcu
        self.phy = phy
        self.wlan_running = False

    # ------------------------------------------------------------------

    def chip_onoff(self, enable: bool, reset: bool = False) -> None:
        """init.c:59 -- write MT_WLAN_FUN_CTRL back untouched, then hand that same value to
        set_wlan_state, which is what gates the clock. Two writes, not one."""
        val = self.tp.rr(MT_WLAN_FUN_CTRL)
        if reset:
            val |= MT_WLAN_FUN_CTRL_GPIO_OUT_EN
            val &= ~MT_WLAN_FUN_CTRL_FRC_WL_ANT_SEL & 0xFFFFFFFF
            if val & MT_WLAN_FUN_CTRL_WLAN_EN:
                val |= MT_WLAN_FUN_CTRL_WLAN_RESET | MT_WLAN_FUN_CTRL_WLAN_RESET_RF
                self.tp.wr(MT_WLAN_FUN_CTRL, val)
                time.sleep(0.000020)                      # udelay(20)
                val &= ~(MT_WLAN_FUN_CTRL_WLAN_RESET
                         | MT_WLAN_FUN_CTRL_WLAN_RESET_RF) & 0xFFFFFFFF
        self.tp.wr(MT_WLAN_FUN_CTRL, val)
        time.sleep(0.000020)                              # udelay(20)
        self.set_wlan_state(val, enable)

    def set_wlan_state(self, val: int, enable: bool) -> None:
        """init.c:16 -- gate the WLAN clock and wait for the crystal and PLL to lock.

        WLAN_CLK stays on even when disabling: init.c:20-24 notes that turning it off
        stops the chip answering on the probe path.
        """
        if enable:
            val |= MT_WLAN_FUN_CTRL_WLAN_EN | MT_WLAN_FUN_CTRL_WLAN_CLK_EN
        else:
            val &= ~MT_WLAN_FUN_CTRL_WLAN_EN & 0xFFFFFFFF
        self.tp.wr(MT_WLAN_FUN_CTRL, val)
        time.sleep(0.000020)                              # udelay(20)

        self.wlan_running = enable
        if not enable:
            return

        for _ in range(XTAL_POLL_ATTEMPTS):
            val = self.tp.rr(MT_CMB_CTRL)
            if val & MT_CMB_CTRL_XTAL_RDY and val & MT_CMB_CTRL_PLL_LD:
                return
            time.sleep(0.000020)                          # udelay(20)
        # init.c:55 logs and carries on -- a slow PLL is not a bring-up failure upstream.
        logger.error("Error: PLL and XTAL check failed!")

    def reset_csr_bbp(self) -> None:
        """init.c:90 -- pulse RESET_CSR|RESET_BBP with USB DMA off in between."""
        self.tp.wr(MT_MAC_SYS_CTRL, MT_MAC_SYS_CTRL_RESET_CSR | MT_MAC_SYS_CTRL_RESET_BBP)
        self.tp.wr(MT_USB_DMA_CFG, 0)
        time.sleep(0.001)                                   # msleep(1)
        self.tp.wr(MT_MAC_SYS_CTRL, 0)

    def init_usb_dma(self) -> None:
        """init.c:99 -- program RX aggregation, then pulse the wireless-drop bit."""
        val = (_field_prep(MT_USB_DMA_CFG_RX_BULK_AGG_TOUT, MT_USB_AGGR_TIMEOUT)
               | _field_prep(MT_USB_DMA_CFG_RX_BULK_AGG_LMT, MT_USB_AGGR_SIZE_LIMIT)
               | MT_USB_DMA_CFG_RX_BULK_EN | MT_USB_DMA_CFG_TX_BULK_EN)
        if self.tp.in_max_packet == USB_DMA_AGG_EN_MAX_PACKET:
            val |= MT_USB_DMA_CFG_RX_BULK_AGG_EN
        self.tp.wr(MT_USB_DMA_CFG, val)

        val |= MT_USB_DMA_CFG_UDMA_RX_WL_DROP
        self.tp.wr(MT_USB_DMA_CFG, val)
        val &= ~MT_USB_DMA_CFG_UDMA_RX_WL_DROP
        self.tp.wr(MT_USB_DMA_CFG, val)

    def wait_asic_ready(self) -> None:
        """core.c:9 mt7601u_wait_asic_ready -- MT_MAC_CSR0 must settle to a non-trivial
        value. Zero means the ASIC has not finished resetting; proceeding would program
        registers the silicon has not started honouring."""
        for _ in range(ASIC_READY_ATTEMPTS):
            val = self.tp.rr(MT_MAC_CSR0)
            # core.c:19 complements a u32, so an all-ones read fails the test. Python's ~ is
            # arbitrary-precision and would make 0xffffffff pass, accepting a dead card.
            if val and ~val & 0xFFFFFFFF:
                return
            time.sleep(0.000010)                          # udelay(10)
        raise BringUpError("asic_ready", "MT_MAC_CSR0 never settled")

    def mcu_cmd_init(self) -> None:
        """mcu.c mt7601u_mcu_cmd_init -- put the MCU into command mode.

        Without this the firmware is running but is not accepting commands, so
        every later MCU write is dropped and the RX DMA never starts.
        """
        self.mcu.function_select(Q_SELECT, 1)

    def write_mac_initvals(self) -> None:
        """init.c:152 -- the WLAN register table, the beacon offsets and the aux clock."""
        from .initvals_mac import mac_chip_vals, mac_common_vals

        self.mcu.write_reg_pairs(MT_MCU_MEMMAP_WLAN, mac_common_vals)
        self.mcu.write_reg_pairs(MT_MCU_MEMMAP_WLAN, mac_chip_vals)
        self.init_beacon_offsets()
        self.tp.wr(MT_AUX_CLK_CFG, 0)

    def init_beacon_offsets(self) -> None:
        """init.c:136 -- pack 16 beacon slots into four 8-bit-per-slot registers.

        The slot stride is 512 bytes and the registers are expressed in 64-byte units.
        """
        regs = [0, 0, 0, 0]
        for i, addr in enumerate(BEACON_OFFSETS):
            regs[i // 4] |= ((addr - MT_BEACON_BASE) // 64) << (8 * (i % 4))
        for i, reg in enumerate(regs):
            self.tp.wr(MT_BCN_OFFSET(i), reg)

    def init_bbp(self) -> None:
        """init.c:118 -- wait for the BBP, then load the common and chip tables."""
        from .initvals_mac import bbp_chip_vals, bbp_common_vals

        self.phy.wait_bbp_ready()
        self.mcu.write_reg_pairs(MT_MCU_MEMMAP_BBP, bbp_common_vals)
        self.mcu.write_reg_pairs(MT_MCU_MEMMAP_BBP, bbp_chip_vals)

    def reset_counters(self) -> None:
        """init.c:220 -- reading the per-station counters clears them."""
        for reg in (MT_RX_STA_CNT0, MT_RX_STA_CNT1, MT_RX_STA_CNT2,
                    MT_TX_STA_CNT0, MT_TX_STA_CNT1, MT_TX_STA_CNT2):
            self.tp.rr(reg)

    def mac_start(self) -> None:
        """init.c:230 mt7601u_mac_start -- enable TX, install the monitor filter, enable RX."""
        self.tp.wr(MT_MAC_SYS_CTRL, MT_MAC_SYS_CTRL_ENABLE_TX)
        if not self.poll(MT_WPDMA_GLO_CFG, DMA_BUSY, 0, 200_000):        # init.c:234
            raise BringUpError("mac_start", "DMA still busy before the filter write")
        self.tp.wr(MT_RX_FILTR_CFG, RX_FILTER_MONITOR)
        self.tp.wr(MT_MAC_SYS_CTRL, MT_MAC_SYS_CTRL_ENABLE_TX | MT_MAC_SYS_CTRL_ENABLE_RX)
        if not self.poll(MT_WPDMA_GLO_CFG, DMA_BUSY, 0, 50):             # init.c:250
            raise BringUpError("mac_start", "DMA still busy after RX was enabled")

    def mac_stop_hw(self) -> None:
        """init.c:257 mt7601u_mac_stop_hw -- stop beacons, drain both queues, stop the MAC.

        Every poll here only warns in the C: a queue that will not settle does not abort
        the teardown, because there is nothing left to abort.
        """
        beacon_timers = (MT_BEACON_TIME_CFG_TIMER_EN | MT_BEACON_TIME_CFG_SYNC_MODE
                         | MT_BEACON_TIME_CFG_TBTT_EN | MT_BEACON_TIME_CFG_BEACON_TX)
        self.tp.rmw(MT_BEACON_TIME_CFG, beacon_timers, 0)                   # init.c:262
        if not self.poll(MT_USB_DMA_CFG, MT_USB_DMA_CFG_TX_BUSY, 0, 1000):
            logger.warning("Warning: TX DMA did not stop!")                 # init.c:269
        self._drain_tx_page_counts()
        if not self.poll(MT_MAC_STATUS, MT_MAC_STATUS_TX, 0, 1000):
            logger.warning("Warning: MAC TX did not stop!")                 # init.c:279
        self.tp.rmw(MT_MAC_SYS_CTRL,
                    MT_MAC_SYS_CTRL_ENABLE_RX | MT_MAC_SYS_CTRL_ENABLE_TX, 0)
        self._drain_rx_page_counts()
        if not self.poll(MT_MAC_STATUS, MT_MAC_STATUS_RX, 0, 1000):
            logger.warning("Warning: MAC RX did not stop!")                 # init.c:299
        if not self.poll(MT_USB_DMA_CFG, MT_USB_DMA_CFG_RX_BUSY, 0, 1000):
            logger.warning("Warning: RX DMA did not stop!")                 # init.c:302

    def _drain_tx_page_counts(self) -> None:
        """init.c:272-276 -- up to 200 passes while any TxQ page count is still set."""
        for _ in range(QUEUE_DRAIN_PASSES):
            if not (self.tp.rr(MT_PCNT_0438) & 0xFFFFFFFF
                    or self.tp.rr(MT_PCNT_0A30) & 0x000000FF
                    or self.tp.rr(MT_PCNT_0A34) & 0x00FF00FF):
                return
            time.sleep(0.010)                                               # msleep(10)

    def _drain_rx_page_counts(self) -> None:
        """init.c:285-296 -- 200 passes, needing seven consecutive clean reads to finish.

        init.c:293's clean branch takes `continue` without sleeping, so a queue that is
        already settled exits in seven back-to-back passes, not seven milliseconds.
        """
        clean = 0
        for _ in range(QUEUE_DRAIN_PASSES):
            if (not self.tp.rr(MT_RXQ_STA) & 0x00FF0000
                    and not self.tp.rr(MT_PCNT_0A30)
                    and not self.tp.rr(MT_PCNT_0A34)):
                if clean > 5:                                               # init.c:292
                    return
                clean += 1
                continue
            time.sleep(0.001)                                               # msleep(1)

    def poll(self, reg: int, mask: int, val: int, timeout_us: int) -> bool:
        """core.c:28 mt76_poll -- reads timeout/10 + 1 times, with no added delay.

        core.c:42's udelay(10) has no counterpart here: one vendor-request read is a USB
        round-trip of at least a USB frame, so the gap the C asks for has already elapsed
        by the time the next read starts. Sleeping for it would also overshoot -- Windows
        cannot deliver a 10us time.sleep, and at the 20001 iterations init.c:234 budgets
        its ~0.5ms floor turns a 200ms poll into ten seconds.
        """
        return self._poll(reg, mask, val, timeout_us, 0.0)

    def poll_msec(self, reg: int, mask: int, val: int, timeout_ms: int) -> bool:
        """core.c:50 mt76_poll_msec -- the same read count, msleep(10) apart.

        10ms is well past the transfer cost, so unlike poll() this delay is real.
        """
        return self._poll(reg, mask, val, timeout_ms, 0.010)

    def _poll(self, reg: int, mask: int, val: int, timeout: int, delay: float) -> bool:
        # core.c:33 divides the timeout by 10 and core.c:43 tests the counter after the
        # read, so the loop reads timeout/10 + 1 times. The count is the contract: reads
        # on this chip are not side-effect-free.
        for _ in range(timeout // 10 + 1):
            if self.tp.rr(reg) & mask == val:
                return True
            if delay:
                time.sleep(delay)
        return False

    def poll_dma_idle(self, timeout_ms: int) -> None:
        """init.c:339 -- both DMA engines idle before the MAC initvals go in."""
        if not self.poll_msec(MT_WPDMA_GLO_CFG, DMA_BUSY, 0, timeout_ms):
            raise BringUpError("wpdma", f"DMA still busy after {timeout_ms}ms")

    def poll_mac_idle(self) -> None:
        """init.c:364 -- wait for MAC_STATUS TX and RX to clear before loading BBP."""
        if not self.poll_msec(MT_MAC_STATUS, MAC_BUSY, 0, 100):
            raise BringUpError("mac_status", "MAC_STATUS TX|RX never cleared")

    def pre_phy_finalise(self) -> None:
        """init.c:383-394 -- the register work that precedes eeprom_init and phy_init."""
        # init.c:375 uses mt76_clear, which always writes the register. rmc would
        # skip it when the bits already read zero, and the kernel's capture shows
        # the write happening, so write it unconditionally.
        mask = (MT_BEACON_TIME_CFG_TIMER_EN | MT_BEACON_TIME_CFG_SYNC_MODE
                | MT_BEACON_TIME_CFG_TBTT_EN | MT_BEACON_TIME_CFG_BEACON_TX)
        self.tp.wr(MT_BEACON_TIME_CFG, self.tp.rr(MT_BEACON_TIME_CFG) & ~mask & 0xFFFFFFFF)
        self.reset_counters()
        self.tp.rmw(MT_US_CYC_CFG, MT_US_CYC_CNT, 0x1E)
        self.tp.wr(MT_TXOP_CTRL_CFG,
                   _field_prep(MT_TXOP_TRUN_EN, 0x3F)
                   | _field_prep(MT_TXOP_EXT_CCA_DLY, 0x58))

    def finalise(self) -> None:
        """init.c:404-409 -- the post-PHY steps, in the kernel's order."""
        self.phy.set_rx_path(0)
        self.phy.set_tx_dac(0)
        # init.c:407-408 is MAC then BBP; phy.c:401-402 orders the same pair the
        # other way round, so neither call site can share a wrapper.
        self.phy.mac_set_ctrlch(False)
        self.phy.bbp_set_ctrlch(False)
        self.phy.bbp_set_bw(MT_BW_20)
