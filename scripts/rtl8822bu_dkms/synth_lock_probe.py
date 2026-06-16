"""Find what actually un-sticks the cold 2.4 GHz synth (RF18 bit15 = stuck/deaf).

Cold-boots repeatedly; on a stuck boot, tries recovery strategies in order and reports the
synth-lock bit (RF18 bit15) + a short RX frame count after each, so we learn what the live
app's hopping does that a quick bounce doesn't. Passive (no TX). Bypasses driver.connect()'s
built-in heal so we can measure recovery from the raw stuck state.

Run: uv run python scripts/rtl8822bu_dkms/synth_lock_probe.py --tries 12
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

from wifit3.chips.rtl8822bu_dkms import bringup, chan, mac, sipi, txpower
from wifit3.chips.rtl8822bu_dkms.driver import Rtl8822buDkmsDriver
from wifit3.chips.rtl8822bu_dkms.rx import iter_frames
from wifit3.chips.rtl8822bu_dkms.transport import Rtl8822buTransport


def stuck(t) -> int:
    return (sipi.read_rf_reg(t, sipi.RF_PATH_A, 0x18) >> 15) & 1


def listen(t, secs: float) -> int:
    """Count RX frames over `secs` via raw bulk-IN (enable_monitor already done)."""
    n = 0
    end = time.perf_counter() + secs
    while time.perf_counter() < end:
        buf = t.bulk_in()
        if buf:
            for _frame, _rssi in iter_frames(buf):
                n += 1
    return n


def cold_boot(t):
    info, e = bringup.cold_bringup(t)
    pg = txpower.parse_pg(e.log_map)
    chan.set_channel_bw(t, 1, txpwr_pg=pg)            # initial 2.4 GHz tune (no heal)
    mac.enable_monitor(t)
    return pg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tries", type=int, default=12)
    args = ap.parse_args()
    entry = Rtl8822buDkmsDriver.SUPPORTED_IDS[0]
    backend = libusb_package.get_libusb1_backend()

    for i in range(args.tries):
        dev = usb.core.find(idVendor=entry.vid, idProduct=entry.pid, backend=backend)
        if dev is None:
            print(f"try {i+1}: no device"); time.sleep(1); continue
        try:
            dev.set_configuration()
        except usb.core.USBError:
            pass
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except Exception:
            pass
        usb.util.claim_interface(dev, 0)
        t = Rtl8822buTransport(dev, bulk_out_ep=0x05)
        try:
            pg = cold_boot(t)
            s0 = stuck(t)
            base = listen(t, 1.0)
            if s0 == 0 and base > 0:
                print(f"try {i+1}: locked OK (bit15=0, frames={base}) — skip")
            else:
                print(f"try {i+1}: STUCK (bit15={s0}, frames={base}) — testing recovery:")
                # A: immediate 5->2.4 bounce
                chan.set_channel_bw(t, 36, prev_ch=1, txpwr_pg=pg)
                s_5g = stuck(t)
                chan.set_channel_bw(t, 1, prev_ch=36, txpwr_pg=pg)
                print(f"    A immediate bounce: 5G bit15={s_5g} -> 2.4G bit15={stuck(t)} frames={listen(t,1.0)}")
                # B: 5G + dwell + 2.4
                chan.set_channel_bw(t, 36, prev_ch=1, txpwr_pg=pg)
                time.sleep(0.5)
                chan.set_channel_bw(t, 1, prev_ch=36, txpwr_pg=pg)
                print(f"    B 5G+0.5s dwell+2.4: bit15={stuck(t)} frames={listen(t,1.0)}")
                # C: walk several 5G channels then back (mimic a full 5G lap)
                prev = 1
                for ch in (36, 40, 44, 48, 1):
                    chan.set_channel_bw(t, ch, prev_ch=prev, txpwr_pg=pg)
                    time.sleep(0.25)
                    prev = ch
                print(f"    C 5G lap+2.4: bit15={stuck(t)} frames={listen(t,1.0)}")
                # D: re-tune 2.4 a few times in place
                for _ in range(5):
                    chan.set_channel_bw(t, 1, prev_ch=1, txpwr_pg=pg)
                    time.sleep(0.1)
                print(f"    D 5x re-tune 2.4: bit15={stuck(t)} frames={listen(t,1.0)}")
        finally:
            usb.util.release_interface(dev, 0)
            usb.util.dispose_resources(dev)
        time.sleep(0.3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
