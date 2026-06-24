"""Disambiguate 'bring-up dead' from '2.4 GHz-only dead': per launch, count RX on ch1 (2.4 GHz, where
connect() parks and the RF18 relatch can fail) THEN on ch36 (5 GHz, the reliable band), and read the
RF18 5G-band bit. If dead launches are 'ch1 dead + ch36 ALIVE', bring-up is fine and the real fault
is the 2.4 GHz relatch — the earlier ch1-parked loops were measuring the wrong thing.

Passive (RX only, no TX).  uv run python scripts/rtl8821cu_dkms/bringup_bands.py [iters] [per_band_s]
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import libusb_package
import usb.core

from wifit3.chips.rtl8821cu_dkms.driver import Rtl8821cuDkmsDriver
from wifit3.chips.rtl8821cu_dkms.rf import read_rf


async def one(dev, per_band: float) -> tuple[int, int, int]:
    """connect (ch1), count ch1, tune ch36, count ch36; return (n_ch1, n_ch36, rf18_bit16)."""
    drv = Rtl8821cuDkmsDriver(dev)
    n = [0]
    drv.register_rx_callback(lambda p: n.__setitem__(0, n[0] + 1))
    await drv.connect()
    t = drv.transport
    await asyncio.sleep(per_band)
    n_ch1 = n[0]
    bit16 = (read_rf(t, 0x18) >> 16) & 1
    await drv.set_channel(36)
    base = n[0]
    await asyncio.sleep(per_band)
    n_ch36 = n[0] - base
    await drv.close()
    return n_ch1, n_ch36, bit16


async def run(iters: int, per_band: float) -> int:
    backend = libusb_package.get_libusb1_backend()
    rows = []
    for i in range(iters):
        dev = usb.core.find(idVendor=0x0BDA, idProduct=0xC820, backend=backend)
        if dev is None:
            print("no 0bda:c820 device")
            return 1
        try:
            n1, n36, bit16 = await one(dev, per_band)
        except Exception as e:  # noqa: BLE001
            print(f"iter {i:2d}: EXCEPTION {e!r}")
            await asyncio.sleep(1.0)
            continue
        v1 = "GOOD" if n1 >= 10 else "dead"
        v36 = "GOOD" if n36 >= 10 else "dead"
        rows.append((v1, v36))
        print(f"iter {i:2d}: ch1(2.4G)={v1:4s} {n1:5d}  |  ch36(5G)={v36:4s} {n36:5d}  |  RF18.bit16={bit16}")
        await asyncio.sleep(0.8)
    a2 = sum(1 for v1, _ in rows if v1 == "GOOD")
    a5 = sum(1 for _, v36 in rows if v36 == "GOOD")
    both_dead = sum(1 for v1, v36 in rows if v1 == "dead" and v36 == "dead")
    print(f"\n=== over {len(rows)} launches: ch1(2.4G) GOOD {a2}, ch36(5G) GOOD {a5}, "
          f"BOTH dead {both_dead} ===")
    if a5 > a2 and both_dead < len(rows):
        print("-> bring-up is largely fine; the failure is 2.4 GHz-specific (RF18 relatch), not power-on")
    elif both_dead == len(rows):
        print("-> genuinely all-bands dead: a real bring-up failure")
    return 0


if __name__ == "__main__":
    it = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    pb = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0
    raise SystemExit(asyncio.run(run(it, pb)))
