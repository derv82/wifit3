"""Drive the REAL WifiteApp headless to time when APs first appear after START.

Reproduces the user's symptom path faithfully: the full Textual event loop, every
UI timer (15 Hz table refresh, sort, PBC poll), the scanner DataTable render, the
RxReaderThread call_soon_threadsafe decode-on-loop, and start_hopping @ 0.25 s.

It records, in wall-clock time from the moment the scanner mounts, when the first
AP appears and the per-AP first-seen channel — to localise "no APs until ~3.6 s on
ch36" to either the driver (no) or event-loop contention at scanner startup (likely).

Run: WIFIT3_RTL8822=dkms uv run python scripts/rtl8822bu_dkms/ui_startup_probe.py
(passive; never injects)
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("WIFIT3_RTL8822", "dkms")
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from wifit3.ui.app import WifiteApp


async def main() -> int:
    app = WifiteApp()
    async with app.run_test(size=(120, 40)) as pilot:
        # 1) wait for the splash to discover the card
        splash = app.screen
        iface = None
        for _ in range(20):
            await pilot.pause(0.3)
            interfaces = await app.device_manager.refresh()
            cards = [i for i in interfaces if i.pid == 0x0138 and i.vid == 0x2357]
            if cards:
                iface = cards[0]
                break
        if iface is None:
            print("[FAIL] T3U Plus (2357:0138) not found on the bus")
            return 1
        print(f"[*] found {iface.description}; driver={type(iface.driver).__name__}")

        # 2) press START (real worker path: connect -> switch_screen('scanner'))
        t_start = time.perf_counter()
        splash.perform_start(iface)

        # 3) watch active_interface.access_points until/after the scanner is live
        seen_first = {}            # bssid -> (t_rel, channel)
        scanner_mounted_at = None
        first_ap_at = None
        deadline = time.perf_counter() + 22.0
        last_print = 0.0
        while time.perf_counter() < deadline:
            await pilot.pause(0.1)
            now = time.perf_counter()
            if app.active_interface is not None and scanner_mounted_at is None \
                    and type(app.screen).__name__ == "ScannerView":
                scanner_mounted_at = now
                print(f"[*] scanner live at t+{now - t_start:4.2f}s "
                      f"(hopping={getattr(app.active_interface, '_is_hopping', '?')})")
            iface_live = app.active_interface
            if iface_live is not None:
                for bssid, ap in list(iface_live.access_points.items()):
                    if bssid not in seen_first:
                        rel = now - (scanner_mounted_at or t_start)
                        seen_first[bssid] = (rel, ap.channel)
                        if first_ap_at is None:
                            first_ap_at = rel
                            print(f"[*] FIRST AP at t+{rel:4.2f}s (since scanner mount) "
                                  f"ch{ap.channel} {ap.ssid!r}")
                if now - last_print >= 1.0:
                    last_print = now
                    n2 = sum(1 for _, c in seen_first.values() if c <= 14)
                    n5 = sum(1 for _, c in seen_first.values() if c > 14)
                    cur = getattr(iface_live.driver, "_channel", "?")
                    print(f"  t+{now - t_start:5.2f}s  APs={len(seen_first)} "
                          f"(2.4G={n2} 5G={n5})  cur_ch={cur}")

        # 4) timeline: APs grouped by their first-seen channel
        print("\n[TIMELINE] first-seen, sorted by time:")
        for bssid, (rel, ch) in sorted(seen_first.items(), key=lambda kv: kv[1][0])[:40]:
            band = "2.4" if ch <= 14 else "5  "
            print(f"    t+{rel:5.2f}s  ch{ch:>3} [{band}GHz]  {bssid}")
        n2 = sum(1 for _, c in seen_first.values() if c <= 14)
        n5 = sum(1 for _, c in seen_first.values() if c > 14)
        print(f"\n[RESULT] {len(seen_first)} APs total: 2.4 GHz={n2}, 5 GHz={n5}; "
              f"first AP at t+{first_ap_at}s after scanner mount")
        if app.active_interface:
            await app.active_interface.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
