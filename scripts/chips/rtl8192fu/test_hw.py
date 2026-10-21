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


async def run(duration: float) -> int:
    device = usb.core.find(idVendor=0x2001, idProduct=0x3328)
    if device is None:
        print("RTL8192FU 2001:3328 not found; unplug, replug, and retry")
        return 1

    packets = 0

    def receive(_packet) -> None:
        nonlocal packets
        packets += 1

    driver = RTL8192FUDriver.from_usb_device(device, SUPPORTED_IDS[0])
    driver.register_rx_callback(receive)
    driver.register_disconnect_callback(lambda: print("device disconnected"))
    try:
        await driver.connect(lambda value, message: print(f"[{value:5.1%}] {message}"))
        print(f"listening for {duration:.1f}s without transmitting...")
        await asyncio.sleep(duration)
        print(f"decoded packets: {packets}")
        return 0
    finally:
        await driver.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="RX-only RTL8192FU cold-boot smoke test")
    parser.add_argument("--duration", type=float, default=10.0)
    args = parser.parse_args()
    return asyncio.run(run(args.duration))


if __name__ == "__main__":
    raise SystemExit(main())
