"""A/B test the ONE confirmed divergence from the vendor: when the bulk-IN RX reader starts.

The vendor posts RX URBs only AFTER rtw_hal_init (rtw_intf_start -> rtl8821cu_inirp_init); our
driver.connect() starts the RxReaderThread BEFORE cold_bringup, so the reader hammers bulk-IN
throughout BB/RF init + the analog DC cancellation. If that bus contention perturbs the analog
settling, it would show as the 5 GHz coin toss. This runs N launches each way, interleaved, same
rest, same 5 GHz dwell, and compares the dead rate:

  during : reader started before cold_bringup  (current driver.connect behaviour)
  quiet  : reader started AFTER cold_bringup + relatch  (vendor-faithful ordering)

If 'quiet' collapses the dead rate, the fix is to reorder the reader start in connect(). If 'quiet'
is ALL dead (pipe wedges with no reader during RX-enable), the old "start reader before RX-enable"
note was right. Passive (RX only).

    uv run python scripts/rtl8821cu_dkms/reader_ab.py [pairs] [dwell_s] [rest_s]
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import libusb_package
import usb.core

from wifit3.chips.rtl8821cu_dkms import bringup, efuse, watchdog
from wifit3.chips.rtl8821cu_dkms.driver import Rtl8821cuDkmsDriver
from wifit3.chips.rx_reader import RxReaderThread

CH = 36


async def _connect_quiet(drv: Rtl8821cuDkmsDriver, loop) -> None:
    """driver.connect() with the reader start moved AFTER cold_bringup (vendor ordering)."""
    drv._claim()
    drv.info = await loop.run_in_executor(None, bringup.cold_bringup, drv.transport)
    await loop.run_in_executor(None, drv._relatch_2g_band)
    drv._reader = RxReaderThread(loop, drv._read_once, drv._dispatch, name="8821cu-dkms-rx")
    drv._reader.start()
    drv._wd_state = watchdog.WatchdogState(
        eeprom_thermal=drv.info.eeprom_thermal, thermal_offset=efuse.thermal_offset(drv.info))
    drv._watchdog_task = loop.create_task(drv._watchdog_loop())


async def one(dev, mode: str, dwell: float) -> int:
    drv = Rtl8821cuDkmsDriver(dev)
    n = [0]
    drv.register_rx_callback(lambda p: n.__setitem__(0, n[0] + 1))
    loop = asyncio.get_running_loop()
    if mode == "quiet":
        await _connect_quiet(drv, loop)
    else:
        await drv.connect()
    await drv.set_channel(CH)
    await asyncio.sleep(dwell)
    frames = n[0]
    await drv.close()
    return frames


async def run(pairs: int, dwell: float, rest: float) -> int:
    backend = libusb_package.get_libusb1_backend()
    tally = {"during": [0, 0], "quiet": [0, 0]}   # [good, dead]
    for k in range(pairs):
        for mode in ("during", "quiet"):
            dev = usb.core.find(idVendor=0x0BDA, idProduct=0xC820, backend=backend)
            if dev is None:
                print("no 0bda:c820 device")
                return 1
            try:
                frames = await one(dev, mode, dwell)
            except Exception as e:  # noqa: BLE001
                print(f"pair {k} {mode:6s}: EXCEPTION {type(e).__name__}: {e}")
                await asyncio.sleep(rest)
                continue
            verdict = "GOOD" if frames >= 10 else "DEAD"
            tally[mode][0 if verdict == "GOOD" else 1] += 1
            print(f"pair {k} {mode:6s}: {verdict}  ch36_frames={frames}")
            await asyncio.sleep(rest)

    print(f"\n=== 5 GHz ch{CH} dead rate ===")
    for mode in ("during", "quiet"):
        g, d = tally[mode]
        print(f"  {mode:6s}: {g} GOOD / {d} DEAD  ({d}/{g+d} dead)")
    return 0


if __name__ == "__main__":
    pr = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    dw = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
    rs = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
    raise SystemExit(asyncio.run(run(pr, dw, rs)))
