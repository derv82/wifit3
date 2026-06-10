"""Reproduces the RX-DMA wedge: two channel tunes running concurrently on the USB device.

Root cause (from wifit3.log @ 20:34:22): a UI view switch cancels the hop task mid
``set_channel``; the ``asyncio.Lock`` releases on ``CancelledError`` but the
``run_in_executor`` THREAD keeps running ``config_channel``, and the next ``set_channel``
spawns a second thread — two threads then issue control transfers to the chip at once and
wedge its RX-DMA.

These tests don't need hardware: a fake device counts the max number of threads inside
``ctrl_transfer`` at once. ``chan.set_channel`` called raw from two threads collides
(>=2); ``driver._tune`` (which holds the new ``_hw_lock``) serializes (==1).
"""
from __future__ import annotations

import threading
import time

from wifit3.chips.rt3070 import chan, constants as C
from wifit3.chips.rt3070.driver import RT3070Driver
from wifit3.chips.rt3070.eeprom import parse_eeprom
from wifit3.chips.rt3070.state import DrvData
from wifit3.chips.rt3070.transport import RT3070Transport


class ConcurrencyDetectingDev:
    """Fake usb.core.Device: records the peak number of threads simultaneously inside
    ctrl_transfer — i.e. concurrent hardware access, the thing that wedges the chip."""

    def __init__(self):
        self._inside = 0
        self.max_concurrent = 0
        self._lk = threading.Lock()

    def ctrl_transfer(self, rt, req, val, idx, data_or_len, timeout=None):
        with self._lk:
            self._inside += 1
            self.max_concurrent = max(self.max_concurrent, self._inside)
        try:
            time.sleep(0.0002)              # widen the window so a real race is caught
            if rt & 0x80:                  # IN/read → 4 bytes (0 ⇒ regbusy 'not busy')
                return b"\x00\x00\x00\x00"
            return len(data_or_len) if not isinstance(data_or_len, int) else 0
        finally:
            with self._lk:
                self._inside -= 1


def _make_eeprom() -> bytes:
    buf = bytearray(512)
    buf[C.EEPROM_NIC_CONF0 * 2] = 0x11     # NIC_CONF0 = 0x0511: RF3020, 1T1R
    buf[C.EEPROM_NIC_CONF0 * 2 + 1] = 0x05
    return bytes(buf)


def _make_driver(dev) -> RT3070Driver:
    d = RT3070Driver.__new__(RT3070Driver)
    d.transport = RT3070Transport(dev, timeout_ms=50)
    d._chip = C.ChipInfo(rt=C.RT3070, rev=C.REV_RT3070F)
    d._eeprom = parse_eeprom(_make_eeprom())
    d._drv = DrvData(calibration_bw20=8, calibration_bw40=139, bbp25=128, bbp26=0)
    d._hw_lock = threading.Lock()
    d._lna_gain = 0
    return d


def _run_two(target) -> int:
    dev = ConcurrencyDetectingDev()
    t1 = threading.Thread(target=lambda: target(dev, 1))
    t2 = threading.Thread(target=lambda: target(dev, 6))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    return dev.max_concurrent


def test_raw_set_channel_from_two_threads_collides():
    """The bug, proven: two un-serialized tunes DO access the device concurrently."""
    eeprom = parse_eeprom(_make_eeprom())
    drv = DrvData(calibration_bw20=8, calibration_bw40=139, bbp25=128, bbp26=0)
    chip = C.ChipInfo(rt=C.RT3070, rev=C.REV_RT3070F)

    def raw(dev, ch):
        chan.set_channel(RT3070Transport(dev, timeout_ms=50), chip, eeprom, drv, ch)

    assert _run_two(raw) >= 2, "expected the race (>=2 threads in ctrl_transfer at once)"


def test_driver_tune_is_serialized_by_hw_lock():
    """The fix: driver._tune holds _hw_lock, so two concurrent tunes never overlap."""
    # Both threads share ONE driver (one _hw_lock), like the real driver.
    dev = ConcurrencyDetectingDev()
    d = _make_driver(dev)
    t1 = threading.Thread(target=lambda: d._tune(1))
    t2 = threading.Thread(target=lambda: d._tune(6))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert dev.max_concurrent == 1, f"_hw_lock failed: {dev.max_concurrent} threads overlapped"
