"""Geometry contract for the Focus v2 shell — the layout half we can verify
without a human eyeball (placement / no-overlap / width-cap / band-height ladder),
plus that the green-LED breathe actually changes the art. Aesthetics ("does it
look good") stay the human's call, fed by the exported SVGs.

Sizes are pinned headless via ``run_test(size=...)`` — no real terminal."""
import pytest
from textual.app import App

from wifit3.ui.screens.focus_v2 import FocusViewV2
from wifit3.ui.screens.focus_v2.art import art_size, breathe

_TOPBAR_H = 3
_CENTER_MAX, _CENTER_MIN, _BOTTOM_MIN = 13, 7, 6


class _Host(App):
    """Minimal host: push the v2 screen straight in (no device manager)."""
    def on_mount(self) -> None:
        self.push_screen(FocusViewV2())


@pytest.mark.parametrize("w,h", [(80, 24), (80, 30), (100, 35), (120, 40)])
async def test_layout_geometry(w, h):
    app = _Host()
    async with app.run_test(size=(w, h)) as pilot:
        await pilot.pause()
        scr = app.screen

        def reg(sel):
            return scr.query_one(sel).region

        card, flow, router = reg("#card"), reg("#flow"), reg("#router")
        # Endpoints pinned at the art width (20); flow fills the middle; the row
        # tiles edge-to-edge with no overlap and no gap.
        assert card.width == 20 and router.width == 20
        assert card.x == 0 and card.right == flow.x
        assert flow.right == router.x and router.right == w
        assert flow.width == w - 40

        log, clients = reg("#log"), reg("#clients")
        # Clients is a fixed exact-fit column; log takes the rest; no overlap.
        assert clients.width == 40
        assert log.x == 0 and log.right == clients.x and clients.right == w

        top, mid, bot = reg("#topbar"), reg("#mid"), reg("#bottom")
        # Three stacked bands cover the full height with no overlap; the mid band
        # caps at _CENTER_MAX (full 2-row sparklines), the bottom takes the rest.
        assert top.y == 0 and top.height == _TOPBAR_H
        assert top.bottom == mid.y and mid.bottom == bot.y and bot.bottom == h
        avail = h - _TOPBAR_H
        expected_center = min(_CENTER_MAX, max(_CENTER_MIN, avail - _BOTTOM_MIN))
        assert mid.height == expected_center
        assert bot.height == avail - expected_center


async def test_topbar_is_the_action_area_and_card_has_no_buttons():
    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        scr = app.screen
        # Back button + the 3 attack buttons all live in the top action area;
        # none remain in the card column.
        assert len(scr.query("#topbar Button")) == 4
        assert len(scr.query("#card Button")) == 0


async def test_flow_rows_and_rate_vs_count():
    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        flow = app.screen.query_one("#flow")
        assert len(flow._rows) == 5
        as_rate = {r.key: r.as_rate for r in flow._rows}
        # eapol reads as a recent count (a handshake is ~4 frames); the rest /s.
        assert as_rate["eapol"] is False
        assert all(as_rate[k] for k in ("beacon", "data", "inject", "deauth"))


def test_breathe_changes_green_leds():
    dark = breathe("focus-card.ans", 0.0)
    bright = breathe("focus-card.ans", 0.5)
    # Same glyphs + geometry — only the LED cells' colour changes.
    assert dark.plain == bright.plain
    assert art_size("focus-card.ans") == (20, 10)

    def led_greens(text):
        out = set()
        for span in text.spans:
            for col in (getattr(span.style, "color", None), getattr(span.style, "bgcolor", None)):
                trip = col.triplet if col is not None else None
                if trip is not None and trip.red == 0 and trip.blue == 0:
                    out.add(trip.green)
        return out

    # The bright frame must push the LED green above the dark (0,128,0) baseline.
    assert max(led_greens(bright)) > max(led_greens(dark))


def test_art_pure_black_is_transparent():
    """The .ans negative space is pure black; the loader must drop it so the art
    blends into the theme surface instead of painting a black rectangle."""
    from wifit3.ui.ansi_art import is_black
    from wifit3.ui.screens.focus_v2.art import _transparent

    for name in ("focus-card.ans", "focus-ap.ans"):
        for span in _transparent(name).spans:
            assert not is_black(span.style.color)
            assert not is_black(span.style.bgcolor)
