"""802.11 channel helpers: scan-hop ordering and per-band label/range compression."""
from __future__ import annotations


# The non-overlapping 2.4 GHz trio nearly every router parks on (FCC 1/6/11)
_PRIORITY_2G = (1, 6, 11)


def parse_custom_channels(channel_input: str | list[int]) -> list[int]:
    """دالة جديدة: لتحويل النص المدخل من المستخدم (مثل "36,40,52,100") إلى قائمة أرقام قنوات."""
    if isinstance(channel_input, list):
        return sorted(list(set(channel_input)))
    
    if not channel_input or not channel_input.strip():
        return []

    channels = set()
    # تقسيم المدخلات بحسب الفاصلة
    parts = channel_input.replace(";", ",").split(",")
    for part in parts:
        part = part.strip()
        if "-" in part:
            # لدعم إدخال مجال قنوات مثل "36-48"
            try:
                start, end = map(int, part.split("-"))
                channels.update(range(start, end + 1))
            except ValueError:
                continue
        elif part.isdigit():
            channels.add(int(part))

    return sorted(list(channels))


def scan_hop_order(channels: list[int]) -> list[int]:
    """Reorder a channel set into scan-priority order."""
    priority = [c for c in _PRIORITY_2G if c in channels]
    rest_2g = [c for c in channels if c <= 14 and c not in _PRIORITY_2G]
    band_5g = [c for c in channels if c > 14]
    return priority + rest_2g + band_5g


def _split_bands(channels: list[int]) -> tuple[list[int], list[int]]:
    """Sorted, de-duped (2.4 GHz ≤14, 5 GHz >14) split of a channel set."""
    chs = sorted(set(channels))
    return [c for c in chs if c <= 14], [c for c in chs if c > 14]


def _compress_runs(channels: list[int], step: int) -> str:
    """Collapse a channel list into ``a-b, c, d-e``."""
    chs = sorted(channels)
    if not chs:
        return ""
    runs: list[tuple[int, int]] = []
    start = prev = chs[0]
    for c in chs[1:]:
        # التعديل هنا: السماح بقفزات مختلفة في الـ 5GHz بدلاً من اشتراط الخطوة 4 فقط
        if c == prev + step or (step == 4 and (c - prev) % 4 == 0):
            prev = c
        else:
            runs.append((start, prev))
            start = prev = c
    runs.append((start, prev))
    return ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in runs)


def band_label(channels: list[int]) -> str:
    """Bands present in a channel set."""
    ch_24, ch_5 = _split_bands(channels)
    parts = []
    if ch_24:
        parts.append("2.4 GHz")
    if ch_5:
        parts.append("5 GHz")
    return " + ".join(parts)


def band_ranges(channels: list[int]) -> list[tuple[str, str]]:
    """Per-band ``(name, compressed_ranges)`` for each band present."""
    ch_24, ch_5 = _split_bands(channels)
    out: list[tuple[str, str]] = []
    if ch_24:
        out.append(("2.4 GHz", _compress_runs(ch_24, 1)))
    if ch_5:
        out.append(("5 GHz", _compress_runs(ch_5, 4)))
    return out