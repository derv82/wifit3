from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import usb.core

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from wifit3.chips.rtl8192fu import SUPPORTED_IDS
from wifit3.chips.rtl8192fu.driver import RTL8192FUDriver


async def run(duration: float, channel: int) -> int:
    found = ((usb.core.find(idVendor=entry.vid, idProduct=entry.pid), entry) for entry in SUPPORTED_IDS)
    device, id_entry = next(((device, entry) for device, entry in found if device is not None), (None, None))
    if device is None:
        print("no RTL8192FU found; unplug, replug, and retry")
        return 1

    packets = 0
    rssi_values: list[int] = []

    def receive(packet) -> None:
        nonlocal packets
        packets += 1
        rssi_values.append(packet.rssi)

    driver = RTL8192FUDriver.from_usb_device(device, id_entry)
    driver.register_rx_callback(receive)
    driver.register_disconnect_callback(lambda: print("device disconnected"))
    try:
        await driver.connect(lambda value, message: print(f"[{value:5.1%}] {message}"))
        await driver.set_channel(channel)
        print(f"listening on channel {channel} for {duration:.1f}s without transmitting...")
        await asyncio.sleep(duration)
        print(f"decoded packets: {packets}")
        if rssi_values:
            ordered = sorted(rssi_values)
            def percentile(value: float) -> int:
                return ordered[round((len(ordered) - 1) * value)]

            positive = sum(value > 0 for value in ordered)
            print(f"RSSI range: {ordered[0]}..{ordered[-1]} dBm")
            print(
                f"RSSI p05/median/p95: {percentile(0.05)}/"
                f"{percentile(0.50)}/{percentile(0.95)} dBm; positive samples: {positive}"
            )
        return 0
    finally:
        await driver.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="RX-only RTL8192FU cold-boot smoke test")
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--channel", type=int, choices=range(1, 15), default=1)
    args = parser.parse_args()
    return asyncio.run(run(args.duration, args.channel))


if __name__ == "__main__":
    raise SystemExit(main())
