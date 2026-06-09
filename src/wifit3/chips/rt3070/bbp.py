"""BBP (baseband processor) init for the RT3070 — RF30xx family path.

Ported from ``rt2800_init_bbp`` dispatch [SRC rt2800lib.c:7247-7309] →
``rt2800_init_bbp_30xx`` [SRC rt2800lib.c:6521-6561], followed by the common
EEPROM-BBP-array overlay loop. Confirmed against capture-1 by verify_pcap.
"""
from __future__ import annotations

from . import constants as C
from .constants import ChipInfo, get_field
from .eeprom import EepromValues
from .transport import RT3070Transport


def _disable_unused_dac_adc(t: RT3070Transport, ev: EepromValues) -> None:
    """Turn off the 2nd DAC/ADC on 1T/1R parts [SRC rt2800lib.c:6434-6446].

    #TODO untestable: only reached for RT3071/RT3090; this RT3070 never calls it.
    """
    value = t.bbp_read(138)
    if ev.tx_chain_num == 1:
        value |= 0x20
    if ev.rx_chain_num == 1:
        value &= ~0x02
    t.bbp_write(138, value)


def init_bbp_30xx(t: RT3070Transport, chip: ChipInfo, ev: EepromValues) -> None:
    """[SRC rt2800lib.c:6521-6561 rt2800_init_bbp_30xx]"""
    t.bbp_write(65, 0x2C)
    t.bbp_write(66, 0x38)
    t.bbp_write(69, 0x12)
    t.bbp_write(73, 0x10)
    t.bbp_write(70, 0x0A)
    t.bbp_write(79, 0x13)
    t.bbp_write(80, 0x05)
    t.bbp_write(81, 0x33)
    t.bbp_write(82, 0x62)
    t.bbp_write(83, 0x6A)
    t.bbp_write(84, 0x99)
    t.bbp_write(86, 0x00)
    t.bbp_write(91, 0x04)
    t.bbp_write(92, 0x00)

    if (chip.rt_rev_gte(C.RT3070, C.REV_RT3070F)
            or chip.rt_rev_gte(C.RT3071, C.REV_RT3071E)
            or chip.rt_rev_gte(C.RT3090, C.REV_RT3070E)):
        t.bbp_write(103, 0xC0)            # this card (REV_RT3070F)
    else:
        t.bbp_write(103, 0x00)

    t.bbp_write(105, 0x05)
    t.bbp_write(106, 0x35)

    if chip.is_rt(C.RT3071) or chip.is_rt(C.RT3090):
        _disable_unused_dac_adc(t, ev)   # #TODO untestable: RT3071/3090 only


def init_bbp(t: RT3070Transport, chip: ChipInfo, ev: EepromValues) -> None:
    """Full BBP init [SRC rt2800lib.c:7247-7309]: per-chip table (RF30xx) then the
    EEPROM-BBP overlay loop. This card's EEPROM-BBP array is empty (all 0xffff/0),
    so the loop emits no writes — but it is ported, not assumed."""
    init_bbp_30xx(t, chip, ev)

    for i in range(C.EEPROM_BBP_SIZE):
        word = ev.word(C.EEPROM_BBP_START + i)
        if word != 0xFFFF and word != 0x0000:
            t.bbp_write(get_field(word, C.EEPROM_BBP_REG_ID),
                        get_field(word, C.EEPROM_BBP_VALUE))
