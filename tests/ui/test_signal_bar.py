"""The signal bar's only contract is its width: the caller sizes it to fill its column
and right-aligns the dBm beside it, so a branch that returns a different cell count
tears the readout off the edge. The dead-AP branch used to return width+2."""
from __future__ import annotations

import pytest

from wifit3.ui.signal_bar import render_signal_bar

WIDTHS = [4, 5, 6, 10, 17, 40]


@pytest.mark.parametrize("width", WIDTHS)
def test_every_branch_fills_exactly_the_requested_width(width: int) -> None:
    """None=warming, ~0=dead, mid=live. All three must honour width identically."""
    assert render_signal_bar(None, width=width).cell_len == width
    assert render_signal_bar(0.01, width=width, pulse=1.0).cell_len == width
    assert render_signal_bar(9.8, width=width).cell_len == width


@pytest.mark.parametrize("width", WIDTHS)
def test_dead_branch_leaves_room_for_its_two_cell_prefix(width: int) -> None:
    """The cross plus its gap occupy two cells, so the track is width-2, not width."""
    dead = render_signal_bar(0.0, width=width, pulse=0.0)
    assert dead.plain.startswith("╳ "), "dead bar must lead with the cross and a gap"


def test_dead_bar_pulses_without_changing_its_width() -> None:
    """pulse only varies the style; a heartbeat that respanned the bar would jitter."""
    lo = render_signal_bar(0.01, width=12, pulse=0.0).cell_len
    hi = render_signal_bar(0.01, width=12, pulse=1.0).cell_len
    assert lo == hi == 12