"""Geometry contract for the Focus v2 shell: the layout half we can verify
without a human eyeball (placement / no-overlap / width-cap / band-height ladder),
plus that the green-LED breathe actually changes the art. Aesthetics ("does it
look good") stay the human's call, fed by the exported SVGs.

Sizes are pinned headless via ``run_test(size=...)``: no real terminal."""
import pytest
import pytest_asyncio
from textual.app import App
from textual.widgets import Button

from wifit3.ui.screens.focus_v2 import FocusViewV2
from wifit3.ui.screens.focus_v2.art import art_size, breathe

_TOPBAR_H = 3
_CHROME_H = 2          # Header (1 row) + Footer (1 row)
_CENTER_MAX, _CENTER_MIN, _BOTTOM_MIN = 13, 7, 6


class _Host(App):
    """Minimal host: push the v2 screen straight in (no device manager)."""
    def on_mount(self) -> None:
        self.push_screen(FocusViewV2())


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def layout_host():
    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0)
        yield app.screen


@pytest.mark.parametrize("w,h", [(80, 24), (80, 30), (100, 35), (120, 40)])
async def test_layout_geometry(w, h):
    app = _Host()
    async with app.run_test(size=(w, h)) as pilot:
        await pilot.pause(0)
        scr = app.screen

        def reg(sel):
            return scr.query_one(sel).region

        card, dash, router = reg("#card"), reg("#dashboard"), reg("#router")
        # Endpoints pinned at the art width (20); dashboard fills the middle. On wide
        # terminals the mid row gets symmetric side padding (none at 80 cols).
        pad = max(0, round((w - 80) * 0.4))
        assert card.width == 20 and router.width == 20
        assert card.x == pad and card.right == dash.x
        assert dash.right == router.x and router.right == w - pad
        assert dash.width == w - 2 * pad - 40

        log, clients = reg("#log"), reg("#clients")
        # Clients is a fixed exact-fit column; log takes the rest; no overlap.
        assert clients.width == 40
        assert log.x == 0 and log.right == clients.x and clients.right == w

        header, footer = reg("Header"), reg("Footer")
        top, mid, bot = reg("#topbar"), reg("#mid"), reg("#bottom")
        assert header.y == 0 and header.height == 1
        assert footer.bottom == h and footer.height == 1
        assert top.y == header.bottom and top.height == _TOPBAR_H
        assert top.bottom == mid.y and mid.bottom == bot.y and bot.bottom == footer.y
        avail = h - _TOPBAR_H - _CHROME_H
        expected_center = min(_CENTER_MAX, max(_CENTER_MIN, avail - _BOTTOM_MIN))
        assert mid.height == expected_center
        assert bot.height == avail - expected_center


@pytest.mark.asyncio(loop_scope="module")
async def test_topbar_is_the_action_area_and_card_has_no_buttons(layout_host):
    scr = layout_host
    # Back button + the full conditional attack set (6 derive_buttons ids +
    # the transient btn-stop-pbc, all shown/hidden per target/tick) live in the
    # top action area; none remain in the card column.
    assert len(scr.query("#topbar Button")) == 8
    for bid in ("btn-gen-ivs", "btn-chop", "btn-pmkid", "btn-deauth", "btn-wps-pin",
                "btn-eviltwin", "btn-stop-pbc"):
        assert scr.query_one(f"#topbar #{bid}", Button) is not None
    assert len(scr.query("#card Button")) == 0


@pytest.mark.asyncio(loop_scope="module")
async def test_dashboard_rows_and_rate_vs_count(layout_host):
    dash = layout_host.query_one("#dashboard")
    assert len(dash._rows) == 5
    as_rate = {r.key: r.as_rate for r in dash._rows}
    # eapol reads as a recent count (a handshake is ~4 frames); the rest /s.
    assert as_rate["eapol"] is False
    assert all(as_rate[k] for k in ("beacon", "data", "inject", "deauth"))


def test_breathe_changes_green_leds():
    dark = breathe("focus-card.ans", 0.0)
    bright = breathe("focus-card.ans", 0.5)
    # Same glyphs + geometry: only the LED cells' colour changes.
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


def test_flicker_spikes_above_the_breathe_band():
    """A packet flicker must be unmistakably brighter than the dim idle breathe,
    so activity reads as a spike, not a slightly-brighter glow."""
    from wifit3.ui.screens.focus_v2.art import (
        _BREATHE_HI, _BREATHE_LO, _FLICKER_GREEN, _breathe_green,
    )
    assert _breathe_green(0.0) == _BREATHE_LO
    assert _breathe_green(0.5) == _BREATHE_HI
    assert _FLICKER_GREEN > _BREATHE_HI


def test_flicker_state_machine_caps_rate_then_decays():
    """pulse() lights ON for one frame, then a refractory forces it dim; a pulse
    arriving mid-refractory only arms the *next* blink (no strobe). With no more
    pulses the LED settles back to idle (breathe only)."""
    from wifit3.ui.screens.focus_v2.art import BreathingArt

    art = BreathingArt("focus-card.ans")          # not mounted, drive it by hand
    assert art._blink == "idle"
    art.pulse()
    assert art._blink == "on"                     # bright this frame
    art._advance_blink()
    assert art._blink == "refractory"             # forced dim …
    art.pulse()                                   # … a fresh pulse can't strobe it
    assert art._blink == "refractory"
    art._advance_blink()
    art._advance_blink()                          # refractory done → pending → on
    assert art._blink == "on"
    # No further pulses → on → refractory → idle.
    art._advance_blink()
    assert art._blink == "refractory"
    art._advance_blink()
    art._advance_blink()
    assert art._blink == "idle"
