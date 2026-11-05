"""Experimentally reconnect one RTL8822CU using its in-memory DACK calibration."""
from __future__ import annotations

import argparse
import asyncio
import logging
import time

from wifit3.chips.log_trace import TRACE
from wifit3.chips.rtl8822cu.driver import RTL8822CUDriver
from wifit3.device.manager import wlan_close, wlan_ifaces


async def run(channel: int, seconds: float) -> int:
    ifaces = wlan_ifaces()
    matches = [iface for iface in ifaces if isinstance(iface.driver, RTL8822CUDriver)]
    if len(matches) != 1:
        print(f"Expected exactly one RTL8822CU, found {len(matches)}; disconnect any others.")
        await wlan_close(ifaces)
        return 1

    driver = matches[0].driver
    received = 0
    disconnected = False

    def on_frame(_frame) -> None:
        nonlocal received
        received += 1

    def on_disconnect(_error) -> None:
        nonlocal disconnected
        disconnected = True

    def progress(pct: float, message: str) -> None:
        print(f"  [{pct * 100:3.0f}%] {message}", flush=True)

    async def observe(label: str) -> int:
        nonlocal received
        received = 0
        if not await driver.set_channel(channel):
            raise RuntimeError(f"Could not tune channel {channel}")
        await asyncio.sleep(seconds)
        print(f"{label}: {received} parsed frames in {seconds:g}s on channel {channel}")
        return received

    driver.register_rx_callback(on_frame)
    driver.register_disconnect_callback(on_disconnect)
    print("Do not unplug the card during this experiment. No frames are transmitted.")
    try:
        start = time.monotonic()
        if not await driver.connect(progress_cb=progress):
            raise RuntimeError("Initial connect failed")
        cold_time = time.monotonic() - start
        cold_frames = await observe("Initial RX")
        if disconnected or not driver.dack.dack_en:
            raise RuntimeError("Card disconnected or DACK calibration was not saved")

        saved_dack = driver.dack
        await driver.close()
        if disconnected:
            raise RuntimeError("Card disconnected; refusing to reuse calibration")
        driver.dack = saved_dack
        print("Reconnecting the same USB device with experimentally retained DACK state.")
        start = time.monotonic()
        if not await driver.connect(progress_cb=progress):
            raise RuntimeError("Reconnect failed")
        warm_time = time.monotonic() - start
        warm_frames = await observe("Reconnect RX")
        print(f"Connect times: initial {cold_time:.2f}s; reconnect {warm_time:.2f}s")
        if disconnected:
            raise RuntimeError("Card disconnected during the test")
        if not cold_frames or not warm_frames:
            print("RX was silent in at least one interval; the test is inconclusive.")
            return 1
        print("Both intervals received frames; compare counts with care as RF traffic varies.")
        return 0
    finally:
        await wlan_close(ifaces)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", type=int, default=1)
    parser.add_argument("--seconds", type=float, default=15.0, help="RX observation time per phase")
    parser.add_argument("--trace", action="store_true", help="Enable driver trace logging")
    args = parser.parse_args()
    if args.channel not in RTL8822CUDriver.SUPPORTED_CHANNELS:
        parser.error("unsupported channel")
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    if args.trace:
        logging.basicConfig(level=TRACE, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(run(args.channel, args.seconds))
    except (RuntimeError, OSError) as exc:
        print(f"Test failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
