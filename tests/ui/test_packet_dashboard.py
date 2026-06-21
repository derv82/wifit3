"""Tests for the PacketDashboard widget.

Two regression guards live here:

1. Auto-height mount. The widget can sit in a fixed-height row with no explicit
   height of its own, so Textual measures its content height via
   ``get_content_height`` → ``Widget._render()``. A repaint helper named
   ``_render`` once shadowed that Textual internal and returned None, crashing
   only on this path (a fixed-height mount never measures content). The mount
   test exercises it end to end so the collision can't return.

2. Encryption gating. The WEP-IV row shows only on WEP targets, the EAPOL row
   only on WPA/WPA2/WPA3 — never both, neither on OPEN.
"""

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.geometry import Size

from wifit3.ui.widgets.packet_dashboard import PacketDashboard
from wifit3.wlan.packet_stats import PacketStats


class _FakeIface:
    def __init__(self):
        self.packet_stats = PacketStats()


class _Harness(App):
    # Mirrors the worst case: the dashboard has NO height of its own, forcing
    # the content-height measurement path that the _render-shadow bug hit.
    CSS = """
    #top-right { height: 8; }
    #panel-activity { width: 1fr; min-width: 0; padding: 0 1; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="top-right"):
            yield PacketDashboard(id="panel-activity")


async def test_mounts_without_height_does_not_crash():
    """Auto-height mount must not raise (the _render-shadow regression)."""
    async with _Harness().run_test() as pilot:
        w = pilot.app.query_one(PacketDashboard)
        # Textual's own measurement path — this is what crashed before.
        h = w.get_content_height(Size(40, 8), Size(120, 24), 40)
        assert h >= 1


async def test_focus_on_renders_classes_and_data():
    iface = _FakeIface()
    bssid = "aa:bb:cc:dd:ee:ff"
    async with _Harness().run_test() as pilot:
        w = pilot.app.query_one(PacketDashboard)
        w.focus_on(iface, bssid, show_wep=False, show_eapol=True)
        for _ in range(8):
            iface.packet_stats.record_rx(bssid, "beacon")
        iface.packet_stats.record_tx(bssid, is_deauth=True)
        w._sample()
        plain = w.render().plain
        # The widget no longer paints its own title — that's a sibling Label in
        # FocusViewV2 now — so it must NOT appear in the widget's own render.
        assert "PACKET ACTIVITY" not in plain
        # Always-on rows are present.
        for label in ("beacon", "data", "inject", "deauth"):
            assert label in plain


async def test_wep_gating_shows_wep_iv_not_eapol():
    async with _Harness().run_test() as pilot:
        w = pilot.app.query_one(PacketDashboard)
        w.focus_on(_FakeIface(), "aa:bb:cc:dd:ee:ff", show_wep=True, show_eapol=False)
        assert list(w._visible_classes()) == ["beacon", "data", "wep_iv", "inject", "deauth"]
        plain = w.render().plain
        assert "wep iv" in plain
        assert "eapol" not in plain


async def test_wpa_gating_shows_eapol_not_wep_iv():
    async with _Harness().run_test() as pilot:
        w = pilot.app.query_one(PacketDashboard)
        w.focus_on(_FakeIface(), "aa:bb:cc:dd:ee:ff", show_wep=False, show_eapol=True)
        assert list(w._visible_classes()) == ["beacon", "data", "inject", "deauth", "eapol"]
        plain = w.render().plain
        assert "eapol" in plain
        assert "wep iv" not in plain


async def test_open_gating_shows_neither():
    async with _Harness().run_test() as pilot:
        w = pilot.app.query_one(PacketDashboard)
        w.focus_on(_FakeIface(), "aa:bb:cc:dd:ee:ff", show_wep=False, show_eapol=False)
        assert list(w._visible_classes()) == ["beacon", "data", "inject", "deauth"]
        plain = w.render().plain
        assert "wep iv" not in plain
        assert "eapol" not in plain


async def test_set_gates_flips_rows_live():
    """The encryption label can upgrade after focus; set_gates updates the rows
    without clearing history."""
    async with _Harness().run_test() as pilot:
        w = pilot.app.query_one(PacketDashboard)
        w.focus_on(_FakeIface(), "aa:bb:cc:dd:ee:ff", show_wep=True, show_eapol=False)
        assert "wep_iv" in w._visible_classes()
        w.set_gates(show_wep=False, show_eapol=True)
        vis = list(w._visible_classes())
        assert "eapol" in vis and "wep_iv" not in vis


async def test_idle_when_no_interface():
    async with _Harness().run_test() as pilot:
        w = pilot.app.query_one(PacketDashboard)
        w.focus_on(None, None)
        # Idle render still lists the always-on classes (dim), just no data.
        plain = w.render().plain
        assert "beacon" in plain
