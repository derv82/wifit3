"""RF/BBP access and the channel tune for MT7601U.

Ported from driver_sources/mt7601u-source-v7.2/mt7601u/phy.c (tag v7.2).

Two indirect register windows sit on top of the plain register file:
`MT_RF_CSR_CFG` reaches RF registers via a (value, bank, offset) descriptor, and
`MT_BBP_CSR_CFG` does the same for the BBP. Both poll their BUSY/KICK bit before
and after. `__mt7601u_phy_set_channel` is the channel tune -- Wifit3's hot path --
and is verified op-for-op against a monitor-mode capture.
"""
from __future__ import annotations

import logging
import time

from .constants import (
    MCU_CAL_BW,
    MT_BBP_CSR_CFG,
    MT_BBP_CSR_CFG_BUSY,
    MT_BBP_CSR_CFG_READ,
    MT_BBP_CSR_CFG_REG_NUM,
    MT_BBP_CSR_CFG_RW_MODE,
    MT_BBP_CSR_CFG_VAL,
    MT_BBP_REG_VERSION,
    MT_BW_20,
    MT_BW_40,
    MT_MCU_MEMMAP_BBP,
    MT_MCU_MEMMAP_RF,
    MT_RF_CSR_CFG,
    MT_RF_CSR_CFG_DATA,
    MT_RF_CSR_CFG_KICK,
    MT_RF_CSR_CFG_REG_BANK,
    MT_RF_CSR_CFG_REG_ID,
    MT_RF_CSR_CFG_WR,
    MT_TEMP_MODE_NORMAL,
    MT_TX_ALC_CFG_0,
    MT_TX_BAND_CFG,
    MT_TX_BAND_CFG_UPPER_40M,
    MT_TX_PWR_CFG_0,
    _field_get,
    _field_prep,
)
from .eeprom import int_to_s6
from .mcu import MT7601UMcu, McuTimeout
from .transport import MT7601UTransport

logger = logging.getLogger(__name__)

FREQ_PLAN_REGS = 4
"""phy.c -- one frequency-plan row is four RF registers."""
FREQ_PLAN_BASE_REG = 17
"""phy.c channel_freq_plan[] = { 17, 18, 19, 20 }."""

# freq_plan[chan_idx][0..3], phy.c. chan_idx = channel - 1.
FREQ_PLAN: list[tuple[int, int, int, int]] = [
    (0x99, 0x99, 0x09, 0x50), (0x46, 0x44, 0x0A, 0x50), (0xEC, 0xEE, 0x0A, 0x50),
    (0x99, 0x99, 0x0B, 0x50), (0x46, 0x44, 0x08, 0x51), (0xEC, 0xEE, 0x08, 0x51),
    (0x99, 0x99, 0x09, 0x51), (0x46, 0x44, 0x0A, 0x51), (0xEC, 0xEE, 0x0A, 0x51),
    (0x99, 0x99, 0x0B, 0x51), (0x46, 0x44, 0x08, 0x52), (0xEC, 0xEE, 0x08, 0x52),
    (0x99, 0x99, 0x09, 0x52), (0x33, 0x33, 0x0B, 0x52),
]

BBP_LNA_REGS = (62, 63, 64)
"""phy.c bbp_settings[]; value is 0x37 - lna_gain."""

CH14_HW_CHAN = 14
BBP_CH14_REG = 178          # phy.c apply_ch14_fixup
BBP_CH14_OBW_VALUE = 0x60

VCO_CAL_SETTLE_S = 0.002     # phy.c vco_cal msleep(2)

_POLL_TIMEOUT_US = 1000     # BBP/RF busy polls
_POLL_STEP_S = 0.000010


def _poll(tp, offset: int, mask: int, want: int, timeout_us: int = _POLL_TIMEOUT_US) -> bool:
    """mt76_poll(dev, offset, mask, want, timeout_us) with udelay(10) steps."""
    budget = timeout_us // 10
    while True:
        if tp.rr(offset) & mask == want:
            return True
        if budget <= 0:
            return False
        budget -= 1
        time.sleep(_POLL_STEP_S)


