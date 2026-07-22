"""Live hardware smoke test for the converged ar9271_v2 driver.

Brings the card up through the real driver (``connect`` — firmware download + cold bring-up + the
bulk-IN RxReaderThread), then hops a few channels via ``set_channel`` and tallies received frames
per channel. This exercises exactly the convergence: ``driver.connect`` and repeated
``driver.set_channel`` over the live USB transport, with RX delivered by ``rx_decode``.

No TX is fired — RX only (the agent's lane; live injection stays the user's gate).

    WIFIT3_AR9271=v2  uv run python scripts/ar9271_v2/test_hw.py [--secs 8] [--channels 1,6,11]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections import Counter
from pathlib import Path

os.environ.setdefault("WIFIT3_AR9271", "v2")            # this test is for the v2 driver
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import libusb_package  # noqa: E402
import usb.core  # noqa: E402
import usb.util  # noqa: E402

from wifit3.chips.ar9271_v2 import constants as C  # noqa: E402
from wifit3.wlan.manager import WlanDeviceManager  # noqa: E402


def _reset_to_cold() -> None:
    """USB-reset the card before bring-up so the test is repeatable regardless of prior state.
    connect() always downloads firmware; re-downloading to a card already running firmware (warm,
    from a previous run) re-enumerates it mid-handshake and the bring-up disconnects. A port reset
    drops the RAM firmware -> a clean cold start, the programmatic equivalent of a replug. (Robust
    repeated bring-up in production wants a warm-reattach path instead; tracked in AR9271_V2.md.)"""
    be = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=C.AR9271_VID, idProduct=C.AR9271_PID, backend=be)
    if dev is None:
        print("[!] no AR9271 to reset (already gone?)")
        return
    try:
        dev.reset()
        print("[*] USB reset -> cold start")
    except Exception as e:  # noqa: BLE001
        print(f"[!] reset() skipped: {e}")
    finally:
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass
    time.sleep(2.0)                                     # let it re-enumerate cold


class Tally:
    """Interface-level RX subscriber: count frame types + distinct APs (BSSIDs) seen."""

    def __init__(self) -> None:
        self.types: Counter = Counter()
        self.bssids: set[str] = set()
        self.beacons = 0

    def __call__(self, pkt) -> None:
        if not pkt:
            return
        self.types[pkt.type] += 1
        if pkt.type == "beacon":
            self.beacons += 1
        b = (pkt.bssid or "").lower()
        if b:
            self.bssids.add(b)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=8.0, help="watch window per channel")
    ap.add_argument("--channels", type=str, default="1,6,11", help="comma-separated channels")
    ap.add_argument("--no-reset", action="store_true",
                    help="skip the USB reset — exercise the warm light-reattach on a warm card")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    if args.debug:
        import logging
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
        logging.getLogger("wifit3.wlan.interface").setLevel(logging.INFO)
    channels = [int(c) for c in args.channels.split(",") if c.strip()]

    if not args.no_reset:
        _reset_to_cold()
    mgr = WlanDeviceManager()
    ifaces = await mgr.refresh()
    if not ifaces:
        print("[-] No supported card found.")
        return 1
    iface = ifaces[0]
    print(f"[*] Bring up {iface.name} ({iface.description})")
    ok = await iface.connect(progress_cb=lambda p, m: print(f"   [{int(p*100):3d}%] {m}"))
    if not ok:
        print("[-] connect() returned False")
        await mgr.close_all()
        return 1
    print(f"[+] up — MAC {iface.driver.mac_address}")

    overall = 0
    for ch in channels:
        tally = Tally()
        iface.register_rx_callback(tally)
        if not await iface.set_channel(ch):
            print(f"[-] set_channel({ch}) failed")
            continue
        start = time.monotonic()
        while time.monotonic() - start < args.secs:
            await asyncio.sleep(0.25)
        iface.unregister_rx_callback(tally)
        total = sum(tally.types.values())
        overall += total
        rate = tally.beacons / args.secs
        print(f"[ch {ch:>2}] {total:5d} frames  {tally.beacons:4d} beacons ({rate:.1f}/s)  "
              f"{len(tally.bssids):2d} APs  types={dict(tally.types.most_common(6))}")

    await mgr.close_all()
    print(f"\n{'PASS' if overall else 'FAIL'}: {overall} frames across {len(channels)} channels")
    return 0 if overall else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[!] interrupted")
        sys.exit(130)
