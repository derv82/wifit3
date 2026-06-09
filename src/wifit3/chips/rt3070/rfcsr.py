"""RFCSR init for the RT3070 — RF30xx family path + RX-filter calibration.

Ported from ``rt2800_init_rfcsr_30xx`` [SRC rt2800lib.c:7618-7686] and the helpers
it calls: ``rt2800_rf_init_calibration`` (7385), ``rt2800_init_rx_filter`` (7320),
``rt2800_rx_filter_calibration`` (7398), ``rt2800_led_open_drain_enable`` (7311),
``rt2800_normal_mode_setup_3xxx`` (7444). Confirmed against capture-1 by verify_pcap.

RX-filter calibration is read-feedback: its tuning loops branch on BBP55
passband/stopband reads. Under the gate those reads are the recorded ones, so a
faithful port reproduces the captured write sequence exactly [[feedback_porting_no_hardware]].

This card is RT3070 rev REV_RT3070F, so the LDO_CFG0 BGSEL/VLEVEL write, the
RT3071/RT3090 branch, and the trailing RFCSR27=0x03 are all NOT taken (rev gating);
they are ported faithfully and marked ``#TODO untestable`` so a future RT3071/3090
inherits a correct port.
"""
from __future__ import annotations

from . import constants as C
from .constants import ChipInfo, get_field, set_field
from .eeprom import EepromValues
from .transport import RT3070Transport


def rf_init_calibration(t: RT3070Transport, rf_reg: int) -> None:
    """Pulse RFCSR bit7 to kick a calibration [SRC rt2800lib.c:7385-7396]."""
    rfcsr = t.rfcsr_read(rf_reg)
    rfcsr |= 0x80
    t.rfcsr_write(rf_reg, rfcsr)
    # kernel msleep(1)
    rfcsr &= ~0x80
    t.rfcsr_write(rf_reg, rfcsr)


def init_rx_filter(t: RT3070Transport, bw40: bool, filter_target: int) -> int:
    """Auto-tune the RX baseband filter [SRC rt2800lib.c:7320-7383
    rt2800_init_rx_filter]. The two BBP55 feedback loops branch on the recorded
    passband/stopband values."""
    rfcsr24 = 0x27 if bw40 else 0x07
    t.rfcsr_write(24, rfcsr24)

    bbp = t.bbp_read(4)
    bbp = set_field(bbp, C.BBP4_BANDWIDTH, 2 * int(bw40))
    t.bbp_write(4, bbp)

    rfcsr = t.rfcsr_read(31)
    rfcsr = set_field(rfcsr, C.RFCSR31_RX_H20M, int(bw40))
    t.rfcsr_write(31, rfcsr)

    rfcsr = t.rfcsr_read(22)
    rfcsr = set_field(rfcsr, C.RFCSR22_BASEBAND_LOOPBACK, 1)
    t.rfcsr_write(22, rfcsr)

    # Passband test tone.
    t.bbp_write(24, 0)
    passband = 0
    for _ in range(100):
        t.bbp_write(25, 0x90)
        # kernel msleep(1)
        passband = t.bbp_read(55)
        if passband:
            break

    # Stopband test tone — tune rfcsr24 up until passband-stopband exceeds target.
    t.bbp_write(24, 0x06)
    overtuned = 0
    for _ in range(100):
        t.bbp_write(25, 0x90)
        # kernel msleep(1)
        stopband = t.bbp_read(55)
        if (passband - stopband) <= filter_target:
            rfcsr24 += 1
            overtuned += 1 if (passband - stopband) == filter_target else 0
        else:
            break
        t.rfcsr_write(24, rfcsr24)

    rfcsr24 -= 1 if overtuned else 0
    t.rfcsr_write(24, rfcsr24)
    return rfcsr24


