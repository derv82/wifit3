"""Hop every supported channel as fast as possible for --seconds; count failed tunes."""
import argparse
import asyncio
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dev import select_device
from wifit3.device.manager import wlan_ifaces, wlan_close


async def main(args) -> int:
    ifaces = wlan_ifaces()
    iface = select_device(ifaces, args.card)
    if iface is None or not await iface.connect():
        await wlan_close(ifaces)
        return 1
    lost = []
    iface.register_disconnect_callback(lost.append)
    errors = Counter()
    tunes = 0
    end = time.monotonic() + args.seconds
    while time.monotonic() < end:
        for channel in iface.supported_channels:
            if lost:
                print(f"device lost after {tunes} tunes: {lost[0]}")
                await wlan_close(ifaces)
                return 1
            try:
                await iface.set_channel(channel)
            except Exception as e:
                errors[f"ch{channel} {type(e).__name__}: {e}"] += 1
            tunes += 1

    frames = 0

    def count_frame(frame) -> None:
        nonlocal frames
        frames += 1

    await iface.set_channel(args.channel)
    iface.register_rx_callback(count_frame)
    await asyncio.sleep(args.dwell)
    await wlan_close(ifaces)
    print(f"dwell ch{args.channel} {args.dwell:g}s frames={frames}")
    print(f"tunes={tunes} errors={sum(errors.values())} tunes/s={tunes / args.seconds:.1f}")
    for error, count in errors.most_common():
        print(f"{count:6d}  {error}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--card", default="")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--dwell", type=float, default=5)
    parser.add_argument("--channel", type=int, required=True)
    sys.exit(asyncio.run(main(parser.parse_args())))
