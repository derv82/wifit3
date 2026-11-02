"""Headless scanner benchmark: synthetic APs mutating at 15 Hz, on a fixed terminal size.

Counts rich render_lines calls and wall time per tick. Works against both the DataTable
scanner and the APTable one, so it can be run on either revision for an A/B.

    uv run python scripts/ui/bench_scanner.py [n_aps] [n_ticks]
"""
import asyncio
import random
import sys
import time
from unittest.mock import patch

from rich.console import Console
from textual.app import App

from wifit3.models import AccessPoint
from wifit3.persist.vault import Vault
from wifit3.ui.screens.scanner import ScannerView

TERMINAL = (179, 52)          # matches the phone profiling geometry
CHANNELS = [1, 6, 11, 36, 40, 44, 48, 149, 153, 157, 161]


class _FakeIface:
    def __init__(self):
        self.supported_channels = CHANNELS
        self.current_channel = 1
        self.chipset = "bench"

    async def stop_hopping(self):
        pass

    async def start_hopping(self, channels=None, interval=0.25):
        pass


class _FakeArray:
    def __init__(self, aps):
        self.access_points = {ap.bssid: ap for ap in aps}
        self.clients = {}
        self.forged_macs = set()
        self.supported_channels = CHANNELS
        self.members = [_FakeIface()]

    def get_access_points(self, include_eviltwin=True):
        return list(self.access_points.values())

    async def start_hopping(self, channels=None, interval=0.25):
        pass

    async def stop_hopping(self):
        pass


class _Host(App):
    def __init__(self, array):
        super().__init__()
        self.array = array
        self.pbc_enabled = False
        self.vault = Vault()

    def persist_config(self):
        pass

    def on_mount(self):
        self.push_screen(ScannerView())


def _make_aps(count):
    rng = random.Random(1234)
    aps = []
    for i in range(count):
        ap = AccessPoint(
            bssid=f"02:00:00:00:{i // 256:02x}:{i % 256:02x}",
            ssid=f"Network-{i:03d}",
            channel=CHANNELS[i % len(CHANNELS)],
            akms=["PSK"] if i % 3 else [],
            encryption="WPA2" if i % 3 else "OPEN",
            beacons=rng.randint(10, 400),
            wps=bool(i % 4),
        )
        ap.signal_by_card = {"card0": -30 - (i % 60)}
        aps.append(ap)
    return aps


async def main(n_aps, n_ticks):
    aps = _make_aps(n_aps)
    rng = random.Random(99)
    app = _Host(_FakeArray(aps))

    counter = {"n": 0}
    original = Console.render_lines

    def counting(self, *args, **kwargs):
        counter["n"] += 1
        return original(self, *args, **kwargs)

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause(0)
        scanner = app.screen
        scanner.refresh_table()
        await pilot.pause()

        counter["n"] = 0
        started = time.perf_counter()
        with patch.object(Console, "render_lines", counting):
            for tick in range(n_ticks):
                # One hop dwell hears ~1/11th of the APs; those beacon and jitter.
                channel = CHANNELS[(tick // 4) % len(CHANNELS)]
                now = time.time()
                for ap in aps:
                    if ap.channel != channel:
                        continue
                    ap.beacons += 1
                    ap.last_seen = now
                    ap.signal_by_card["card0"] = max(-90, min(-25, ap.signal + rng.choice((-1, 0, 1))))
                scanner.refresh_table()
                await pilot.pause(0)
        elapsed = time.perf_counter() - started

    print(f"aps={n_aps} ticks={n_ticks} terminal={TERMINAL[0]}x{TERMINAL[1]}")
    print(f"  render_lines calls : {counter['n']:>8}  ({counter['n'] / n_ticks:.1f} per tick)")
    print(f"  wall time          : {elapsed:>8.3f}s  ({elapsed / n_ticks * 1000:.2f} ms per tick)")


if __name__ == "__main__":
    n_aps = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    n_ticks = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    asyncio.run(main(n_aps, n_ticks))
