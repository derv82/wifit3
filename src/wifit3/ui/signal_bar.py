"""Reception-quality bar: a smooth beacons/s meter for the live UI.

An AP is fully heard at ~9.77 beacons/s (one per 102.4 ms TBTT), so beacons/s is
really a *reception-quality* signal. This renders it as a horizontal meter built
from left-fractional eighth-block glyphs (▏▎▍▌▋▊▉█) — 8 sub-steps per character,
fine enough to track the windowed decimal rate without visibly stepping.

Colour is **positional**: each cell carries a fixed red→green hue and only the
fill *length* follows the rate. A weak AP's bar reaches only the reds; a strong
one pushes into green — and since no cell ever changes hue, the bar never strobes
as the rate wobbles (the reason the plain-number display refused to colour
anything but a near-dead rate).
"""
from __future__ import annotations

from typing import Optional, Tuple

from rich.text import Text

# 0/8 .. 8/8 of a cell, filled from the left.
_EIGHTHS = " ▏▎▍▌▋▊▉█"

# One beacon per 102.4 ms TBTT — the rate at which an AP is fully heard.
FULL_SCALE_RATE = 9.77

# Brightness of the unfilled track: a dim ghost of each cell's fill hue, so the
# bar's full width (the headroom) stays visible.
_EMPTY = 0.22


def _hue(t: float) -> Tuple[int, int, int]:
    """Positional gradient: red (t=0) → yellow → green (t=1)."""
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    if t < 0.5:
        return (255, int(t * 2 * 255), 0)            # red → yellow
    return (int((1 - (t - 0.5) * 2) * 255), 205, 0)  # yellow → green


def _dim(rgb: Tuple[int, int, int], k: float) -> str:
    r, g, b = rgb
    return f"rgb({int(r * k)},{int(g * k)},{int(b * k)})"


def render_signal_bar(
    rate: Optional[float],
    *,
    width: int = 10,
    full_scale: float = FULL_SCALE_RATE,
    pulse: float = 1.0,
) -> Text:
    """Render the meter for ``rate`` beacons/s.

    ``rate=None`` → warming up (faint track). ``rate≈0`` → dead: a dim,
    heartbeat-pulsing track plus a red ✕. ``pulse`` (0..1) drives that heartbeat
    and is ignored for a live bar.
    """
    bar = Text(no_wrap=True)
    span = max(1, width - 1)

    if rate is None:
        for i in range(width):
            bar.append("█", style=_dim(_hue(i / span), _EMPTY))
        return bar

    if rate <= 0.05:
        beat = _EMPTY * (0.5 + 0.5 * pulse)
        # Red ╳ on the left — where the fading bar's last (red) cell sits before
        # it dies. The box-drawing cross fills the cell, so it's level with the
        # blocks (a centred ✕ glyph floats high).
        bar.append("╳", style=f"bold rgb({int(110 + 145 * pulse)},0,0)")
        bar.append(" ")
        for i in range(width):
            bar.append("█", style=_dim(_hue(i / span), beat))
        return bar

    filled = min(1.0, rate / full_scale) * width
    full = int(filled)
    eighths = int(round((filled - full) * 8))
    if eighths == 8:
        full, eighths = full + 1, 0

    for i in range(width):
        rgb = _hue(i / span)
        if i < full:
            bar.append("█", style=_dim(rgb, 1.0))
        elif i == full and eighths:
            # Partial tip: bright fill on the left eighths, dim track behind.
            bar.append(_EIGHTHS[eighths], style=f"{_dim(rgb, 1.0)} on {_dim(rgb, _EMPTY)}")
        else:
            bar.append("█", style=_dim(rgb, _EMPTY))
    return bar
