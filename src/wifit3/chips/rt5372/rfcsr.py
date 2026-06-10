"""RFCSR init for the RT5372 (RT5392).

``init_rfcsr_5392`` reproduces ``rt2800_init_rfcsr_5392`` [SRC rt2800lib.c:8394-8460]:
a one-shot RFCSR2 calibration kick, a 56-entry static RFCSR table, then
``rt2800_normal_mode_setup_5xxx`` + ``rt2800_led_open_drain_enable``.

Unlike RT3070, RT5392 runs **no** RX-filter loopback calibration (no ``init_rx_filter`` /
BBP55 feedback loop) and writes no per-channel RFCSR24/31 from a cal result — the table
values are final. So there is no init-derived calibration state threaded into the tune;
``init_rfcsr_5392`` returns ``None``.
"""
from __future__ import annotations

from . import constants as C
from .bbp import bbp4_mac_if_ctrl
from .constants import ChipInfo, set_field
from .eeprom import EepromValues
from .transport import RT5372Transport

# Static RFCSR init table [SRC rt2800lib.c:8398-8455]: (regnum, value), in wire order.
_RFCSR_5392 = (
    (1, 0x17), (3, 0x88), (5, 0x10), (6, 0xe0), (7, 0x00), (10, 0x53), (11, 0x4a),
    (12, 0x46), (13, 0x9f), (14, 0x00), (15, 0x00), (16, 0x00), (18, 0x03), (19, 0x4d),
    (20, 0x00), (21, 0x8d), (22, 0x20), (23, 0x0b), (24, 0x44), (25, 0x80), (26, 0x82),
    (27, 0x09), (28, 0x00), (29, 0x10), (30, 0x10), (31, 0x80), (32, 0x20), (33, 0xC0),
    (34, 0x07), (35, 0x12), (36, 0x00), (37, 0x08), (38, 0x89), (39, 0x1b), (40, 0x0f),
    (41, 0xbb), (42, 0xd5), (43, 0x9b), (44, 0x0e), (45, 0xa2), (46, 0x73), (47, 0x0c),
    (48, 0x10), (49, 0x94), (50, 0x94), (51, 0x3a), (52, 0x48), (53, 0x44), (54, 0x38),
    (55, 0x43), (56, 0xa1), (57, 0x00), (58, 0x39), (59, 0x07), (60, 0x45), (61, 0x91),
    (62, 0x39), (63, 0x07),
)


def rf_init_calibration(t: RT5372Transport, rf_reg: int) -> None:
    """Pulse the calibration bit (FIELD8 0x80) of RFCSR ``rf_reg`` high then low
    [SRC rt2800lib.c:7385-7396 rt2800_rf_init_calibration]."""
    rfcsr = t.rfcsr_read(rf_reg)
    rfcsr = set_field(rfcsr, 0x80, 1)
    t.rfcsr_write(rf_reg, rfcsr)
    # kernel msleep(1)
    rfcsr = set_field(rfcsr, 0x80, 0)
    t.rfcsr_write(rf_reg, rfcsr)


def led_open_drain_enable(t: RT5372Transport) -> None:
    """[SRC rt2800lib.c:7311-7318 rt2800_led_open_drain_enable]."""
    reg = t.register_read(C.OPT_14_CSR)
    reg = set_field(reg, C.OPT_14_CSR_BIT0, 1)
    t.register_write(C.OPT_14_CSR, reg)


def normal_mode_setup_5xxx(t: RT5372Transport, ev: EepromValues) -> None:
    """[SRC rt2800lib.c:7551-7578 rt2800_normal_mode_setup_5xxx]. DAC1/ADC1 power-down is
    gated on the single-chain case (==1); on PAU05/PAU06 (2T2R) BBP138 is read + written
    back unchanged. Then RX LO1/LO2 disables, the MAC-IF toggle, and RFCSR30 RX_VCM=2."""
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


def init_rfcsr_5392(t: RT5372Transport, chip: ChipInfo, ev: EepromValues) -> None:
    """[SRC rt2800lib.c:8394-8460 rt2800_init_rfcsr_5392]. Returns None — RT5392 has no
    init-derived per-tune calibration (no RX-filter loopback)."""
    rf_init_calibration(t, 2)
    for regnum, value in _RFCSR_5392:
        t.rfcsr_write(regnum, value)
    normal_mode_setup_5xxx(t, ev)
    led_open_drain_enable(t)
    return None
