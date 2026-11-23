"""Focus v2: the status bar skips unchanged work; the LED art breathes and flickers."""
import types
from unittest.mock import Mock

from textual.app import App

from wifit3.ui import focus_model as fm
from wifit3.ui.screens.focus_v2 import FocusViewV2
from wifit3.ui.screens.focus_v2.art import BreathingArt, art_size, breathe


class _Host(App):
    """Minimal host: push the v2 screen straight in (no device manager)."""
    target_ap = None
    array = None
    def on_mount(self) -> None:
        self.push_screen(FocusViewV2())


async def test_status_layout_skips_unchanged_button_measurement(monkeypatch):
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0)
        screen = app.screen
        screen._tick_timer.pause()
        calls = 0
        original = type(screen.query_one("#back")).get_content_width

        def count_measurement(button, *args):
            nonlocal calls
            calls += 1
            return original(button, *args)

        monkeypatch.setattr(type(screen.query_one("#back")), "get_content_width", count_measurement)
        screen._status_layout_inputs = ()
        screen._balance_status()
        measured_once = calls
        screen._balance_status()

        assert measured_once > 0
        assert calls == measured_once


async def test_status_footer_skips_identical_markup(monkeypatch):
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0)
        screen = app.screen
        screen._tick_timer.pause()
        screen._target_ap = types.SimpleNamespace(pmf_posture=None)
        status_under_dash = Mock(return_value=["[cyan]Passive listening[/]"])
        monkeypatch.setattr(fm, "status_under_dash", status_under_dash)
        dashboard = screen.query_one("#dashboard")
        dashboard.set_footer = Mock()

        screen._refresh_status_footer()
        screen._refresh_status_footer()

        assert status_under_dash.call_count == 2
        dashboard.set_footer.assert_called_once()
        screen._target_ap = None


def test_dashboard_rows_and_rate_vs_count():
    # WPA family: beacon + data + eapol + inject + deauth.
    rows = fm.dashboard_rows(types.SimpleNamespace(encryption="WPA2"))
    assert len(rows) == 5
    as_rate = {r.key: r.as_rate for r in rows}
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
