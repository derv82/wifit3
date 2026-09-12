"""Geometry contract for the Focus v2 shell: the layout half we can verify
without a human eyeball (placement / no-overlap / width-cap / band-height ladder),
plus that the green-LED breathe actually changes the art. Aesthetics ("does it
look good") stay the human's call, fed by the exported SVGs.

Sizes are pinned headless via ``run_test(size=...)``: no real terminal."""
import types

import pytest
import pytest_asyncio
from textual.app import App
from textual.widgets import Button

from wifit3.models import AccessPoint, ApIdentity, IdKey, IdSource
from wifit3.ui import focus_model as fm
from wifit3.ui.screens.focus_v2 import FocusViewV2
from wifit3.ui.screens.focus_v2.art import BreathingArt, art_size, breathe

_TOPBAR_H = 3
_CHROME_H = 2          # Header (1 row) + Footer (1 row)
_CENTER_MAX, _CENTER_MIN, _BOTTOM_MIN = 13, 7, 6


class _Host(App):
    """Minimal host: push the v2 screen straight in (no device manager)."""
    target_ap = None
    array = None
    def on_mount(self) -> None:
        self.push_screen(FocusViewV2())


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def layout_host():
    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0)
        yield app.screen


async def test_layout_geometry():
    app = _Host()
    async with app.run_test(size=(80, 24)) as pilot:
        for w, h in [(80, 24), (80, 30), (100, 35), (120, 40)]:
            await pilot.resize_terminal(w, h)
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


async def test_router_identity_button_logs_details_from_keyboard_without_tooltip():
    class _IdentityHost(_Host):
        target_ap = AccessPoint(
            bssid="02:00:00:00:00:01",
            ssid="Router",
            channel=1,
            wps=True,
            identity=ApIdentity(IdSource.WSC_BEACON, manufacturer="MikroTik", model_name="hAP ac²"),
        )

    app = _IdentityHost()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0)
        logs = []
        app.screen._log = logs.append
        identity = app.screen.query_one("#ap-identity", Button)
        assert identity.styles.line_pad == 0
        assert identity.tooltip is None
        art = app.screen.query_one("#router-art", BreathingArt)
        assert art.tooltip is not None
        assert "MikroTik" in str(art.tooltip)
        assert "hAP ac²" in str(art.tooltip)
        assert identity.disabled is False
        identity.focus()
        await pilot.press("enter")
        await pilot.pause(0)
        assert logs[0] == "[bold]Router identity[/bold]"
        assert any("MikroTik" in line for line in logs)
        assert any("├─►" in line for line in logs[:-1])
        assert "└─►" in logs[-1]


async def test_router_endpoint_layout_states():
    app = _Host()
    async with app.run_test(size=(120, 40)):
        # Non-WPS target
        app.target_ap = AccessPoint(bssid="02:00:00:00:00:01", ssid="OpenAir", channel=6, wps=False)
        await app.screen._enter_target()
        identity = app.screen.query_one("#ap-identity", Button)
        probe = app.screen.query_one("#ap-probe", Button)
        assert identity.label.plain == "channel 6"
        assert identity.disabled is True
        assert probe.display is False

        # WPS with prior M1 identity
        ap_m1 = AccessPoint(bssid="02:00:00:00:00:01", ssid="Office", channel=11, wps=True)
        ap_m1.identity.set(IdSource.WSC_M1, IdKey.MANUFACTURER, "Netgear")
        ap_m1.identity.set(IdSource.WSC_M1, IdKey.MODEL_NAME, "Nighthawk X6 R8000")
        app.target_ap = ap_m1
        await app.screen._enter_target()
        assert identity.label.plain == "Netgear Nighthawk X…"  # truncated to 20 chars
        assert identity.disabled is False
        assert probe.display is False

        # WPS without M1 (fallback to channel)
        app.target_ap = AccessPoint(bssid="02:00:00:00:00:01", ssid="EmptyWPS", channel=11, wps=True)
        await app.screen._enter_target()
        assert identity.label.plain == "ch 11"
        assert identity.disabled is True
        assert probe.display is True
        assert probe.label.plain == "🔍"
        assert probe.tooltip == "Probe AP for WPS/WSC attributes"


