"""Record raw RTL8821AU monitor RX frames, including ACK control frames, into a PCAP."""
from __future__ import annotations

import argparse
import asyncio
import struct
import sys
import time
from pathlib import Path

import usb.core

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from wifit3.chips.rtl8821au import SUPPORTED_IDS
from wifit3.chips.rtl8821au.driver import RTL8821AUDriver
from wifit3.chips.rtl8821au.rx import iter_bulk_frames

PCAP_LINKTYPE_IEEE802_11 = 105
RTL8821AU_ID = next(entry for entry in SUPPORTED_IDS if (entry.vid, entry.pid) == (0x2357, 0x011F))


class PcapWriter:
    def __init__(self, path: Path) -> None:
        self._file = path.open("wb")
        header = struct.pack(
            "<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, PCAP_LINKTYPE_IEEE802_11
        )
        self._file.write(header)

    def write(self, frame: bytes) -> None:
        now = time.time()
        seconds = int(now)
        microseconds = int((now - seconds) * 1_000_000)
        self._file.write(struct.pack("<IIII", seconds, microseconds, len(frame), len(frame)))
        self._file.write(frame)

    def close(self) -> None:
        self._file.close()


async def capture(channel: int, seconds: float, output: Path) -> int:
    device = usb.core.find(idVendor=0x2357, idProduct=0x011F)
    if device is None:
        print("RTL8821AU 2357:011f not found")
        return 1

    output.parent.mkdir(parents=True, exist_ok=True)
    writer = PcapWriter(output)
    driver = RTL8821AUDriver.from_usb_device(device, RTL8821AU_ID)
    original_dispatch = driver._rx_dispatch
    frames_written = 0

    def capture_dispatch(buffer: bytes) -> None:
        nonlocal frames_written
        for _status, frame, _rssi in iter_bulk_frames(buffer):
            writer.write(frame)
            frames_written += 1
        original_dispatch(buffer)

    driver._rx_dispatch = capture_dispatch
    connected = False
    try:
        await driver.connect(lambda value, message: print(f"[{value:5.1%}] {message}"))
        connected = True
        await driver.enable_rx_acks()
        await driver.set_channel(channel)
        print(f"capturing RTL8821AU monitor RX on channel {channel} for {seconds:.1f}s")
        await asyncio.sleep(seconds)
    finally:
        if connected:
            await driver.disable_rx_acks()
            await driver.close()
        writer.close()
    print(f"saved {frames_written} frame(s) to {output}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Passively record RTL8821AU raw monitor RX into a classic 802.11 PCAP"
    )
    parser.add_argument("--channel", type=int, required=True)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--out", type=Path, default=Path("rtl8821au-observer.pcap"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.channel not in RTL8821AUDriver.SUPPORTED_CHANNELS:
        parser.error("--channel is unsupported by RTL8821AU")
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    if args.dry_run:
        print(f"would capture RTL8821AU monitor RX on channel {args.channel} for {args.seconds:.1f}s")
        return 0
    return asyncio.run(capture(args.channel, args.seconds, args.out))


if __name__ == "__main__":
    raise SystemExit(main())
