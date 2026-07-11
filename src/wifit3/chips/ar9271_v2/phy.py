"""ar9002 PHY/analog bring-up over WMI.

Ported from ar9002_hw.c / ar9002_phy.c. Begins with the RF claim that __ath9k_hw_init runs
from ath9k_hw_post_init (not 9300+ path).
"""
from __future__ import annotations

from . import eeprom
from . import initvals as I
from . import phy_power
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

    override_ini(hw, chan)
    set_channel_regs(hw, chan)
    init_chain_masks(hw)
    # ath9k_olc_init is a no-op on the 9271 (4k eeprom has no EEP_OL_PWRCTRL -> OLC disabled).
    phy_power.apply_txpower(hw, chan)


def set_rfmode(hw: AthHw, chan: Channel) -> None:
    """ar5008_hw_set_rfmode [SRC] ar5008_phy.c:826 — for 2.4 GHz on 9280_20+ this is just
    AR_PHY_MODE_DYNAMIC (the RF2GHZ band bit is only set on pre-9280 silicon)."""
    rf_mode = R.AR_PHY_MODE_DYNAMIC if chan.is_2ghz() else R.AR_PHY_MODE_OFDM
    if not hw.is_9280_20_or_later():                      # never on the 9271
        rf_mode |= R.AR_PHY_MODE_RF2GHZ if chan.is_2ghz() else 0
    hw.write(R.AR_PHY_MODE, rf_mode)


def _delta_slope_vals(coef_scaled: int) -> tuple[int, int]:
    """ath9k_hw_get_delta_slope_vals [SRC] hw.c — split a scaled coefficient into the
    mantissa/exponent the AR_PHY_TIMING3 / AR_PHY_HALFGI fields carry."""
    for coef_exp in range(31, 0, -1):
        if (coef_scaled >> coef_exp) & 0x1:
            break
    coef_exp = 14 - (coef_exp - R.COEF_SCALE_S)
    coef_man = coef_scaled + (1 << (R.COEF_SCALE_S - coef_exp - 1))
    mantissa = coef_man >> (R.COEF_SCALE_S - coef_exp)
    exponent = (coef_exp - 16) & 0xffffffff             # u32 wrap (field is masked on write)
    return mantissa, exponent


def set_delta_slope(hw: AthHw, chan: Channel) -> None:
    """ar5008_hw_set_delta_slope [SRC] ar5008_phy.c:850 — program the OFDM clock delta-slope
    (full-rate and the 0.9x half-GI variant) from the channel's synth centre."""
    clock_scaled = 0x64000000                            # 20 MHz (no half/quarter on 9271)
    coef_scaled = clock_scaled // chan.center_freq       # centers.synth_center (20 MHz)
    man, exp = _delta_slope_vals(coef_scaled)
    hw.rmw_field(R.AR_PHY_TIMING3, R.AR_PHY_TIMING3_DSC_MAN, man)
    hw.rmw_field(R.AR_PHY_TIMING3, R.AR_PHY_TIMING3_DSC_EXP, exp)
    coef_scaled = (9 * coef_scaled) // 10
    man, exp = _delta_slope_vals(coef_scaled)
    hw.rmw_field(R.AR_PHY_HALFGI, R.AR_PHY_HALFGI_DSC_MAN, man)
    hw.rmw_field(R.AR_PHY_HALFGI, R.AR_PHY_HALFGI_DSC_EXP, exp)


