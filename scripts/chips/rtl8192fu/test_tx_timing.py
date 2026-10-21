"""Measure RTL8192FU TX submission latency and directed-deauth ACKs."""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import usb.core

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from wifit3.chips.rtl8192fu import SUPPORTED_IDS
from wifit3.chips.rtl8192fu.driver import RTL8192FUDriver
from wifit3.dot11 import build_deauth, str_to_mac
from wifit3.dot11.deauth import deauth_nav_bytes

USB_SLOW_SECONDS = 0.100
ACK_WAIT_SECONDS = 0.050


async def run(channel: int, ap_bssid: str, client_mac: str, count: int, dry_run: bool) -> int:
    ap = str_to_mac(ap_bssid)
    client = str_to_mac(client_mac)
    frames = (
        ("AP->client", build_deauth(
            client, ap, ap, 7, duration=deauth_nav_bytes(client_mac)
        ), ap),
        ("client->AP", build_deauth(
            ap, client, ap, 7, duration=deauth_nav_bytes(ap_bssid)
        ), client),
    )
    if dry_run:
        print(f"would send {count} directed deauth pair(s) on channel {channel}")
        return 0

    device = usb.core.find(idVendor=0x2001, idProduct=0x3328)
    if device is None:
        print("RTL8192FU 2001:3328 not found; unplug, replug, and retry")
        return 1

    driver = RTL8192FUDriver.from_usb_device(device, SUPPORTED_IDS[0])
    connected = False
    observations: list[tuple[str, float, int]] = []
    try:
        await driver.connect(lambda value, message: print(f"[{value:5.1%}] {message}"))
        connected = True
        await driver.set_channel(channel)
        await driver.enable_rx_acks()
        print(f"testing {count} directed deauth pair(s) on channel {channel}")
        for attempt in range(1, count + 1):
            for label, frame, source_mac in frames:
                before = driver.acks_seen(source_mac)
                started = time.monotonic()
                await driver.inject_frame(frame)
                submitted = time.monotonic() - started
                await asyncio.sleep(ACK_WAIT_SECONDS)
                acked = driver.acks_seen(source_mac) - before
                observations.append((label, submitted, acked))
                print(
                    f"{attempt:02d} {label:10s} USB submit={submitted * 1000:7.1f} ms "
                    f"ACKs={acked}"
                )
        return _report(observations)
    except Exception as error:
        print(f"FAIL: {type(error).__name__}: {error}")
        return 1
    finally:
        if connected:
            await driver.disable_rx_acks()
            await driver.close()


def _report(observations: list[tuple[str, float, int]]) -> int:
    slow = [item for item in observations if item[1] >= USB_SLOW_SECONDS]
    acked = sum(item[2] for item in observations)
    print(
        f"summary: {len(observations)} frames, {acked} ACKs, "
        f"{len(slow)} USB submissions >= {USB_SLOW_SECONDS * 1000:.0f} ms"
    )
    if slow:
        print("diagnosis: USB I/O slowdown or a stalled TX endpoint")
        return 2
    if not acked:
        print("diagnosis: USB submission is fast; no recipient ACK was observed")
        return 3
    print("diagnosis: USB submission and directed ACK path look healthy")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure RTL8192FU USB TX latency and directed-deauth ACKs"
    )
    parser.add_argument("--channel", type=int, choices=range(1, 15), required=True)
    parser.add_argument("--ap-bssid", required=True)
    parser.add_argument("--client-mac", required=True)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be at least 1")
    return asyncio.run(run(args.channel, args.ap_bssid, args.client_mac, args.count, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
