"""Dev harness: render FocusViewV2 headless at fixed sizes, export an SVG for
the human eyeball, and dump each region's rectangle for geometry checks.

    uv run python scripts/ui/shoot_focus_v2.py [out_dir]

No hardware, no real terminal — Textual's run_test(size=...) pins exact
dimensions, which is the whole autonomous-verification story for the redesign.
"""
import asyncio
import io
import sys
from pathlib import Path

from rich.console import Console
from textual.app import App

from wifit3.ui.screens.focus_v2 import FocusViewV2

sys.stdout.reconfigure(encoding="utf-8")  # block glyphs vs Windows cp1252 stdout

SIZES = [(80, 24), (80, 30), (100, 35), (120, 40), (180, 45)]
REGIONS = ["#topbar", "#mid", "#bottom", "#card", "#flow", "#router", "#log", "#clients"]


class ShooterApp(App):
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


async def shoot(w: int, h: int, out_dir: Path) -> None:
    app = ShooterApp()
    async with app.run_test(size=(w, h)) as pilot:
        await pilot.pause()
        # Prime the flow channel with history so the sparklines are populated
        # (a live run fills these over ~30 s; the shot shouldn't show them empty).
        flow = app.screen.query_one("#flow")
        for _ in range(90):
            flow._tick()
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
