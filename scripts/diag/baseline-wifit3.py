"""wifit3 side of the card health check.

Brings up our userland driver, dwells on each channel, and feeds every beacon
to the shared aggregator via the SAME ``feed(ts, parsed, rssi, channel)`` call
the Linux side uses. Writes ``wifit3-<chip>.json``.

    python baseline-wifit3.py                         # SUPPORTED_CHANNELS
    python baseline-wifit3.py --channels 1,6,11 --secs 15
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "src"))
sys.path.insert(0, str(_HERE))

from driver_health import Health  # noqa: E402

from wifit3.wlan.manager import WlanDeviceManager  # noqa: E402


def _chip(iface) -> str:
    return type(iface.driver).__name__.lower().removesuffix("driver")


async def run(args) -> int:
    print("[*] Discovering interfaces...", file=sys.stderr)
    mgr = WlanDeviceManager()
    ifaces = await mgr.refresh()
    if not ifaces:
        print("[-] No supported devices found.", file=sys.stderr)
        return 1
    iface = ifaces[0]
    if len(ifaces) > 1:
        print(f"[!] {len(ifaces)} interfaces, using {iface.name}; unplug others.", file=sys.stderr)

    def _progress(pct: float, msg: str) -> None:
        print(f"  [{int(pct * 100):3d}%] {msg}", file=sys.stderr)

    print(f"[*] Bringing up {iface.name} ({iface.description})...", file=sys.stderr)
    try:
        if not await iface.connect(progress_cb=_progress):
            print("[-] Bring-up returned False.", file=sys.stderr)
            await mgr.close_all()
            return 1
    except Exception as e:  # noqa: BLE001, USB bring-up can raise
        print(f"[-] Bring-up failed: {e}", file=sys.stderr)
        await mgr.close_all()
        return 1

    chip = _chip(iface)
    health = Health(chip, "wifit3")
    cur_channel = {"ch": 0}

    def on_rx(pkt) -> None:
        if cur_channel["ch"] == 0:
            return  # RX loop runs before the first set_channel; don't bucket pre-sweep frames at ch0
        health.feed(time.monotonic(), pkt, pkt.rssi, cur_channel["ch"])

    iface.register_rx_callback(on_rx)

    channels = (
        [int(c) for c in args.channels.split(",") if c.strip()]
        if args.channels
        else list(getattr(iface.driver, "SUPPORTED_CHANNELS", []) or [1, 6, 11])
    )
    print(f"[*] Sweeping {channels} at {args.secs}s/channel", file=sys.stderr)
    try:
        for ch in channels:
            if not await iface.set_channel(ch):
                print(f"  CH{ch:>3}: set_channel failed, skipping", file=sys.stderr)
                continue
            cur_channel["ch"] = ch
            await asyncio.sleep(0.25)  # AGC / pipe drain
            await asyncio.sleep(args.secs)
            print(f"  CH{ch:>3}: dwelt {args.secs}s", file=sys.stderr)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[!] Interrupted: writing partial.", file=sys.stderr)
    finally:
        await mgr.close_all()

    health.to_json(_HERE / f"wifit3-{chip}.json")
    print(f"[*] next: baseline-linux.py --chip {chip} (--capture or --pcap)", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="wifit3-side card health baseline.")
    p.add_argument("--channels", default=None, help="Comma-separated; default SUPPORTED_CHANNELS.")
    p.add_argument("--secs", type=float, default=15.0, help="Dwell seconds per channel.")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    if args.debug:
        import logging
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(name)s] %(message)s")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
