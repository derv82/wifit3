"""wifit3 side of the card health check.

Brings up our userland driver, dwells on each channel, and feeds every beacon
to the shared aggregator via the SAME ``feed(ts, parsed, rssi, channel)`` call
the Linux side uses. Writes ``wifit3-<chip>.json``.

    python baseline_wifit3.py                         # SUPPORTED_CHANNELS
    python baseline_wifit3.py --channels 1,6,11 --secs 15
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
sys.path.insert(0, str(_HERE.parent))  # scripts/ for dev.py

from shared import Health, add_reference_args, load_reference_aps, ref_bssids  # noqa: E402
from dev import pick_interface  # noqa: E402
from baseline_diff import diff  # noqa: E402

from wifit3.wlan.discovery import build_interfaces, close_interfaces  # noqa: E402


def _chip(iface) -> str:
    return type(iface.driver).__name__.lower().removesuffix("driver")


async def run(args) -> int:
    print("[*] Discovering interfaces...", file=sys.stderr)
    ifaces = build_interfaces()
    if not ifaces:
        print("[-] No supported devices found.", file=sys.stderr)
        return 1
    iface = pick_interface(ifaces, args.card)
    if iface is None:
        await close_interfaces(ifaces)
        return 1

    def _progress(pct: float, msg: str) -> None:
        print(f"  [{int(pct * 100):3d}%] {msg}", file=sys.stderr)

    print(f"[*] Bringing up {iface.name} ({iface.description})...", file=sys.stderr)
    try:
        if not await iface.connect(progress_cb=_progress):
            print("[-] Bring-up returned False.", file=sys.stderr)
            await close_interfaces(ifaces)
            return 1
    except Exception as e:  # noqa: BLE001, USB bring-up can raise
        print(f"[-] Bring-up failed: {e}", file=sys.stderr)
        await close_interfaces(ifaces)
        return 1

    chip = args.chip or _chip(iface)
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
        await close_interfaces(ifaces)

    wifit3_json = _HERE / f"wifit3-{chip}.json"
    health.to_json(wifit3_json)
    # A/B diff fires from whichever side runs second. In the settled sweep order (linux first,
    # wifit3 second) the diff would otherwise never print, since only baseline_linux used to call
    # it. Pin the reference AP(s) so the beacon-rate line doesn't drift to a transient AP.
    linux_json = _HERE / f"linux-{chip}.json"
    if linux_json.exists():
        diff(wifit3_json, linux_json, ref_bssids=ref_bssids(load_reference_aps(args)) or None)
    else:
        print(f"[*] no {linux_json.name} yet - run baseline_linux.py --chip {chip} "
              f"(--capture or --pcap) for the A/B.", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="wifit3-side card health baseline.")
    p.add_argument("--channels", default=None, help="Comma-separated; default SUPPORTED_CHANNELS.")
    p.add_argument("--secs", type=float, default=15.0, help="Dwell seconds per channel.")
    p.add_argument("--card", default="",
                   help="substring of the adapter to bring up (e.g. 8812, mt7921); default: first found.")
    p.add_argument("--chip", default=None,
                   help="output slug override (default: derived from the driver). Use a suffix like "
                        "mt7921au_axml to keep same-chipset cards' baselines from clobbering each other.")
    add_reference_args(p)
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