async def test_router_endpoint_probe_click_and_cancel(monkeypatch):
    import asyncio
    from wifit3.campaigns.probe import ProbeResult

    ap = AccessPoint(
        bssid="02:00:00:00:00:01",
        ssid="ProbeMe",
        channel=6,
        wps=True,
    )

    probe_started = asyncio.Event()
    probe_cancelled = asyncio.Event()

    async def fake_probe(iface, target):
        probe_started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            probe_cancelled.set()
            raise
        return ProbeResult(ok=True)

    import wifit3.ui.screens.focus_v2.screen as screen_mod
    monkeypatch.setattr(screen_mod, "probe_ap", fake_probe)

    from contextlib import asynccontextmanager
    from wifit3.wlan.sink import WlanSink

    class _FakeProbeArray:
        def __init__(self):
            self.members = [types.SimpleNamespace(chipset="rtl8821au", mac_address="00:11:22:33:44:55", current_channel=6)]
            self._sink = WlanSink()

        def select_iface(self, channel):
            return self.members[0]

        async def set_channel(self, ch, scan=False):
            return True

        @asynccontextmanager
        async def claim(self, iface):
            yield iface

        def __getattr__(self, name):
            return getattr(self._sink, name)

    class _ProbeHost(_Host):
        def __init__(self, target_ap):
            super().__init__()
            self.target_ap = target_ap
            self.array = _FakeProbeArray()

    app = _ProbeHost(ap)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0)
        probe = app.screen.query_one("#ap-probe", Button)
        assert probe.label.plain == "🔍"

        # Click probe button to start probe
        probe.press()
        await pilot.pause()
        assert probe_started.is_set()
        assert probe.label.plain == "❌"
        assert probe.tooltip == "Cancel WPS/WSC probe"

        # Verify attack buttons disabled during probing
        wps_btn = app.screen.query_one("#btn-wps-pin", Button)
        assert wps_btn.disabled is True

        # Click probe button again (now '❌') to cancel
        probe.press()
        await pilot.pause()
        assert probe_cancelled.is_set()
        assert probe.label.plain == "🔍"
        assert probe.tooltip == "Probe AP for WPS/WSC attributes"


async def test_router_endpoint_probe_success_updates_layout_and_logs(monkeypatch):
    from contextlib import asynccontextmanager
    from wifit3.campaigns.probe import ProbeResult
    from wifit3.wlan.sink import WlanSink

    ap = AccessPoint(
        bssid="02:00:00:00:00:01",
        ssid="ProbeSuccess",
        channel=6,
        wps=True,
    )

    async def fake_probe(iface, target):
        target.identity.set(IdSource.WSC_M1, IdKey.MANUFACTURER, "ASUS")
        target.identity.set(IdSource.WSC_M1, IdKey.MODEL_NAME, "RT-AX88U")
        return ProbeResult(ok=True, source="WSC M1", vendor="ASUS", model="RT-AX88U")

    import wifit3.ui.screens.focus_v2.screen as screen_mod
    monkeypatch.setattr(screen_mod, "probe_ap", fake_probe)

    class _FakeProbeArray:
        def __init__(self):
            self.members = [types.SimpleNamespace(chipset="rtl8821au", mac_address="00:11:22:33:44:55", current_channel=6)]
            self._sink = WlanSink()

        def select_iface(self, channel):
            return self.members[0]

        async def set_channel(self, ch, scan=False):
            return True

        @asynccontextmanager
        async def claim(self, iface):
            yield iface

        def __getattr__(self, name):
            return getattr(self._sink, name)

    class _ProbeHost(_Host):
        def __init__(self, target_ap):
            super().__init__()
            self.target_ap = target_ap
            self.array = _FakeProbeArray()

    app = _ProbeHost(ap)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0)
        logs = []
        app.screen._log = logs.append
        probe = app.screen.query_one("#ap-probe", Button)
        identity = app.screen.query_one("#ap-identity", Button)
        assert probe.display is True

        # Click probe
        probe.press()
        await pilot.pause()

        # On success: probe button hidden, identity label expands to 20 chars
        assert probe.display is False
        assert identity.label.plain == "ASUS RT-AX88U"
        assert identity.disabled is False

        # Identity tree logged to RichLog
        assert any("probe matched" in log for log in logs)
        assert any("Router identity" in log for log in logs)
        assert any("ASUS" in log for log in logs)


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
