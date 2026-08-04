"""RFCSR init for the RT5370 (RT5390).

``init_rfcsr_5390`` reproduces ``rt2800_init_rfcsr_5390`` [SRC rt2800lib.c:8296-8392]:
a one-shot RFCSR2 calibration kick, a static RFCSR table (six entries gated on the
REV_RT5390F revision), then ``rt2800_normal_mode_setup_5xxx`` +
``rt2800_led_open_drain_enable``.

Unlike RT3070, RT5390 runs **no** RX-filter loopback calibration (no ``init_rx_filter`` /
BBP55 feedback loop) and writes no per-channel RFCSR24/31 from a cal result — the table
values are final. So there is no init-derived calibration state threaded into the tune;
``init_rfcsr_5390`` returns ``None``.
"""
from __future__ import annotations

from . import constants as C
from .bbp import bbp4_mac_if_ctrl
from .constants import ChipInfo, set_field
from .eeprom import EepromValues
from .transport import RT5370Transport


def rf_init_calibration(t: RT5370Transport, rf_reg: int) -> None:
    """Pulse the calibration bit (FIELD8 0x80) of RFCSR ``rf_reg`` high then low
    [SRC rt2800lib.c:7385-7396 rt2800_rf_init_calibration]."""
    rfcsr = t.rfcsr_read(rf_reg)
    rfcsr = set_field(rfcsr, 0x80, 1)
    t.rfcsr_write(rf_reg, rfcsr)
    # kernel msleep(1)
    rfcsr = set_field(rfcsr, 0x80, 0)
    t.rfcsr_write(rf_reg, rfcsr)


def led_open_drain_enable(t: RT5370Transport) -> None:
    """[SRC rt2800lib.c:7311-7318 rt2800_led_open_drain_enable]."""
    reg = t.register_read(C.OPT_14_CSR)
    reg = set_field(reg, C.OPT_14_CSR_BIT0, 1)
    t.register_write(C.OPT_14_CSR, reg)


def normal_mode_setup_5xxx(t: RT5370Transport, ev: EepromValues) -> None:
    """[SRC rt2800lib.c:7551-7578 rt2800_normal_mode_setup_5xxx]. DAC1/ADC1 power-down is
    gated on the single-chain case (==1); this card is 1T1R so both fire (BBP138 RX_ADC1=0,
    TX_DAC1=1). Then RX LO1/LO2 disables, the MAC-IF toggle, and RFCSR30 RX_VCM=2."""
    reg = t.bbp_read(138)
    if ev.rx_chain_num == 1:
        reg = set_field(reg, C.BBP138_RX_ADC1, 0)
    if ev.tx_chain_num == 1:
        reg = set_field(reg, C.BBP138_TX_DAC1, 1)
    t.bbp_write(138, reg)

    reg = t.rfcsr_read(38)
    reg = set_field(reg, C.RFCSR38_RX_LO1_EN, 0)
    t.rfcsr_write(38, reg)

    reg = t.rfcsr_read(39)
    reg = set_field(reg, C.RFCSR39_RX_LO2_EN, 0)
    t.rfcsr_write(39, reg)

    bbp4_mac_if_ctrl(t)

    reg = t.rfcsr_read(30)
    reg = set_field(reg, C.RFCSR30_RX_VCM, 2)
    t.rfcsr_write(30, reg)


def init_rfcsr_5390(t: RT5370Transport, chip: ChipInfo, ev: EepromValues) -> None:
    """[SRC rt2800lib.c:8296-8392 rt2800_init_rfcsr_5390]. Returns None — RT5390 has no
    init-derived per-tune RX-filter-loopback calibration.

    Six entries (RFCSR 6/25/46/53/56/61) fork on REV_RT5390F; this card reads rev 0x0502
    == REV_RT5390F so ``f`` is True. This is a USB-only driver, so ``rt2x00_is_usb()`` is
    always True — the PCI sub-arm of the RFCSR25/61 branches is #TODO untestable here.
    """
    rf_init_calibration(t, 2)

    f = chip.rt_rev_gte(C.RT5390, C.REV_RT5390F)
    # (regnum, value) in exact wire order [SRC rt2800lib.c:8300-8387]. The gated values
    # match the C's rev (and USB) checks; everything else is a fixed write.
    table = (
        (1, 0x0f), (2, 0x80), (3, 0x88), (5, 0x10),
        (6, 0xe0 if f else 0xa0),                          # :8304-8307
        (7, 0x00), (10, 0x53), (11, 0x4a), (12, 0x46), (13, 0x9f), (14, 0x00),
        (15, 0x00), (16, 0x00), (18, 0x03), (19, 0x00),
        (20, 0x00), (21, 0x00), (22, 0x20), (23, 0x00), (24, 0x00),
        (25, 0x80 if f else 0xc0),                         # :8324-8328 (usb && F)
        (26, 0x00), (27, 0x09), (28, 0x00), (29, 0x10),
        (30, 0x10), (31, 0x80), (32, 0x80), (33, 0x00), (34, 0x07), (35, 0x12),
        (36, 0x00), (37, 0x08), (38, 0x85), (39, 0x1b),
        (40, 0x0b), (41, 0xbb), (42, 0xd2), (43, 0x9a), (44, 0x0e), (45, 0xa2),
        (46, 0x73 if f else 0x7b),                         # :8351-8354
        (47, 0x00), (48, 0x10), (49, 0x94),
        (52, 0x38),
        (53, 0x00 if f else 0x84),                         # :8360-8363
        (54, 0x78), (55, 0x44),
        (56, 0x42 if f else 0x22),                         # :8366-8369
        (57, 0x80), (58, 0x7f), (59, 0x8f),
        (60, 0x45),
        (61, 0xd1 if f else 0xdd),                         # :8375-8385 (usb arm)
        (62, 0x00), (63, 0x00),
    )
    for regnum, value in table:
        t.rfcsr_write(regnum, value)

    normal_mode_setup_5xxx(t, ev)
    led_open_drain_enable(t)
    return None
