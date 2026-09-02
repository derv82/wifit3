"""RTL8822C TX gap-K: snapshot the RF TX gain table per band and mirror it into BB.

[SRC hal/phydm/halrf/rtl8822c/halrf_txgapk_8822c.c]
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .phy import get_rf_reg, set_bb_reg, set_rf_reg
from .transport import RTL8822CUTransport

_THREE_WIRE = (0x180C, 0x410C)
_GAIN_INDICES = range(1, 32, 3)     # the 11 RF 0x00 gain steps sampled per band

# A representative channel per band, and the band/CCK bits that go with it.
_CH_NUM = (1, 1, 36, 100, 149)
_BAND_NUM = (0x0, 0x0, 0x1, 0x3, 0x5)
_CCK = (0x1, 0x0, 0x0, 0x0, 0x0)
# 0x1b98[14:12] per band index. Band 0 is deliberately absent: the vendor chain starts at 1.
_BAND_SEL = {1: 0x0, 2: 0x2, 3: 0x3, 4: 0x4}


@dataclass
class TxGapKState:
    """_halrf_txgapk_info: the saved RF gain table, read once per driver lifetime."""
    read_txgain: bool = False
    rf3f_bp: list = field(
        default_factory=lambda: [[[0] * 2 for _ in range(11)] for _ in range(5)])


def _write_gain_bb_table(t: RTL8822CUTransport, gapk: TxGapKState) -> None:
    """Copy the saved RF gain table into the BB gain table, one gain index per 0x1b98 latch.
    Once a band/path hits a gain word saturated in both nibbles, that word is repeated for the
    rest of the band instead of the measured one. [SRC halrf_txgapk_8822c.c:302]"""
    for band_idx in range(5):
        for path in (0, 1):
            set_bb_reg(t, 0x1B00, 0x00000006, path)
            if band_idx in _BAND_SEL:
                set_bb_reg(t, 0x1B98, 0x00007000, _BAND_SEL[band_idx])
            set_bb_reg(t, 0x1B9C, 0x000000FF, 0x88)
            tmp_3f = 0
            check_txgain = False
            for gain_idx in range(11):
                gain = gapk.rf3f_bp[band_idx][gain_idx][path]
                if ((gain & 0xF00) >> 8) >= 0xC and ((gain & 0xF0) >> 4) >= 0xE:
                    if not check_txgain:
                        tmp_3f = gain
                        check_txgain = True
                else:
                    tmp_3f = gain & 0xFFF
                set_bb_reg(t, 0x1B98, 0x00000FFF, tmp_3f)
                set_bb_reg(t, 0x1B98, 0x000F0000, gain_idx)
                set_bb_reg(t, 0x1B98, 0x00008000, 0x1)
                set_bb_reg(t, 0x1B98, 0x00008000, 0x0)


def save_all_tx_gain_table(t: RTL8822CUTransport, gapk: TxGapKState) -> None:
    """halrf_txgapk_save_all_tx_gain_table_8822c: park each RF path on a representative channel
    per band, read back its 11-entry TX gain table, then mirror the lot into BB.
    [SRC halrf_txgapk_8822c.c:711]"""
    if gapk.read_txgain:
        _write_gain_bb_table(t, gapk)
        return
    for band_idx in range(5):
        for path in (0, 1):
            rf18 = get_rf_reg(t, path, 0x18, 0xFFFFF)
            set_bb_reg(t, _THREE_WIRE[path], 0x00000003, 0x0)
            set_rf_reg(t, path, 0x18, 0x000FF, _CH_NUM[band_idx])
            set_rf_reg(t, path, 0x18, 0x70000, _BAND_NUM[band_idx])
            set_rf_reg(t, path, 0x1A, 0x00001, _CCK[band_idx])
            set_rf_reg(t, path, 0x1A, 0x10000, _CCK[band_idx])
            for gain_idx, rf0_idx in enumerate(_GAIN_INDICES):
                set_rf_reg(t, path, 0x00, 0x000FF, rf0_idx)
                gapk.rf3f_bp[band_idx][gain_idx][path] = get_rf_reg(t, path, 0x5F, 0xFFFFF)
            set_rf_reg(t, path, 0x18, 0xFFFFF, rf18)
            set_bb_reg(t, _THREE_WIRE[path], 0x00000003, 0x3)
    _write_gain_bb_table(t, gapk)
    gapk.read_txgain = True
