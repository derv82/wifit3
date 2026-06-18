"""Dev harness: render FocusViewV2 headless at fixed sizes, export an SVG for
the human eyeball, and dump each region's rectangle for geometry checks.

    uv run python scripts/ui/shoot_focus_v2.py [out_dir]

Drives a real mock ``WlanInterface`` (no hardware, no real terminal) so the
shots exercise the LIVE wiring — real headline, signal bar, clients, encryption
line, captured-handshake log — rather than the fake demo snapshot. Textual's
run_test(size=...) pins exact dimensions, which is the whole autonomous-
verification story for the redesign.
"""
import asyncio
import io
import sys
import time
from pathlib import Path

from rich.console import Console
from textual.app import App

from wifit3.ui.screens.focus_v2 import FocusViewV2
from wifit3.wlan.interface import WlanInterface

sys.stdout.reconfigure(encoding="utf-8")  # block glyphs vs Windows cp1252 stdout

SIZES = [(80, 24), (80, 30), (100, 35), (120, 40), (180, 45)]
REGIONS = ["#topbar", "#mid", "#bottom", "#card", "#flow", "#router", "#log", "#clients"]

_BSSID = "a8:fc:b7:0e:1d:42"
_CLIENTS = ["fa:11:22:33:44:aa", "04:2e:c1:51:43:b8", "9c:b6:d0:1a:2b:3c"]


class MockDriver:
    async def set_channel(self, ch, scan=False):
        return True

    def register_rx_callback(self, cb):
        pass


def _beacon(bssid, ssid, ch):
    return {
        "type": "beacon", "bssid": bssid, "ssid": ssid, "channel": ch,
        "rssi": -71, "encryption": "WPA2", "akms": ["PSK"],
        "pairwise_cipher": "CCMP", "raw": b"\xff-beacon-raw",
    }


def _eapol(bssid, client, msg_num, replay, *, to_ap, pmkid=None):
    return {
        "type": "eapol", "bssid": bssid, "rssi": -71,
        "source": client if to_ap else bssid,
        "dest": bssid if to_ap else client,
        "raw": bytes([msg_num]) + b"-eapol-" + replay,
        "eapol_replay_counter": replay, "eapol_msg_num": msg_num,
        "eapol_nonce": b"\x01" * 32, "eapol_mic": b"\x02" * 16,
        "eapol_key_data_len": 0, "eapol_payload": bytes(120), "eapol_pmkid": pmkid,
    }


def _build_iface() -> tuple[WlanInterface, object]:
    """A WPA2 target mid-capture: 3 clients, one completed handshake + PMKID."""
    iface = WlanInterface(MockDriver(), "wlan0", "Alfa AWUS036H")
    iface._on_frame_parsed(_beacon(_BSSID, "NETGEAR91", 6))
    ap = iface.access_points[_BSSID]
    for i, c in enumerate(_CLIENTS):
        for _ in range(10 * (i + 1)):
            iface._on_frame_parsed({"type": "data", "bssid": _BSSID, "source": c,
                                    "dest": _BSSID, "rssi": -67 - i * 6, "raw": b"d"})
    replay = b"\x00" * 8
    iface._on_frame_parsed(_eapol(_BSSID, _CLIENTS[1], 1, replay, to_ap=False, pmkid=b"\xaa" * 16))
    iface._on_frame_parsed(_eapol(_BSSID, _CLIENTS[1], 2, replay, to_ap=True))
    return iface, ap


class ShooterApp(App):
    def __init__(self, iface, ap):
        super().__init__()
        self.active_interface = iface
        self.target_ap = ap

    def on_mount(self) -> None:
        self.push_screen(FocusViewV2())


def screen_text(app: App) -> str:
    """The screen as a plain-text grid (same compositor render as the SVG export,
    but export_text instead of export_svg) — lets the agent read the render."""
    w, h = app.size
    console = Console(width=w, height=h, file=io.StringIO(), force_terminal=True,
                      color_system="truecolor", record=True, legacy_windows=False,
                      safe_box=False)
    render = app.screen._compositor.render_update(
        full=True, screen_stack=app._background_screens, simplify=False)
    console.print(render)
    return console.export_text()


def _prime(app: App, iface, ap) -> None:
    """Populate the live sparklines + signal bar so a snapshot isn't empty (a
    real run fills these over ~30 s). Seeds the beacon-rate window for a healthy
    signal bar, then pumps packet_stats so the flow channel's deltas are lively."""
    focus = app.screen
    focus._beacon_samples.clear()
    focus._beacon_samples.append((time.time() - 2.0, ap.beacons - 14))  # ~7 beacons/s
    flow = focus.query_one("#flow")
    bumps = {"beacon": 3, "data": 80, "eapol": 0, "inject": 12, "deauth": 5}
    for i in range(90):
        for cls, base in bumps.items():
            n = max(0, base + (i % 7) - 3 + (8 if cls in ("inject", "deauth") and i % 11 < 2 else 0))
            for _ in range(n):
                iface.packet_stats.record_rx(_BSSID, cls)
        flow._tick()
    focus._tick()


async def shoot(w: int, h: int, out_dir: Path) -> None:
    iface, ap = _build_iface()
    app = ShooterApp(iface, ap)
    async with app.run_test(size=(w, h)) as pilot:
        await pilot.pause()
        _prime(app, iface, ap)
        await pilot.pause()
        name = f"focus_v2_{w}x{h}.svg"
        app.save_screenshot(path=str(out_dir), filename=name)
        print(f"\n=== {w}x{h}  ->  {name} ===")
        scr = app.screen
        for sel in REGIONS:
            try:
                r = scr.query_one(sel).region
                print(f"  {sel:<9} x={r.x:>3} y={r.y:>3} w={r.width:>3} h={r.height:>3}"
                      f"  (right={r.right:>3} bottom={r.bottom:>3})")
            except Exception as e:  # noqa: BLE001 — dev tool, surface anything
                print(f"  {sel:<9} ERROR {e}")
        print(f"--- {w}x{h} text render ---")
        print(screen_text(app))


async def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "focus_v2_shots"
    out_dir.mkdir(parents=True, exist_ok=True)
    for w, h in SIZES:
        await shoot(w, h, out_dir)
    print(f"\nSVGs in: {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
