"""Does RF reg 0x18 (the synth/channel/band word) latch reliably on a 5 GHz tune?

The cold-divergence diff showed the only GOOD-vs-DEAD separators are in the RF 0x18 tune region
(read_rf 0x18 == ctrl 0x2860, LSSI write == 0xc90). RF 0x18 holds the channel[7:0] + band/sub-band
bits + PLL — if it doesn't latch (or a concurrent bulk-IN read drops the LSSI write, as
_relatch_2g_band already guards against), the synth sits on the wrong frequency, the front-end never
locks, RXFF stays 0 = dead RX. This drives the REAL driver (reader thread running, so the write-drop
race is live), tunes 5 GHz, reads RF 0x18 back several times, counts 5 GHz frames, and reports the
RF18 readback GOOD vs DEAD across launches. If dead launches show a wrong/unstable RF18, that's it.

Passive (RX only).  uv run python scripts/rtl8821cu_dkms/rf18_latch.py [iters] [dwell_s]
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

CH = 36   # 5 GHz, central_ch 0x24; expect RF18 low byte 0x24, band bits BIT16|BIT8, BIT17/18 clear


async def one(dev, dwell: float) -> dict:
    drv = Rtl8821cuDkmsDriver(dev)
    n = [0]
    drv.register_rx_callback(lambda p: n.__setitem__(0, n[0] + 1))
    await drv.connect()
    t = drv.transport
    await drv.set_channel(CH)
    # read RF18 a few times (spread over the dwell) to catch instability / a dropped write
    rf18 = []
    end = asyncio.get_running_loop().time() + dwell
    while asyncio.get_running_loop().time() < end:
        rf18.append(await asyncio.get_running_loop().run_in_executor(None, read_rf, t, 0x18))
        await asyncio.sleep(dwell / 5)
    frames = n[0]
    await drv.close()
    return {"frames": frames, "rf18": rf18}


async def run(iters: int, dwell: float) -> int:
    backend = libusb_package.get_libusb1_backend()
    rows = []
    for k in range(iters):
        dev = usb.core.find(idVendor=0x0BDA, idProduct=0xC820, backend=backend)
        if dev is None:
            print("no 0bda:c820 device")
            return 1
        try:
            r = await one(dev, dwell)
        except Exception as e:  # noqa: BLE001
            print(f"launch {k}: EXCEPTION {type(e).__name__}: {e}")
            await asyncio.sleep(2.0)
            continue
        verdict = "GOOD" if r["frames"] >= 10 else "DEAD"
        uniq = sorted({f"0x{v:05x}" for v in r["rf18"]})
        rows.append((verdict, r["rf18"]))
        print(f"launch {k}: {verdict:4s} ch36_frames={r['frames']:5d}  RF18={uniq}")
        await asyncio.sleep(2.0)

    good = [rf for v, rf in rows if v == "GOOD"]
    dead = [rf for v, rf in rows if v == "DEAD"]
    print(f"\n=== {len(good)} GOOD / {len(dead)} DEAD on 5 GHz ch{CH} ===")
    gset = sorted({f"0x{v:05x}" for rf in good for v in rf})
    dset = sorted({f"0x{v:05x}" for rf in dead for v in rf})
    print(f"  RF18 seen on GOOD: {gset}")
    print(f"  RF18 seen on DEAD: {dset}")
    return 0


if __name__ == "__main__":
    it = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    dw = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
    raise SystemExit(asyncio.run(run(it, dw)))
