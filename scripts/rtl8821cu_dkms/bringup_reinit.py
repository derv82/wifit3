"""Test whether the bring-up failure is a RE-INIT/cleanup bug: good launches ratchet to permanent
dead across soft re-inits (connect/close/connect) because close() never powers the chip off, so the
next connect sees "already powered" and skips the clean card-enable flow.

Hypothesis: forcing a clean power-OFF (CARD_DIS_FLOW) before each bring-up makes every soft re-init
start cold (like a physical replug), so it stays good instead of ratcheting dead.

  baseline  - connect/dwell/close, no forced power-off (reproduces the ratchet)
  forceoff  - force mac_pwr_switch(OFF) before each cold_bringup (the proposed fix surface)

Passive (RX only).  uv run python scripts/rtl8821cu_dkms/bringup_reinit.py <baseline|forceoff> [iters] [dwell]
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import libusb_package
import usb.core

from wifit3.chips.rtl8821cu_dkms import bringup, pwrseq
from wifit3.chips.rtl8821cu_dkms.driver import Rtl8821cuDkmsDriver
from wifit3.chips.rtl8821cu_dkms.rx import iter_frames


class SimpleReader:
    def __init__(self, t):
        self.t, self.n, self._run, self._th = t, 0, False, None

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
                for _f, _r in iter_frames(buf, getattr(self.t, "cck_new_agc", False)):
                    self.n += 1

    def stop(self):
        self._run = False
        if self._th:
            self._th.join(timeout=2.0)


def one(dev, force_off: bool, dwell: float) -> int:
    drv = Rtl8821cuDkmsDriver(dev)
    drv._claim()
    t = drv.transport
    if force_off:
        try:
            pwrseq.mac_pwr_switch(t, power_on=False)     # force a clean cold state first
        except Exception as e:  # noqa: BLE001
            print(f"    (force-off raised: {e!r})")
    bringup.cold_bringup(t)
    drv._relatch_2g_band()
    reader = SimpleReader(t)
    reader.start()
    time.sleep(dwell)
    reader.stop()
    # power the chip down on the way out, so the next iteration's baseline is identical
    try:
        pwrseq.mac_pwr_switch(t, power_on=False)
    except Exception:  # noqa: BLE001
        pass
    t.close()
    return reader.n


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "forceoff"
    iters = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    dwell = float(sys.argv[3]) if len(sys.argv) > 3 else 6.0
    force_off = mode == "forceoff"
    backend = libusb_package.get_libusb1_backend()
    good = dead = 0
    print(f"mode={mode} (force_off={force_off}), {iters} soft re-inits, dwell={dwell}s\n")
    for i in range(iters):
        dev = usb.core.find(idVendor=0x0BDA, idProduct=0xC820, backend=backend)
        if dev is None:
            print("no 0bda:c820 device")
            return 1
        try:
            n = one(dev, force_off, dwell)
        except Exception as e:  # noqa: BLE001
            print(f"iter {i:2d}: EXCEPTION {e!r}")
            time.sleep(1.0)
            continue
        verdict = "GOOD" if n >= 10 else "DEAD"
        good += verdict == "GOOD"
        dead += verdict == "DEAD"
        print(f"iter {i:2d}: {verdict}  delivered={n:5d} ({n/dwell:5.1f}/s)")
        time.sleep(0.8)
    print(f"\n=== {good} GOOD / {dead} DEAD  ({mode}) ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