class MT7601UPhy:
    """RF and BBP register windows plus the channel tune."""

    def __init__(self, tp: MT7601UTransport, mcu: MT7601UMcu, ee):
        self.tp = tp
        self.mcu = mcu
        self.ee = ee                       # MT7601UEepromParams, from eeprom.py
        self.bw = MT_BW_20
        self.chan_ext_below = False
        self.rf_pa_mode = [0, 0]
        self.agc_saved: int | None = None
        # Calibration state; phy.c keeps these on the device struct.
        self.raw_temp = 0
        self.curr_temp = 0
        self.dpd_temp = 0
        # phy.c:306 memoises against a kzalloc'd dev->temp_mode, i.e.
        # MT_TEMP_MODE_NORMAL. A -1 sentinel here writes the normal-temperature BBP
        # table at boot, which upstream skips.
        self.temp_mode = MT_TEMP_MODE_NORMAL
        self.pll_lock_protect = False

    # ------------------------------------------------------------------
    # RF window (phy.c:18 mt7601u_rf_wr)
    # ------------------------------------------------------------------

    def rf_wr(self, bank: int, offset: int, value: int) -> None:
        if offset > 63:
            raise ValueError(f"RF offset {offset} out of range")
        if not _poll(self.tp, MT_RF_CSR_CFG, MT_RF_CSR_CFG_KICK, 0, 100):
            raise McuTimeout(f"RF write {bank}:{offset:#04x} timed out")
        self.tp.wr(MT_RF_CSR_CFG,
                   _field_prep(MT_RF_CSR_CFG_DATA, value)
                   | _field_prep(MT_RF_CSR_CFG_REG_BANK, bank)
                   | _field_prep(MT_RF_CSR_CFG_REG_ID, offset)
                   | MT_RF_CSR_CFG_WR | MT_RF_CSR_CFG_KICK)

    def rf_rr(self, bank: int, offset: int) -> int:
        """phy.c:53 mt7601u_rf_rr -- reads go through the RF window, not a plain
        register read: a KICK-only write asks the RF block, then the value is
        polled back out of MT_RF_CSR_CFG."""
        if offset > 63:
            raise ValueError(f"RF offset {offset} out of range")
        if not _poll(self.tp, MT_RF_CSR_CFG, MT_RF_CSR_CFG_KICK, 0, 100):
            raise McuTimeout(f"RF read {bank}:{offset:#04x} timed out")
        self.tp.wr(MT_RF_CSR_CFG,
                   _field_prep(MT_RF_CSR_CFG_REG_BANK, bank)
                   | _field_prep(MT_RF_CSR_CFG_REG_ID, offset)
                   | MT_RF_CSR_CFG_KICK)
        if not _poll(self.tp, MT_RF_CSR_CFG, MT_RF_CSR_CFG_KICK, 0, 100):
            raise McuTimeout(f"RF read {bank}:{offset:#04x} timed out")
        val = self.tp.rr(MT_RF_CSR_CFG)
        if (_field_get(MT_RF_CSR_CFG_REG_ID, val) == offset
                and _field_get(MT_RF_CSR_CFG_REG_BANK, val) == bank):
            return _field_get(MT_RF_CSR_CFG_DATA, val)
        raise McuTimeout(f"RF read {bank}:{offset:#04x} returned a different register")

    def rf_rmw(self, bank: int, offset: int, mask: int, val: int) -> int:
        """phy.c:94 -- read via the RF window, then write."""
        val |= self.rf_rr(bank, offset) & ~mask & 0xFF
        self.rf_wr(bank, offset, val)
        return val

    def rf_set(self, bank: int, offset: int, val: int) -> int:
        """phy.c:111 -- rf_set is rf_rmw with mask 0."""
        return self.rf_rmw(bank, offset, 0, val)

    def rf_clear(self, bank: int, offset: int, mask: int) -> int:
        """phy.c:116."""
        return self.rf_rmw(bank, offset, mask, 0)

    # ------------------------------------------------------------------
    # BBP window (phy.c:112 mt7601u_bbp_wr, phy.c:144 mt7601u_bbp_rr)
    # ------------------------------------------------------------------

    def bbp_wr(self, offset: int, val: int) -> None:
        if not _poll(self.tp, MT_BBP_CSR_CFG, MT_BBP_CSR_CFG_BUSY, 0):
            raise McuTimeout(f"BBP write {offset:#04x} timed out")
        self.tp.wr(MT_BBP_CSR_CFG,
                   _field_prep(MT_BBP_CSR_CFG_VAL, val)
                   | _field_prep(MT_BBP_CSR_CFG_REG_NUM, offset)
                   | MT_BBP_CSR_CFG_RW_MODE | MT_BBP_CSR_CFG_BUSY)

    def bbp_rr(self, offset: int) -> int:
        if not _poll(self.tp, MT_BBP_CSR_CFG, MT_BBP_CSR_CFG_BUSY, 0):
            raise McuTimeout(f"BBP read {offset:#04x} timed out")
        self.tp.wr(MT_BBP_CSR_CFG,
                   _field_prep(MT_BBP_CSR_CFG_REG_NUM, offset)
                   | MT_BBP_CSR_CFG_RW_MODE | MT_BBP_CSR_CFG_BUSY
                   | MT_BBP_CSR_CFG_READ)
        if not _poll(self.tp, MT_BBP_CSR_CFG, MT_BBP_CSR_CFG_BUSY, 0):
            raise McuTimeout(f"BBP read {offset:#04x} timed out")
        val = self.tp.rr(MT_BBP_CSR_CFG)
        if _field_get(MT_BBP_CSR_CFG_REG_NUM, val) == offset:
            return _field_get(MT_BBP_CSR_CFG_VAL, val)
        raise McuTimeout(f"BBP read {offset:#04x} returned a different register")

    def bbp_rmw(self, offset: int, mask: int, val: int) -> int:
        val |= self.bbp_rr(offset) & ~mask & 0xFF
        self.bbp_wr(offset, val)
        return val

    def bbp_rmc(self, offset: int, mask: int, val: int) -> int:
        """phy.c:195 -- skips the write when nothing changes."""
        cur = self.bbp_rr(offset)
        val |= cur & ~mask & 0xFF
        if cur != val:
            self.bbp_wr(offset, val)
        return val

    def wait_bbp_ready(self) -> None:
        """phy.c:209 -- poll MT_BBP_REG_VERSION until it reads sane."""
        for _ in range(20):
            val = self.bbp_rr(MT_BBP_REG_VERSION)
            if val and val != 0xFF:
                return
        raise McuTimeout("BBP is not ready")

    # ------------------------------------------------------------------
    # Tune helpers
    # ------------------------------------------------------------------

    def vco_cal(self) -> None:
        """phy.c:263."""
        self.rf_wr(0, 4, 0x0A)
        self.rf_wr(0, 5, 0x20)
        self.rf_set(0, 4, 1 << 7)
        time.sleep(VCO_CAL_SETTLE_S)

    def set_bw_filter(self, cal: bool) -> None:
        """phy.c:271 -- two CMD_CALIBRATION_OP, TX then RX."""
        filt = 0
        if not cal:
            filt |= 0x10000
        if self.bw != MT_BW_20:
            filt |= 0x00100
        self.mcu.calibrate(MCU_CAL_BW, filt | 1)
        self.mcu.calibrate(MCU_CAL_BW, filt)

    def bbp_set_bw(self, bw: int) -> None:
        """phy.c:1180 -- the unchanged-bandwidth case, which is what a 20 MHz tune takes."""
        self.bbp_rmc(4, 0x18, 0 if bw == MT_BW_20 else 0x10)

    def bbp_set_ctrlch(self, below: bool) -> None:
        """phy.c:228."""
        self.bbp_rmc(3, 0x20, 0x20 if below else 0)

    def apply_ch14_fixup(self, hw_chan: int) -> None:
        """phy.c:322 -- narrow-bandwidth boost on channel 14 only."""
        if hw_chan != CH14_HW_CHAN or self.bw != MT_BW_20:
            self.bbp_rmw(4, 0x20, 0)
            self.bbp_wr(BBP_CH14_REG, 0xFF)
            self.ee.power_rate_table.cck[0].bw20 = self.ee.real_cck_bw20[0]
            self.ee.power_rate_table.cck[1].bw20 = self.ee.real_cck_bw20[1]
        else:
            self.bbp_wr(4, BBP_CH14_OBW_VALUE)
            self.bbp_wr(BBP_CH14_REG, 0)
            # Vendor code is buggy for negative values (phy.c:300).
            self.ee.power_rate_table.cck[0].bw20 = self.ee.real_cck_bw20[0] - 2
            self.ee.power_rate_table.cck[1].bw20 = self.ee.real_cck_bw20[1] - 2

    def mac_set_ctrlch(self, below: bool) -> None:
        """mt7601u.h:382."""
        self.tp.rmc(MT_TX_BAND_CFG, MT_TX_BAND_CFG_UPPER_40M, int(below))

    # ------------------------------------------------------------------
    # The channel tune (phy.c __mt7601u_phy_set_channel)
    # ------------------------------------------------------------------

    def set_channel(self, channel: int, bw: int = MT_BW_20,
                    scan: bool = False) -> None:
        """Tune to ``channel``. ``bw`` is MT_BW_20 or MT_BW_40.

        40 MHz carries the same code path with the HT40 channel arithmetic from
        phy.c:353-362; it is unreachable from the 20 MHz scans Wifit3 drives.
        """
        if not 1 <= channel <= 14:
            raise ValueError(f"MT7601U has no channel {channel}")
        chan_idx = channel - 1
        below = bw != MT_BW_20           # chan_ext_below; only HT40MINUS sets it

        if bw == MT_BW_40:
            # TODO: verify, untested here, needs HT40 hardware -- the HT40PLUS /
            # HT40MINUS centre-channel arithmetic at phy.c:353-362. 20 MHz is the
            # only width Wifit3 tunes, so this arithmetic is unexercised.
            if chan_idx > 1 and below:
                chan_idx -= 2
            elif chan_idx < 12:
                chan_idx += 2
            else:
                logger.error("Error: invalid 40MHz channel!!")

        if bw != self.bw or below != self.chan_ext_below:
            self.bbp_set_bw(bw)
            self.bbp_set_ctrlch(below)
            self.mac_set_ctrlch(below)
            self.chan_ext_below = below
        self.bw = bw

        plan = FREQ_PLAN[chan_idx]
        self.mcu.write_reg_pairs(
            MT_MCU_MEMMAP_RF,
            [(FREQ_PLAN_BASE_REG + i, plan[i]) for i in range(FREQ_PLAN_REGS)])

        self.tp.rmw(MT_TX_ALC_CFG_0, 0x3F3F, self.ee.chan_pwr[chan_idx] & 0x3F)

        gain = 0x37 - self.ee.lna_gain
        self.mcu.write_reg_pairs(MT_MCU_MEMMAP_BBP, [(r, gain) for r in BBP_LNA_REGS])

        self.vco_cal()
        self.bbp_set_bw(bw)
        self.set_bw_filter(cal=False)

        self.apply_ch14_fixup(channel)
        self._pack_tx_pwr_cfg()

        if scan:                                    # phy.c:434
            self.agc_reset()

    def agc_default(self) -> int:
        """phy.c:948 -- the AGC floor the EEPROM's LNA gain implies, as a u8."""
        return ((self.ee.lna_gain - 8) * 2 + 0x34) & 0xFF

    def agc_reset(self) -> None:
        """phy.c:953 -- put BBP 66 back to that default.

        The MAC initvals leave BBP 66 at a literal (initvals.h:34), which is not the
        value either dongle's LNA gain computes to.
        """
        self.bbp_wr(66, self.agc_default())

    def agc_save(self) -> None:
        """phy.c:960 -- remember BBP 66 before a scan moves it.

        main.c:271 runs this once at sw_scan_start, so a second hop must not
        overwrite the saved value with the scanning one.
        """
        if self.agc_saved is None:
            self.agc_saved = self.bbp_rr(66)

    def agc_restore(self) -> None:
        """phy.c:965 -- put the pre-scan value back (main.c:281)."""
        if self.agc_saved is not None:
            self.bbp_wr(66, self.agc_saved)
            self.agc_saved = None

    def _pack_tx_pwr_cfg(self) -> None:
        """phy.c:432 -- the four per-rate s6 values into one register."""
        t = self.ee.power_rate_table
        self.tp.wr(MT_TX_PWR_CFG_0,
                   ((int_to_s6(t.ofdm[1].bw20) << 24) | (int_to_s6(t.ofdm[0].bw20) << 16)
                    | (int_to_s6(t.cck[1].bw20) << 8) | int_to_s6(t.cck[0].bw20)) & 0xFFFFFFFF)

    # ------------------------------------------------------------------
    # PHY init (phy.c mt7601u_phy_init)
    # ------------------------------------------------------------------

    def set_rx_path(self, path: int) -> None:
        """phy.c mt7601u_set_rx_path -- the receive path in BBP register 3."""
        self.bbp_rmw(3, 0x18, (path & 0x3) << 3)

    def set_tx_dac(self, dac: int) -> None:
        """phy.c mt7601u_set_tx_dac -- which TX DAC to use, BBP register 1."""
        self.bbp_rmc(1, 0x18, (dac & 0x3) << 3)

    def phy_init(self) -> None:
        """phy.c:1103 -- read the PA modes, write the RF tables, arm calibration."""
        from .constants import MT_RF_PA_MODE_CFG0, MT_RF_PA_MODE_CFG1
        from .initvals_phy import rf_central, rf_channel, rf_vga

        self.rf_pa_mode[0] = self.tp.rr(MT_RF_PA_MODE_CFG0)
        self.rf_pa_mode[1] = self.tp.rr(MT_RF_PA_MODE_CFG1)

        self.rf_wr(0, 12, self.ee.rf_freq_off)
        self.mcu.write_reg_pairs(0, rf_central)
        self.mcu.write_reg_pairs(0, rf_channel)
        self.mcu.write_reg_pairs(0, rf_vga)
        from .cal import init_cal
        init_cal(self)