def rx_filter_calibration(t: RT3070Transport, chip: ChipInfo) -> tuple[int, int]:
    """Calibrate both the 20 MHz and 40 MHz RX filters [SRC rt2800lib.c:7398-7442
    rt2800_rx_filter_calibration]. Both run at init regardless of operating width
    (we only ever *tune* 20 MHz channels). Returns (bbp25, bbp26) for channel switch."""
    if chip.is_rt(C.RT3070):
        filter_tgt_bw20, filter_tgt_bw40 = 0x16, 0x19
    else:
        filter_tgt_bw20, filter_tgt_bw40 = 0x13, 0x15

    init_rx_filter(t, False, filter_tgt_bw20)
    init_rx_filter(t, True, filter_tgt_bw40)

    # Saved for RF3052 channel switching (unused on RF3020, but the reads are on
    # the wire so we issue them).
    bbp25 = t.bbp_read(25)
    bbp26 = t.bbp_read(26)

    t.bbp_write(24, 0)

    rfcsr = t.rfcsr_read(22)
    rfcsr = set_field(rfcsr, C.RFCSR22_BASEBAND_LOOPBACK, 0)
    t.rfcsr_write(22, rfcsr)

    bbp = t.bbp_read(4)
    bbp = set_field(bbp, C.BBP4_BANDWIDTH, 0)
    t.bbp_write(4, bbp)
    return bbp25, bbp26


def led_open_drain_enable(t: RT3070Transport) -> None:
    """[SRC rt2800lib.c:7311-7318 rt2800_led_open_drain_enable]"""
    reg = t.register_read(C.OPT_14_CSR)
    reg = set_field(reg, C.OPT_14_CSR_BIT0, 1)
    t.register_write(C.OPT_14_CSR, reg)


def normal_mode_setup_3xxx(t: RT3070Transport, chip: ChipInfo, ev: EepromValues) -> None:
    """[SRC rt2800lib.c:7444-7513 rt2800_normal_mode_setup_3xxx]"""
    rfcsr = t.rfcsr_read(17)
    rfcsr = set_field(rfcsr, C.RFCSR17_TX_LO1_EN, 0)
    if (chip.is_rt(C.RT3070)
            or chip.rt_rev_lt(C.RT3071, C.REV_RT3071E)
            or chip.rt_rev_lt(C.RT3090, C.REV_RT3070E)):
        if not ev.external_lna_bg:
            rfcsr = set_field(rfcsr, C.RFCSR17_R, 1)

    min_gain = 1 if chip.is_rt(C.RT3070) else 2
    if ev.txmixer_gain_24g >= min_gain:
        rfcsr = set_field(rfcsr, C.RFCSR17_TXMIXER_GAIN, ev.txmixer_gain_24g)
    t.rfcsr_write(17, rfcsr)

    if chip.is_rt(C.RT3090):
        # #TODO untestable: RT3090 DAC1/ADC1 power-down.
        bbp = t.bbp_read(138)
        if ev.rx_chain_num == 1:
            bbp = set_field(bbp, C.BBP138_RX_ADC1, 0)
        if ev.tx_chain_num == 1:
            bbp = set_field(bbp, C.BBP138_TX_DAC1, 1)
        t.bbp_write(138, bbp)

    if chip.is_rt(C.RT3070):
        rfcsr = t.rfcsr_read(27)
        if chip.rt_rev_lt(C.RT3070, C.REV_RT3070F):
            rfcsr = set_field(rfcsr, C.RFCSR27_R1, 3)
        else:                                       # this card (REV_RT3070F)
            rfcsr = set_field(rfcsr, C.RFCSR27_R1, 0)
        rfcsr = set_field(rfcsr, C.RFCSR27_R2, 0)
        rfcsr = set_field(rfcsr, C.RFCSR27_R3, 0)
        rfcsr = set_field(rfcsr, C.RFCSR27_R4, 0)
        t.rfcsr_write(27, rfcsr)
    elif chip.is_rt(C.RT3071) or chip.is_rt(C.RT3090):
        # #TODO untestable: RT3071/RT3090 chain power-down (no hardware).
        rfcsr = t.rfcsr_read(1)
        rfcsr = set_field(rfcsr, C.RFCSR1_RF_BLOCK_EN, 1)
        rfcsr = set_field(rfcsr, C.RFCSR1_RX0_PD, 0)
        rfcsr = set_field(rfcsr, C.RFCSR1_TX0_PD, 0)
        rfcsr = set_field(rfcsr, C.RFCSR1_RX1_PD, 1)
        rfcsr = set_field(rfcsr, C.RFCSR1_TX1_PD, 1)
        t.rfcsr_write(1, rfcsr)

        rfcsr = t.rfcsr_read(15)
        rfcsr = set_field(rfcsr, C.RFCSR15_TX_LO2_EN, 0)
        t.rfcsr_write(15, rfcsr)

        rfcsr = t.rfcsr_read(20)
        rfcsr = set_field(rfcsr, C.RFCSR20_RX_LO1_EN, 0)
        t.rfcsr_write(20, rfcsr)

        rfcsr = t.rfcsr_read(21)
        rfcsr = set_field(rfcsr, C.RFCSR21_RX_LO2_EN, 0)
        t.rfcsr_write(21, rfcsr)


