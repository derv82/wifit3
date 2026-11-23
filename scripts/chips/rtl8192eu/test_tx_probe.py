"""Over-air TX validation for the RTL8192EU: inject broadcast Probe Requests (benign client
scanning, not targeted at any network) and count Probe Responses addressed back to our MAC.
A reply proves the injected frame left the radio and an AP answered it."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import usb.core

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from wifit3.chips.rtl8192eu import SUPPORTED_IDS
from wifit3.chips.rtl8192eu.driver import RTL8192EUDriver
from wifit3.dot11.mac import BROADCAST_MAC
from wifit3.dot11.probe import probe_req

PROBE_RESP_SUBTYPE = 5


async def run(channel: int, probes: int) -> int:
    found = ((usb.core.find(idVendor=entry.vid, idProduct=entry.pid), entry) for entry in SUPPORTED_IDS)
    device, id_entry = next(((device, entry) for device, entry in found if device is not None), (None, None))
    if device is None:
        print("no RTL8192EU found; unplug, replug, and retry")
        return 1

    driver = RTL8192EUDriver.from_usb_device(device, id_entry)
    replies_to_us = 0
    total = 0
    our_mac_str: str | None = None

    def receive(packet) -> None:
        nonlocal replies_to_us, total
        total += 1
        if (our_mac_str is not None and packet.type_id == 0
                and packet.subtype_id == PROBE_RESP_SUBTYPE and packet.dest == our_mac_str):
            replies_to_us += 1

    driver.register_rx_callback(receive)
    driver.register_disconnect_callback(lambda *_: print("device disconnected"))
    try:
        await driver.connect(lambda value, message: print(f"[{value:5.1%}] {message}"))
        our_mac_str = driver.mac_address
        our_mac = bytes.fromhex(our_mac_str.replace(":", ""))
        await driver.set_channel(channel)

        # baseline: probe responses to us before we transmit (should be ~0)
        await asyncio.sleep(2.0)
        baseline = replies_to_us
        print(f"our MAC {our_mac_str}; baseline probe-responses-to-us in 2s: {baseline}")

        frame = probe_req(BROADCAST_MAC, our_mac, "", channel)   # wildcard broadcast probe
        print(f"injecting {probes} broadcast probe requests on channel {channel}...")
        sent = 0
        for _ in range(probes):
            await driver.inject_frame(frame)
            sent += 1
            await asyncio.sleep(0.1)

        await asyncio.sleep(2.0)   # let replies land
        new_replies = replies_to_us - baseline
        print(f"probe requests injected: {sent}")
        print(f"probe responses addressed to our MAC after injecting: {new_replies}")
        print(f"total frames seen meanwhile: {total}")
        if new_replies > 0:
            print("TX CONFIRMED: APs answered our injected probe requests over the air.")
            return 0
        print("No probe responses to us. TX frames were submitted without USB error, but no reply "
              "was observed (some APs ignore broadcast probes; try a busier channel).")
        return 2
    finally:
        await driver.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="RTL8192EU broadcast probe-request TX test")
    parser.add_argument("--channel", type=int, choices=range(1, 15), default=1)
    parser.add_argument("--probes", type=int, default=30)
    args = parser.parse_args()
    return asyncio.run(run(args.channel, args.probes))


if __name__ == "__main__":
    raise SystemExit(main())
