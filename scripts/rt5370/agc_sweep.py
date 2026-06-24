"""THROWAWAY: sweep BBP66 (AGC/VGC) in place on one channel and measure a target AP's
beacon rate at each value, to test the front-end-overload hypothesis on a very-close AP.

Relative measurement (same chip state across all BBP66 values), so warm-vs-cold doesn't
matter — but we force the monitor RX filter so a warm reattach (which skips enable_monitor)
still receives. Higher BBP66 = less gain (back off → relieve compression); the kernel's
link tuner adds +0x10 when RSSI > -80 dBm but never runs it in monitor mode.

    uv run python scripts/rt5370/agc_sweep.py --channel 9 --bssid 1c:b7:2c:38:0a:80 --window 15
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wifit3.wlan.manager import WlanDeviceManager  # noqa: E402
from wifit3.wlan.packet import WlanFrameParser  # noqa: E402
from wifit3.chips.rt5370 import mac, monitor  # noqa: E402


class Collector:
    def __init__(self) -> None:
        self.events: list[tuple[float, str, int]] = []

    def __call__(self, raw: bytes, rssi: int, ts: float) -> None:
        try:
            p = WlanFrameParser.parse_80211_frame(raw, rssi)
        except Exception:
            return
        if not p or p.get("type") != "beacon":
            return
        b = (p.get("bssid") or "").lower()
        if b:
            self.events.append((time.monotonic(), b, rssi))


async def main(args) -> int:
    target = args.bssid.lower()
    mgr = WlanDeviceManager()
    ifaces = await mgr.refresh()
    if not ifaces:
        print("[-] no device")
        return 1
    iface = ifaces[0]
    print(f"[*] bring up {iface.name} ...", file=sys.stderr)
    await iface.connect()
    drv = iface.driver
    t = drv.transport
    loop = asyncio.get_running_loop()

    col = Collector()
    iface.register_rx_callback(col)
    await iface.set_channel(args.channel)

    def force_monitor() -> None:
        with drv._hw_lock:
            mac.config_filter(t, monitor.MONITOR_FILTER, monitoring=True)  # 0x93

    def read66() -> int:
        with drv._hw_lock:
            return t.bbp_read(66)

    def write66(v: int) -> None:
        with drv._hw_lock:
            t.bbp_write(66, v)

    await loop.run_in_executor(None, force_monitor)
    base = await loop.run_in_executor(None, read66)
    print(f"[*] CH{args.channel}, target {target}; default BBP66 = 0x{base:02x}")
    print(f"{'BBP66':>6} {'off':>5} {'ASUS/s':>7} {'n':>4} {'rssi':>6}  {'allAP/s':>8} {'#APs':>5}")

    offsets = [-0x08, 0x00, 0x08, 0x10, 0x18, 0x20, 0x28, 0x30, 0x40]
    rows = []
    for off in offsets:
        v = max(0, min(0xFF, base + off))
        await loop.run_in_executor(None, write66, v)
        await asyncio.sleep(1.0)            # settle
        col.events.clear()
        t0 = time.monotonic()
        await asyncio.sleep(args.window)
        ev = [e for e in col.events if e[0] >= t0]
        tgt = [e for e in ev if e[1] == target]
        rate = len(tgt) / args.window
        rssi = round(statistics.mean([e[2] for e in tgt]), 1) if tgt else None
        naps = len(Counter(e[1] for e in ev))
        allrate = len(ev) / args.window
        rows.append((v, off, rate, len(tgt), rssi, allrate, naps))
        print(f"  0x{v:02x} {off:+#05x} {rate:7.1f} {len(tgt):4d} {str(rssi):>6}  "
              f"{allrate:8.1f} {naps:5d}")

    await loop.run_in_executor(None, write66, base)   # restore
    await mgr.close_all()

    best = max(rows, key=lambda r: r[2])
    print(f"\n[=] best target rate {best[2]:.1f}/s at BBP66=0x{best[0]:02x} "
          f"(off {best[1]:+#05x}) vs default {rows[1][2]:.1f}/s at 0x{base:02x}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--channel", type=int, required=True)
    p.add_argument("--bssid", type=str, required=True)
    p.add_argument("--window", type=float, default=15.0)
    raise SystemExit(asyncio.run(main(p.parse_args())))
