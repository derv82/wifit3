"""Test whether the bring-up coin toss is a TIMING/RACE issue, by A/B-alternating two bring-up
configs and comparing their dead-rates. Alternating (not blocked) so any chip drift over the run
hits both configs equally.

Configs (reader_mode, write-pace):
  baseline   - bulk-IN reader running DURING cold_bringup (what the driver does), no write pacing
  quiet      - NO reader during bringup; start it only AFTER init completes (what the C driver does)
  paced      - reader during bringup, but sleep `pace_us` after every register write
  quietpaced - quiet + paced

The hypothesis: identical register sequence, different outcome → timing. If `quiet` and/or `paced`
collapse the dead-rate vs `baseline`, the race is the concurrent reader and/or the un-paced writes.

Passive (RX only).  uv run python scripts/rtl8821cu_dkms/bringup_timing.py <cfgA> <cfgB> [total] [dwell_s] [pace_us]
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import libusb_package
import usb.core

from wifit3.chips.rtl8821cu_dkms import bringup
from wifit3.chips.rtl8821cu_dkms.driver import Rtl8821cuDkmsDriver
from wifit3.chips.rtl8821cu_dkms.rx import iter_frames

_CFG = {
    "baseline":   ("during", 0),
    "quiet":      ("after", 0),
    "paced":      ("during", 1),     # pace_us multiplier applied below
    "quietpaced": ("after", 1),
}


class SimpleReader:
    """Minimal bulk-IN reader thread: count delivered good frames. Full control over start time."""
    def __init__(self, t):
        self.t = t
        self.n = 0
        self._run = False
        self._th = None

    def start(self):
        self._run = True
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    def _loop(self):
        while self._run:
            try:
                buf = self.t.bulk_in()
            except Exception:  # noqa: BLE001
                continue
            if buf:
                for _frame, _rssi in iter_frames(buf, getattr(self.t, "cck_new_agc", False)):
                    self.n += 1

    def stop(self):
        self._run = False
        if self._th:
            self._th.join(timeout=2.0)


def _pace_writes(t, pace_s: float):
    orig = t.writeN
    def paced(addr, data):
        orig(addr, data)
        time.sleep(pace_s)
    t.writeN = paced


def one(dev, reader_mode: str, pace_s: float, dwell: float) -> tuple[int, int]:
    """Run one bring-up in the given config; return (delivered, rxff_ptr_after)."""
    drv = Rtl8821cuDkmsDriver(dev)
    drv._claim()
    t = drv.transport
    if pace_s > 0:
        _pace_writes(t, pace_s)
    reader = SimpleReader(t)
    if reader_mode == "during":
        reader.start()
    try:
        bringup.cold_bringup(t)
        drv._relatch_2g_band()
    finally:
        if reader_mode == "after":
            reader.start()
    time.sleep(dwell)
    reader.stop()
    try:
        rxff = t.read32(0x1118)
    except Exception:  # noqa: BLE001
        rxff = -1
    t.close()
    return reader.n, rxff


def main() -> int:
    cfg_a = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    cfg_b = sys.argv[2] if len(sys.argv) > 2 else "quiet"
    total = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    dwell = float(sys.argv[4]) if len(sys.argv) > 4 else 6.0
    pace_us = float(sys.argv[5]) if len(sys.argv) > 5 else 800.0
    for c in (cfg_a, cfg_b):
        if c not in _CFG:
            print(f"unknown config {c!r}; choose from {list(_CFG)}")
            return 1
    backend = libusb_package.get_libusb1_backend()
    tally = {cfg_a: [0, 0], cfg_b: [0, 0]}        # name -> [good, dead]
    order = [cfg_a, cfg_b]
    print(f"A/B: {cfg_a} vs {cfg_b}, {total} launches, dwell={dwell}s, pace={pace_us}us\n")
    for i in range(total):
        cfg = order[i % 2]
        reader_mode, pace_mult = _CFG[cfg]
        pace_s = (pace_us / 1e6) * pace_mult
        dev = usb.core.find(idVendor=0x0BDA, idProduct=0xC820, backend=backend)
        if dev is None:
            print("no 0bda:c820 device")
            return 1
        try:
            delivered, rxff = one(dev, reader_mode, pace_s, dwell)
        except Exception as e:  # noqa: BLE001
            print(f"iter {i:2d} [{cfg:10s}] EXCEPTION: {e!r}")
            time.sleep(1.0)
            continue
        verdict = "GOOD" if delivered >= 10 else "DEAD"
        tally[cfg][0 if verdict == "GOOD" else 1] += 1
        print(f"iter {i:2d} [{cfg:10s}] {verdict}  delivered={delivered:5d}  RXFF_PTR=0x{rxff:x}")
        time.sleep(0.8)
    print("\n=== dead-rate by config ===")
    for c in (cfg_a, cfg_b):
        g, d = tally[c]
        tot = g + d
        print(f"  {c:11s}: {g} GOOD / {d} DEAD  ({100*d/tot:.0f}% dead)" if tot else f"  {c}: no runs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
