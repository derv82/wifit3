"""ar9002 PHY/analog bring-up over WMI.

Ported from ar9002_hw.c / ar9002_phy.c. Begins with the RF claim that __ath9k_hw_init runs
from ath9k_hw_post_init (not 9300+ path).
"""
from __future__ import annotations

from . import eeprom
from . import initvals as I
from . import reg as R
from .chan import Channel
from .hw import AthHw


def _write_ini_array(hw: AthHw, table: list[list[int]], col: int) -> None:
    """REG_WRITE_ARRAY / the iniModes-iniCommon write loops [SRC] ar5008_phy.c:751-803: one
    buffered batch writing (row[0], row[col]) per row. The 0x7800-0x78a0 analog-shift udelay
    is skipped on USB, so there are no interruptions to the batch."""
    hw.enable_write_buffer()
    for row in table:
        reg, val = row[0], row[col]
        if reg == R.AR_AN_TOP2 and hw.need_an_top2_fixup:   # never fires on 9271 (4k eeprom)
            val &= ~R.AR_AN_TOP2_PWDCLKIND
        hw.write(reg, val)
    hw.write_flush()


def process_ini(hw: AthHw, chan: Channel) -> None:
    """ar5008_hw_process_ini [SRC] ar5008_phy.c:722 — apply the analog-shift prologue then the
    AR9271 init tables. For 2.4 GHz/20 MHz: modesIndex 4, freqIndex 2. The Rx/Tx-gain and
    BB_RfGain arrays aren't on the 9271 path here; iniAddac is empty (the 9271 init-mode-regs
    branch returns before setting it)."""
    modesIndex = 3 if False else 4               # HT40 would be 3; monitor is 20 MHz -> 4
    hw.write(R.AR_PHY(0), 0x00000007)
    hw.write(R.AR_PHY_ADC_SERIAL_CTL, R.AR_PHY_SEL_EXTERNAL_RADIO)
    hw.write(R.AR_PHY_ADC_SERIAL_CTL, R.AR_PHY_SEL_INTERNAL_ADDAC)
    _write_ini_array(hw, I.MODES_9271, modesIndex)       # iniModes (303 rows)

    # iniModesTxGain — AR9271 qualifies via AR_SREV_9285_12_OR_LATER; the table is selected by
    # the eeprom txGainType (ar9271_hw_init_txgain_ini) [SRC] ar9002_hw.c:144 + ar5008_phy.c:776.
    if eeprom.txgain_type(hw) == R.AR5416_EEP_TXGAIN_HIGH_POWER:
        txgain = I.MODES_HIGH_POWER_TX_GAIN_9271
    else:
        txgain = I.MODES_NORMAL_POWER_TX_GAIN_9271
    _write_ini_array(hw, txgain, modesIndex)             # iniModesTxGain (33 rows)

    _write_ini_array(hw, I.COMMON_9271, 1)               # iniCommon (325 rows)


def compute_pll_control(ah: AthHw, chan: Channel | None) -> int:
    """ar9002_hw_compute_pll_control [SRC] ar9002_phy.c — for the AR9271 (2.4 GHz, no fast
    clock / half / quarter rate) this is the constant ref_div=5, pll_div=0x2c -> 0x142c."""
    ref_div = 5
    pll_div = 0x2c
    if chan and chan.is_5ghz():          # untested here (AR9271 is 2.4 GHz only)
        pll_div = 0x28
    pll = R.SM(ref_div, R.AR_RTC_9160_PLL_REFDIV)
    pll |= R.SM(pll_div, R.AR_RTC_9160_PLL_DIV)
    if chan and chan.is_half_rate():
        pll |= R.SM(0x1, R.AR_RTC_9160_PLL_CLKSEL)
    elif chan and chan.is_quarter_rate():
        pll |= R.SM(0x2, R.AR_RTC_9160_PLL_CLKSEL)
    return pll


def reverse_bits(val: int, n: int) -> int:
    """ath9k_hw_reverse_bits [SRC] hw.c:155 — MSB-first bit reversal of the low n bits."""
    retval = 0
    for _ in range(n):
        retval = (retval << 1) | (val & 1)
        val >>= 1
    return retval


def get_radiorev(hw: AthHw) -> int:
    """ar9002_hw_get_radiorev [SRC] ar9002_hw.c:324 — a buffered probe write then read the
    analog rev out of AR_PHY(256)."""
    hw.enable_write_buffer()
    hw.write(R.AR_PHY(0x36), 0x00007058)
    for _ in range(8):
        hw.write(R.AR_PHY(0x20), 0x00010000)
    hw.write_flush()

    val = (hw.read(R.AR_PHY(256)) >> 24) & 0xff
    val = ((val & 0xf0) >> 4) | ((val & 0x0f) << 4)
    return reverse_bits(val, 8)


def rf_claim(hw: AthHw) -> None:
    """ar9002_hw_rf_claim [SRC] ar9002_hw.c:343 — seed AR_PHY(0) and validate the radio rev."""
    hw.write(R.AR_PHY(0), 0x00000007)

    val = get_radiorev(hw)
    major = val & R.AR_RADIO_SREV_MAJOR
    if major == 0:
        val = R.AR_RAD5133_SREV_MAJOR
    elif major in (R.AR_RAD5133_SREV_MAJOR, R.AR_RAD5122_SREV_MAJOR,
                   R.AR_RAD2133_SREV_MAJOR, R.AR_RAD2122_SREV_MAJOR):
        pass
    else:
        raise RuntimeError(f"ar9271_v2: unsupported radio chip rev 0x{major:02x}")
    hw.analog5GhzRev = val
