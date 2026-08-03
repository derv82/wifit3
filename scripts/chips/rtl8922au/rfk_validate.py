"""Validate the RFK-wait fix on real hardware.

Confirms what verify_pcap structurally cannot: that the firmware's pkt_type=10 RFK-report C2H
actually arrives and the per-step waits land (state OK) instead of timing out, and that RX is now
deterministic per channel (the old bug was random deaf tunes). Instruments transport.RfkWait, then
watches fixed channels via the real reader path.

    uv run python scripts/chips/rtl8922au/rfk_validate.py            # ch1, ch36, ch1 again
    uv run python scripts/chips/rtl8922au/rfk_validate.py --watch 30

Run once right after a physical replug (a clean cold boot).
"""
import argparse
import asyncio
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

import libusb_package
import usb.core

from wifit3.chips.rtl8922au.driver import RTL8922AUDriver


def _find():
    backend = libusb_package.get_libusb1_backend()
    for did in RTL8922AUDriver.SUPPORTED_IDS:
        dev = usb.core.find(idVendor=did.vid, idProduct=did.pid, backend=backend)
        if dev is not None:
            return dev, did
    return None, None


def _instrument(rfk_wait, stats):
    """Wrap prep/signal/wait to tally RFK completions vs timeouts."""
    _prep, _signal, _wait = rfk_wait.prep, rfk_wait.signal, rfk_wait.wait

    def prep():
        stats["prepped"] += 1
        return _prep()

    def signal(state):
        stats["signaled"] += 1
        stats["states"][state] += 1
        return _signal(state)

    def wait(timeout_s):
        r = _wait(timeout_s)
        stats["ok" if r == 1 else "timeout_or_fail"] += 1
        return r

    rfk_wait.prep, rfk_wait.signal, rfk_wait.wait = prep, signal, wait


async def main(args):
    dev, did = _find()
    if dev is None:
        print("[-] device not found")
        return 1
    print(f"[*] {did.vid:04x}:{did.pid:04x} speed={getattr(dev,'speed',None)} addr={dev.address}")
    drv = RTL8922AUDriver.from_usb_device(dev, did)
    stats = {"prepped": 0, "signaled": 0, "ok": 0, "timeout_or_fail": 0, "states": Counter()}

    beacons = Counter()
    drv.register_rx_callback(lambda pkt: beacons.update([pkt.type]) if pkt else None)

    t0 = time.monotonic()
    print("[*] connect() ...")
    ok = await drv.connect(progress_cb=lambda p, m: None)
    print(f"[*] connect ok={ok} in {time.monotonic()-t0:.1f}s")
    # Instrument AFTER connect: on USB-2 the mode switch re-enumerates and connect rebuilds the
    # transport (a fresh rfk_wait), so we must wrap the FINAL transport's rfk_wait to see the per-hop
    # RFK waits. init_late's RFK (during connect) is on the earlier transport and not counted here.
    _instrument(drv.transport.rfk_wait, stats)
    print(f"[*] rfk_wait.enabled={drv.transport.rfk_wait.enabled} (reader running); "
          "per-hop RFK measured below")

    for ch in args.channels:
        before = dict(stats)
        beacons.clear()
        t = time.monotonic()
        await drv.set_channel(ch)     # the driver runs the prehdl double-tune (2+0 then 1+1) itself
        tune_s = time.monotonic() - t
        d_ok = stats["ok"] - before["ok"]
        d_to = stats["timeout_or_fail"] - before["timeout_or_fail"]
        beacons.clear()
        await asyncio.sleep(args.watch)
        b = beacons.get("beacon", 0)
        print(f"[*] CH{ch:>3}: tune {tune_s:4.1f}s (RFK landed_OK={d_ok} timeout/fail={d_to})  "
              f"-> {b} beacons in {args.watch}s ({b/args.watch:.1f}/s)  {dict(beacons)}")

    print(f"\n[*] TOTAL RFK: prepped={stats['prepped']} landed_OK={stats['ok']} "
          f"timeout/fail={stats['timeout_or_fail']} states={dict(stats['states'])}")
    print("[*] PASS criteria: landed_OK should dominate (C2H arriving), and each channel's "
          "beacons/s should be steady and non-zero on every tune (no random deaf).")
    await drv.close()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--channels", type=int, nargs="+", default=[1, 36, 1])
    p.add_argument("--watch", type=int, default=20)
    # --mlo is gone: the driver now derives + runs the prehdl double-tune (2+0 then 1+1) per hop, so
    # every tune ends in 1+1 (both RX chains). This harness validates that path lands + RX is steady.
    sys.exit(asyncio.run(main(p.parse_args())))
