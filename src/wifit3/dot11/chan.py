"""Channel helpers shared by the dot11 IE builders: 802.11 operating class and band from a channel
number (IEEE 802.11-2020 Annex E global operating classes)."""


def channel_operating_class(channel: int) -> int:
    """Global operating class (802.11-2020 Table E-4) for a 20 MHz channel. Raises for a channel
    outside the mapped 2.4/5 GHz ranges (pass operating_class= explicitly instead)."""
    if 1 <= channel <= 13:
        return 81
    if channel == 14:
        return 82
    if 36 <= channel <= 48:
        return 115
    if 52 <= channel <= 64:
        return 118
    if 100 <= channel <= 144:
        return 121
    if 149 <= channel <= 177:
        return 125
    raise ValueError(f"no operating class mapping for channel {channel}")


def same_band(channel_a: int, channel_b: int) -> bool:
    """True when both channels sit in the same band (both 2.4 GHz <=14, or both 5 GHz >14)."""
    return (channel_a <= 14) == (channel_b <= 14)