def spur_mitigate(hw: AthHw, chan: Channel) -> None:
    """ar9002_hw_spur_mitigate [SRC] ar9002_phy.c — find an in-band baseband spur from the
    EEPROM spur table; this card's first 2 GHz spur is AR_NO_SPUR, so the path is the single
    REG_CLR_BIT(AR_PHY_FORCE_CLKEN_CCK, MRC_MUX) and return. The spur-programming branch is
    ported behind its guard but not exercised here."""
    from .eeprom_4k import Map4k
    eep = Map4k(hw.eeprom)
    freq = chan.center_freq
    bb_spur = R.AR_NO_SPUR
    for i in range(5):                                   # AR_EEPROM_MODAL_SPURS
        cur = eep.get_spur_channel(i)
        if cur == R.AR_NO_SPUR:
            break
        cur = (cur // 10) + R.AR_BASE_FREQ_2GHZ - freq
        if -R.AR_SPUR_FEEQ_BOUND_HT20 < cur < R.AR_SPUR_FEEQ_BOUND_HT20:
            bb_spur = cur
            break
    hw.rmw(R.AR_PHY_FORCE_CLKEN_CCK, 0, R.AR_PHY_FORCE_CLKEN_CCK_MRC_MUX)   # REG_CLR_BIT
    if bb_spur != R.AR_NO_SPUR:
        # The notch-filter programming (ar9002_hw_spur_mitigate tail + ar5008_hw_cmn_spur_mitigate,
        # ~24 further PHY regs) is unported: the reference card's spurChans[0] is AR_NO_SPUR, as is
        # every AR9271 4k EEPROM seen, so this path has no wire to verify against. Documented residual
        # in AR9271_V2.md; a card with a populated 2 GHz spur table would need it ported.
        raise NotImplementedError(
            f"ar9271_v2: untested variant: in-band 2 GHz spur (bb_spur={bb_spur}) — notch-filter "
            "mitigation not ported (reference EEPROM has no in-band spur)")


def init_bb(hw: AthHw, chan: Channel) -> None:
    """ar5008_hw_init_bb [SRC] ar5008_phy.c — read the synth delay (for the post-enable wait)
    then power the baseband on via AR_PHY_ACTIVE. The synth-delay udelay has no wire effect."""
    hw.read(R.AR_PHY_RX_DELAY)                            # & AR_PHY_RX_DELAY_DELAY (for the wait)
    hw.write(R.AR_PHY_ACTIVE, R.AR_PHY_ACTIVE_EN)


def rfbus_req(hw: AthHw) -> bool:
    """ar5008_hw_rfbus_req [SRC] ar5008_phy.c:887 — ask the baseband to pause RX so the synth can
    retune, then wait for the grant. Used by the fast-channel-change path."""
    hw.write(R.AR_PHY_RFBUS_REQ, R.AR_PHY_RFBUS_REQ_EN)
    return hw.wait(R.AR_PHY_RFBUS_GRANT, R.AR_PHY_RFBUS_GRANT_EN, R.AR_PHY_RFBUS_GRANT_EN)


def rfbus_done(hw: AthHw) -> None:
    """ar5008_hw_rfbus_done [SRC] ar5008_phy.c:894 — release the baseband after the retune. The
    synth-settling udelay (read AR_PHY_RX_DELAY) has no wire effect beyond the read."""
    hw.read(R.AR_PHY_RX_DELAY)                            # & AR_PHY_RX_DELAY_DELAY (settle wait)
    hw.write(R.AR_PHY_RFBUS_REQ, 0)


def load_ani_reg(hw: AthHw, chan: Channel) -> None:
    """ar9002_hw_load_ani_reg [SRC] ar9002_hw.c:426 — re-apply the AR9271 ANI baseline table for
    the channel's mode (the fast-channel-change path skips process_ini, which is what normally
    seeds these). modesIndex 4 = 2.4 GHz / 20 MHz. The CCK-detect row preserves the live
    weak-signal threshold, taking only the rest of the field from the table."""
    modesIndex = (2 if chan.is_ht40() else 1) if chan.is_5ghz() else (3 if chan.is_ht40() else 4)
    hw.enable_write_buffer()
    for row in I.MODES_9271_ANI_reg:
        reg, val = row[0], row[modesIndex]
        if reg == R.AR_PHY_CCK_DETECT:
            val_orig = hw.read(reg)
            val = ((val & R.AR_PHY_CCK_DETECT_WEAK_SIG_THR_CCK)
                   | (val_orig & ~R.AR_PHY_CCK_DETECT_WEAK_SIG_THR_CCK)) & 0xFFFFFFFF
        hw.write(reg, val)
    hw.write_flush()


def rf_set_freq(hw: AthHw, chan: Channel) -> None:
    """ar9002_hw_set_channel [SRC] ar9002_phy.c:66 — program the single-chip synthesizer. The
    AR9271 is 2.4 GHz only, so this is always the fractional 2 GHz path: seed CHANSEL_2G and
    set bMode/fracMode in AR_PHY_SYNTH_CONTROL (the 5 GHz ndiv branch is guarded out)."""
    freq = chan.center_freq                              # centers.synth_center
    reg32 = hw.read(R.AR_PHY_SYNTH_CONTROL) & 0xc0000000
    if freq >= 4800:                                     # 5 GHz — never on the 9271
        raise NotImplementedError("ar9271_v2: 5 GHz synthesizer not ported")
    b_mode, frac_mode, amode_ref_sel = 1, 1, 0
    channel_sel = R.CHANSEL_2G(freq)
    # not AR_SREV_9287_11_OR_LATER: toggle CCK channel-14 spreading via AR_PHY_CCK_TX_CTRL.
    txctl = hw.read(R.AR_PHY_CCK_TX_CTRL)
    if freq == 2484:                                     # channel 14 (unused here)
        hw.write(R.AR_PHY_CCK_TX_CTRL, txctl | R.AR_PHY_CCK_TX_CTRL_JAPAN)
    else:
        hw.write(R.AR_PHY_CCK_TX_CTRL, txctl & ~R.AR_PHY_CCK_TX_CTRL_JAPAN)
    reg32 |= (b_mode << 29) | (frac_mode << 28) | (amode_ref_sel << 26) | channel_sel
    hw.write(R.AR_PHY_SYNTH_CONTROL, reg32 & 0xFFFFFFFF)


def init_chain_masks(hw: AthHw) -> None:
    """ar5008_hw_init_chain_masks [SRC] ar5008_phy.c:813 — program the RX/cal/self-gen chain
    masks from the eeprom-derived chainmask (1T1R on the AR9271). The 3-chain alt-swap and the
    5416_1.0 special case are ported behind their checks but never run here."""
    rx, tx = hw.rxchainmask, hw.txchainmask
    if rx == 0x5:                                        # untested: 3-chain alt-swap
        hw.rmw(R.AR_PHY_ANALOG_SWAP, R.AR_PHY_SWAP_ALT_CHAIN, 0)
    hw.enable_write_buffer()
    if rx in (0x1, 0x2, 0x3, 0x5, 0x7):
        hw.write(R.AR_PHY_RX_CHAINMASK, rx)
        hw.write(R.AR_PHY_CAL_CHAINMASK, rx)
    hw.write(R.AR_SELFGEN_MASK, tx)
    hw.write_flush()
    if tx == 0x5:                                        # untested
        hw.rmw(R.AR_PHY_ANALOG_SWAP, R.AR_PHY_SWAP_ALT_CHAIN, 0)


def override_ini(hw: AthHw, chan: Channel) -> None:
    """ar5008_hw_override_ini [SRC] ar5008_phy.c:653 — block RX during the change, then drop
    the adhoc multicast-key search and ignore CFP. For 9280+ it returns right after."""
    hw.rmw(R.AR_DIAG_SW, R.AR_DIAG_RX_DIS | R.AR_DIAG_RX_ABORT, 0)   # REG_SET_BIT
    val = hw.read(R.AR_PCU_MISC_MODE2) & ~R.AR_ADHOC_MCAST_KEYID_ENABLE
    # !AR_SREV_9271 would clear HWWAR1 — skipped here (this is a 9271).
    val |= R.AR_PCU_MISC_MODE2_CFP_IGNORE
    hw.write(R.AR_PCU_MISC_MODE2, val)


def set_channel_regs(hw: AthHw, chan: Channel) -> None:
    """ar5008_hw_set_channel_regs [SRC] ar5008_phy.c:678 — the per-channel PHY config: 11n
    flags (20 MHz here), the 20/40 MAC mode, and the global TX / carrier-sense timeouts."""
    enable_dac_fifo = hw.read(R.AR_PHY_TURBO) & R.AR_PHY_FC_ENABLE_DAC_FIFO   # 9285_12+
    phymode = (R.AR_PHY_FC_HT_EN | R.AR_PHY_FC_SHORT_GI_40
               | R.AR_PHY_FC_SINGLE_HT_LTF1 | R.AR_PHY_FC_WALSH | enable_dac_fifo)
    # HT40 would add DYN2040 bits; monitor runs 20 MHz.
    hw.enable_write_buffer()
    hw.write(R.AR_PHY_TURBO, phymode)
    hw.write(R.AR_2040_MODE, 0)                          # set11nmac2040 (20 MHz -> 0)
    hw.write(R.AR_GTXTO, 25 << R.AR_GTXTO_TIMEOUT_LIMIT_S)
    hw.write(R.AR_CST, 0xF << R.AR_CST_TIMEOUT_LIMIT_S)
    hw.write_flush()


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