def init_rfcsr_30xx(t: RT3070Transport, chip: ChipInfo, ev: EepromValues) -> None:
    """[SRC rt2800lib.c:7618-7686 rt2800_init_rfcsr_30xx]"""
    rf_init_calibration(t, 30)

    for reg, val in ((4, 0x40), (5, 0x03), (6, 0x02), (7, 0x60), (9, 0x0F),
                     (10, 0x41), (11, 0x21), (12, 0x7B), (14, 0x90), (15, 0x58),
                     (16, 0xB3), (17, 0x92), (18, 0x2C), (19, 0x02), (20, 0xBA),
                     (21, 0xDB), (24, 0x16), (25, 0x03), (29, 0x1F)):
        t.rfcsr_write(reg, val)

    if chip.rt_rev_lt(C.RT3070, C.REV_RT3070F):
        # #TODO untestable: pre-REV_RT3070F LDO core voltage bump (this card is F).
        reg = t.register_read(C.LDO_CFG0)
        reg = set_field(reg, C.LDO_CFG0_BGSEL, 1)
        reg = set_field(reg, C.LDO_CFG0_LDO_CORE_VLEVEL, 3)
        t.register_write(C.LDO_CFG0, reg)
    elif chip.is_rt(C.RT3071) or chip.is_rt(C.RT3090):
        # #TODO untestable: RT3071/RT3090 LDO + GPIO_SWITCH setup (no hardware).
        t.rfcsr_write(31, 0x14)
        rfcsr = t.rfcsr_read(6)
        rfcsr = set_field(rfcsr, C.RFCSR6_R2, 1)
        t.rfcsr_write(6, rfcsr)
        reg = t.register_read(C.LDO_CFG0)
        reg = set_field(reg, C.LDO_CFG0_BGSEL, 1)
        if (chip.rt_rev_lt(C.RT3071, C.REV_RT3071E)
                or chip.rt_rev_lt(C.RT3090, C.REV_RT3070E)):
            if get_field(ev.nic_conf1, C.EEPROM_NIC_CONF1_DAC_TEST):
                reg = set_field(reg, C.LDO_CFG0_LDO_CORE_VLEVEL, 3)
            else:
                reg = set_field(reg, C.LDO_CFG0_LDO_CORE_VLEVEL, 0)
        t.register_write(C.LDO_CFG0, reg)
        reg = t.register_read(C.GPIO_SWITCH)
        reg = set_field(reg, C.GPIO_SWITCH_5, 0)
        t.register_write(C.GPIO_SWITCH, reg)

    rx_filter_calibration(t, chip)

    if (chip.rt_rev_lt(C.RT3070, C.REV_RT3070F)
            or chip.rt_rev_lt(C.RT3071, C.REV_RT3071E)
            or chip.rt_rev_lt(C.RT3090, C.REV_RT3070E)):
        # #TODO untestable: pre-rev-F RFCSR27 (this card is F → skipped).
        t.rfcsr_write(27, 0x03)

    led_open_drain_enable(t)
    normal_mode_setup_3xxx(t, chip, ev)
