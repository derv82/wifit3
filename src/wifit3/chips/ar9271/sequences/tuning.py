import struct

from ..constants import AR_PHY_SYNTH_CONTROL, WMI_REG_WRITE_CMDID


def _synth_word(channel: int) -> int:
    """AR9271 2.4 GHz synthesizer-control word — ath9k CHANSEL_2G fractional-N:
    0x30000000 (the fixed 2.4 GHz PLL bits) | (freq * 0x10000 / 15). Reproduces the two
    captured words exactly (CH1=0x30a0cccc, CH6=0x30a27777). [SRC] ath9k ar9002_phy."""
    freq = 2412 + 5 * (channel - 1)          # 2.4 GHz channel center in MHz (CH1..13)
    return 0x30000000 | ((freq * 0x10000) // 15)


def get_channel_hop_sequence(channel: int):
    """WMI reg-writes for a channel change — the synth poke that moves the RF onto
    ``channel``. (ath9k's per-channel analog/calibration writes aren't ported; basic RX
    works without them, as CH1/CH6 always did — the bug was the synth, not the cal.)"""
    return [(WMI_REG_WRITE_CMDID,
             struct.pack(">II", AR_PHY_SYNTH_CONTROL, _synth_word(channel)))]
