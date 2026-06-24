"""BBP init for the RT5370 (RT5390).

``init_bbp`` reproduces ``rt2800_init_bbp`` [SRC rt2800lib.c:7247-7309]: the RT5390/
RT5392 dispatch to ``rt2800_init_bbp_53xx`` [SRC rt2800lib.c:6858-6965] followed by the
shared EEPROM-BBP overlay loop. This card is RT5390 so BBP106=0x03 and the RT5392-only
writes (BBP88/95/98/134/135, BBP106=0x12) are skipped; the BT-coex GPIO block
(bt_coexist clear) and the hardware-RX-antenna-diversity block (rev 0x0502, below
REV_RT5390R/RT5370G) are both gated off.
"""
from __future__ import annotations

from . import constants as C
from .constants import ChipInfo, get_field, set_field
from .eeprom import EepromValues
from .transport import RT5370Transport


def bbp4_mac_if_ctrl(t: RT5370Transport) -> None:
    """Set BBP4 MAC_IF_CTRL [SRC rt2800lib.c:6378-6385 rt2800_bbp4_mac_if_ctrl]."""
    value = t.bbp_read(4)
    value = set_field(value, C.BBP4_MAC_IF_CTRL, 1)
    t.bbp_write(4, value)


def init_freq_calibration(t: RT5370Transport) -> None:
    """Reprogram the inband interface for the frequency-calibration RXWI fields
    [SRC rt2800lib.c:6387-6391 rt2800_init_freq_calibration]."""
    t.bbp_write(142, 1)
    t.bbp_write(143, 57)


def disable_unused_dac_adc(t: RT5370Transport, ev: EepromValues) -> None:
    """Power down DAC1/ADC1 on a single-chain part [SRC rt2800lib.c:6434-6446]. Gated on
    the RAW NIC_CONF0 TXPATH/RXPATH==1; this card is 1T1R so both fire (BBP138 TX_DAC1=1,
    RX_ADC1=0)."""
    value = t.bbp_read(138)
    if ev.tx_chain_num == 1:
        value |= 0x20                      # BBP138_TX_DAC1
    if ev.rx_chain_num == 1:
        value &= ~0x02                     # BBP138_RX_ADC1
    t.bbp_write(138, value)


def init_bbp_53xx(t: RT5370Transport, chip: ChipInfo, ev: EepromValues) -> None:
    """[SRC rt2800lib.c:6858-6965 rt2800_init_bbp_53xx]."""
    bbp4_mac_if_ctrl(t)

    t.bbp_write(31, 0x08)
    t.bbp_write(65, 0x2c)
    t.bbp_write(66, 0x38)
    t.bbp_write(68, 0x0b)
    t.bbp_write(69, 0x12)
    t.bbp_write(73, 0x13)
    t.bbp_write(75, 0x46)
    t.bbp_write(76, 0x28)
    t.bbp_write(77, 0x59)
    t.bbp_write(70, 0x0a)
    t.bbp_write(79, 0x13)
    t.bbp_write(80, 0x05)
    t.bbp_write(81, 0x33)
    t.bbp_write(82, 0x62)
    t.bbp_write(83, 0x7a)
    t.bbp_write(84, 0x9a)
    t.bbp_write(86, 0x38)

    if chip.is_rt(C.RT5392):
        t.bbp_write(88, 0x90)

    t.bbp_write(91, 0x04)
    t.bbp_write(92, 0x02)

    if chip.is_rt(C.RT5392):
        t.bbp_write(95, 0x9a)
        t.bbp_write(98, 0x12)

    t.bbp_write(103, 0xc0)
    t.bbp_write(104, 0x92)
    t.bbp_write(105, 0x3c)

    if chip.is_rt(C.RT5390):
        t.bbp_write(106, 0x03)
    else:                                  # RT5392 path — never taken here (chip is RT5390)
        t.bbp_write(106, 0x12)

    t.bbp_write(128, 0x12)

    if chip.is_rt(C.RT5392):
        t.bbp_write(134, 0xd0)
        t.bbp_write(135, 0xf6)

    disable_unused_dac_adc(t, ev)

    div_mode = ev.ant_diversity            # NIC_CONF1 ANT_DIVERSITY
    ant = 1 if div_mode == 3 else 0

    if ev.bt_coexist:
        # #TODO untestable: bt_coexist is clear on this card (no BT-combo).
        reg = t.register_read(C.GPIO_CTRL)
        reg = set_field(reg, C.GPIO_CTRL_DIR3, 0)
        reg = set_field(reg, C.GPIO_CTRL_DIR6, 0)
        reg = set_field(reg, C.GPIO_CTRL_VAL3, 0)
        reg = set_field(reg, C.GPIO_CTRL_VAL6, 0)
        if ant == 0:
            reg = set_field(reg, C.GPIO_CTRL_VAL3, 1)
        elif ant == 1:
            reg = set_field(reg, C.GPIO_CTRL_VAL6, 1)
        t.register_write(C.GPIO_CTRL, reg)

    if (chip.rt_rev_gte(C.RT5390, C.REV_RT5390R)
            or chip.rt_rev_gte(C.RT5390, C.REV_RT5370G)):
        # #TODO untestable: hardware RX antenna diversity — gated on RT5390 rev, never
        # taken here (this card is rev 0x0502, below REV_RT5390R/RT5370G). Ported per source.
        t.bbp_write(150, 0)
        t.bbp_write(151, 0)
        t.bbp_write(154, 0)

    value = t.bbp_read(152)
    value = set_field(value, C.BBP152_RX_DEFAULT_ANT, 1 if ant == 0 else 0)
    t.bbp_write(152, value)

    init_freq_calibration(t)


def init_bbp(t: RT5370Transport, chip: ChipInfo, ev: EepromValues) -> None:
    """Full BBP init [SRC rt2800lib.c:7247-7309 rt2800_init_bbp]: per-chip table then the
    shared EEPROM-BBP overlay (write each non-blank EEPROM_BBP entry)."""
    init_bbp_53xx(t, chip, ev)

    for i in range(C.EEPROM_BBP_SIZE):
        word = ev.word(C.EEPROM_BBP_START + i)
        if word not in (0xFFFF, 0x0000):
            reg_id = get_field(word, C.EEPROM_BBP_REG_ID)
            value = get_field(word, C.EEPROM_BBP_VALUE)
            t.bbp_write(reg_id, value)
