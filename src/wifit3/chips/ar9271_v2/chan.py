"""Channel model — the slice of ``struct ath9k_channel`` the bring-up/tune path reads.

The AR9271 is 2.4 GHz only, so ``channelFlags`` never carries CHANNEL_5GHZ; the predicates
mirror the IS_CHAN_* macros [SRC] hw.h:457-468 so ported code reads like the C.
"""
from __future__ import annotations

from dataclasses import dataclass

CHANNEL_5GHZ = 0x1                      # [SRC] hw.h:457
CHANNEL_HALF = 0x2
CHANNEL_QUARTER = 0x4


@dataclass
class Channel:
    channel: int                       # 802.11 channel number (1..14 on 2.4 GHz)
    center_freq: int                   # MHz
    channelFlags: int = 0

    def is_5ghz(self) -> bool:
        return bool(self.channelFlags & CHANNEL_5GHZ)

    def is_2ghz(self) -> bool:
        return not self.is_5ghz()

    def is_half_rate(self) -> bool:
        return bool(self.channelFlags & CHANNEL_HALF)

    def is_quarter_rate(self) -> bool:
        return bool(self.channelFlags & CHANNEL_QUARTER)


def channel_2ghz(ch: int) -> Channel:
    """A 2.4 GHz channel by number. ch1=2412 MHz, +5 MHz/channel (ch14=2484 is special but
    unused here)."""
    return Channel(channel=ch, center_freq=2407 + ch * 5, channelFlags=0)
