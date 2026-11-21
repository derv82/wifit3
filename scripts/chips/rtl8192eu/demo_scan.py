"""Drive the real wifit3 TUI headlessly to prove the RTL8192EU works end-to-end in the app:
launch -> Splash discovers the card -> START brings it up -> Scanner hops and finds APs.
Saves an SVG screenshot of the Scanner and prints the discovered access points."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from wifit3.ui.app import WifiteApp


async def drive(scan_seconds: float, out_svg: str) -> int:
    app = WifiteApp(cli_log_level="quiet")
    async with app.run_test(size=(150, 48)) as pilot:
        # 1. Wait for the Splash to discover the card.
        for _ in range(60):
            await pilot.pause(0.5)
            screen = app.screen
            if getattr(screen, "_devices", None):
                print(f"Splash found: {[str(d) for d in screen._devices]}")
                break
        else:
            print("no device discovered on Splash")
            return 1

        # 2. START (single card auto-targets) -> bring-up -> Scanner.
        await pilot.press("enter")

        # 3. Let the Scanner hop channels and populate.
        print(f"bringing up + scanning for {scan_seconds:.0f}s...")
        deadline = asyncio.get_event_loop().time() + scan_seconds
        while asyncio.get_event_loop().time() < deadline:
            await pilot.pause(1.0)

        # 4. Screenshot the current screen (the Scanner) and report APs.
        await pilot.pause()
        app.save_screenshot(out_svg)
        aps = list(app.array.get_access_points(include_eviltwin=False)) if app.array else []
        print(f"screenshot: {out_svg}")
        print(f"screen: {type(app.screen).__name__}")
        print(f"access points discovered: {len(aps)}")
        for ap in sorted(aps, key=lambda a: (a.channel or 0, a.bssid))[:25]:
            ssid = ap.ssid or "<hidden>"
            print(f"  ch{ap.channel or '?':>3}  {ap.bssid}  {ap.signal:>4} dBm  {ssid}")
        return 0 if aps else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Headless wifit3 TUI demo on the RTL8192EU")
    parser.add_argument("--seconds", type=float, default=25.0)
    parser.add_argument("--out", default="/tmp/wifit3_rtl8192eu_scanner.svg")
    args = parser.parse_args()
    return asyncio.run(drive(args.seconds, args.out))


if __name__ == "__main__":
    raise SystemExit(main())
