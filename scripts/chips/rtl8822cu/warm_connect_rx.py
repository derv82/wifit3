"""Warm-connect RX health: connect the test card, measure one 2.4 GHz and one 5 GHz AP's beacons/s, leave the
card in an end state (2g, 5g or scan), close it, repeat without a replug.

The reference card stays up the whole run and tunes with the test card, so both count the same seconds. The
test card's IGI (0x1D70, path A / path B) is read right after connect and once per second while dwelling.
Reconnects are in-process, like a disconnect + connect in the app: in-process driver state (the DACK cache
with WIFIT3_8822CU_DACK=1) carries over between cycles. Replug both cards before each run.

    uv run python scripts/chips/rtl8822cu/warm_connect_rx.py --card 8822CU --ref-card 7925 \
        --bssid2g <BSSID> --channel2g 1 --bssid5g <BSSID> --channel5g 149 [--cycles 9] [--seconds 30]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from dev import select_device  # noqa: E402

from wifit3.chips.driver import Driver  # noqa: E402
from wifit3.chips.rtl8822cu.driver import RTL8822CUDriver  # noqa: E402
from wifit3.device.manager import wlan_close, wlan_ifaces  # noqa: E402
from wifit3.dot11.packet import Packet  # noqa: E402
from wifit3.wlan.interface import WlanInterface  # noqa: E402

END_STATES = ("2g", "5g", "scan")
END_STATE_SECONDS = 5
HOP_INTERVAL = 0.25
REG_IGI = 0x1D70


@dataclass
class Dwell:
    name: str
    bssid: str
    seconds: int
    start: float
    beacons: dict[str, list[int]]
    igi: list[tuple[int, int]] = field(default_factory=list)


class BeaconCounter:
    def __init__(self) -> None:
        self.dwell: Dwell | None = None

    def callback_for(self, card_label: str) -> Callable[[Packet], None]:
        def on_packet(packet: Packet) -> None:
            dwell = self.dwell
            if dwell is None or packet.type != "beacon" or packet.bssid != dwell.bssid:
                return
            second = int(time.monotonic() - dwell.start)
            if second < dwell.seconds:
                dwell.beacons[card_label][second] += 1
        return on_packet


async def read_igi(driver: RTL8822CUDriver) -> tuple[int, int]:
    def read() -> int:
        with driver._control_lock:
            return driver.transport.read32(REG_IGI)
    value = await asyncio.get_running_loop().run_in_executor(None, read)
    return value & 0x7F, (value >> 8) & 0x7F


async def dwell(counter: BeaconCounter, cards: list[WlanInterface], labels: list[str], driver: RTL8822CUDriver,
                name: str, channel: int, bssid: str, seconds: int) -> Dwell:
    for iface, label in zip(cards, labels):
        if not await iface.set_channel(channel):
            print(f"[-] {label}: set_channel({channel}) failed", file=sys.stderr)
    record = Dwell(name, bssid.lower(), seconds, time.monotonic(), {label: [0] * seconds for label in labels})
    counter.dwell = record
    for second in range(seconds):
        record.igi.append(await read_igi(driver))
        await asyncio.sleep(max(0.0, record.start + second + 1 - time.monotonic()))
    counter.dwell = None
    return record


async def leave_in(state: str, test: WlanInterface, channel2g: int, channel5g: int) -> None:
    if state == "2g":
        await test.set_channel(channel2g)
        await asyncio.sleep(END_STATE_SECONDS)
    elif state == "5g":
        await test.set_channel(channel5g)
        await asyncio.sleep(END_STATE_SECONDS)
    else:
        channels = [c for c in test.supported_channels if c not in Driver.CHANNELS_DFS]
        await test.start_hopping(channels=channels, interval=HOP_INTERVAL)
        await asyncio.sleep(len(channels) * HOP_INTERVAL)
        await test.stop_hopping()


def print_dwell(record: Dwell, labels: list[str]) -> None:
    test_label, ref_label = labels
    test_mean = sum(record.beacons[test_label]) / record.seconds
    ref_mean = sum(record.beacons[ref_label]) / record.seconds
    ratio = test_mean / ref_mean if ref_mean else 0.0
    zero_seconds = record.beacons[test_label].count(0)
    igi_a = [a for a, _ in record.igi]
    igi_b = [b for _, b in record.igi]
    print(f"  {record.name:<8} {test_label} {test_mean:4.1f}/s  {ref_label} {ref_mean:4.1f}/s  ratio {ratio:4.2f}  "
          f"zero-s {zero_seconds:2}  IGI A 0x{min(igi_a):02x}-0x{max(igi_a):02x} B 0x{min(igi_b):02x}-0x{max(igi_b):02x}")
    for label in labels:
        print(f"    {label:<10} " + " ".join(f"{n:2}" for n in record.beacons[label]))
    print(f"    {'IGI A':<10} " + " ".join(f"{a:02x}" for a in igi_a))


async def run(args: argparse.Namespace) -> int:
    ifaces = wlan_ifaces()
    test = select_device(ifaces, args.card)
    ref = select_device([i for i in ifaces if i is not test], args.ref_card)
    if test is None or ref is None:
        await wlan_close(ifaces)
        return 1
    cards = [test, ref]
    labels = [test.chipset or test.name, ref.chipset or ref.name]
    counter = BeaconCounter()
    for iface, label in zip(cards, labels):
        iface.register_rx_callback(counter.callback_for(label))

    def progress(pct: float, msg: str) -> None:
        print(f"  [{int(pct * 100):3d}%] {msg}", file=sys.stderr)

    try:
        print(f"[*] Bringing up reference {ref.description}", file=sys.stderr)
        if not await ref.connect(progress_cb=progress):
            print("[-] reference bring-up failed", file=sys.stderr)
            return 1
        print(f"[*] test {test.description}, reference {ref.description}; {args.cycles} cycles of "
              f"{args.seconds} s on ch{args.channel2g} + {args.seconds} s on ch{args.channel5g}, "
              f"end states rotate {', '.join(END_STATES)}")
        previous_state = "none (first connect)"
        for cycle in range(args.cycles):
            print(f"[*] cycle {cycle}: connect after {previous_state}", file=sys.stderr)
            connect_start = time.monotonic()
            if not await test.connect(progress_cb=progress):
                print(f"[-] cycle {cycle}: test bring-up failed", file=sys.stderr)
                return 1
            connect_seconds = time.monotonic() - connect_start
            driver = test.driver
            assert isinstance(driver, RTL8822CUDriver)
            igi_a, igi_b = await read_igi(driver)
            print(f"\n[*] cycle {cycle}  after {previous_state}  connect {connect_seconds:.1f} s  "
                  f"IGI after connect A 0x{igi_a:02x} B 0x{igi_b:02x}")
            for name, channel, bssid in ((f"ch{args.channel2g}", args.channel2g, args.bssid2g),
                                         (f"ch{args.channel5g}", args.channel5g, args.bssid5g)):
                record = await dwell(counter, cards, labels, driver, name, channel, bssid, args.seconds)
                print_dwell(record, labels)
            sys.stdout.flush()
            state = END_STATES[cycle % len(END_STATES)]
            await leave_in(state, test, args.channel2g, args.channel5g)
            previous_state = f"{state} (closed on ch{test.current_channel})"
            await test.close()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[!] Interrupted.", file=sys.stderr)
    finally:
        await wlan_close(ifaces)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--card", required=True, help="test card substring, e.g. 8822CU")
    parser.add_argument("--ref-card", required=True, help="reference card substring, e.g. 7925")
    parser.add_argument("--bssid2g", required=True)
    parser.add_argument("--channel2g", type=int, default=1)
    parser.add_argument("--bssid5g", required=True)
    parser.add_argument("--channel5g", type=int, default=149)
    parser.add_argument("--cycles", type=int, default=9)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    if args.debug:
        import logging
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(name)s] %(message)s")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
