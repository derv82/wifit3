"""Is a SETTLE DELAY around the ck320 stop/restart the gate-green fix for the RX coin toss?

dc_steps localized the destabilizer to _dc_cancellation's ck320 stop/restart (0x8b4[6]): skipping
just that toggle (no_ck320) makes 5 GHz RX reliable, like skipping the whole cal. The 0x8b4 register
reads back restored, so it's the TRANSIENT of restarting the 320 MHz BB clock that intermittently
leaves the demod un-relocked. If the vendor's RX is steady with the same ops, we're likely proceeding
before the clock re-locks (a wait emits no op -> invisible to verify_pcap, same class as the LDO
DELAY fix). This A/Bs:

  full        : unmodified (erratic baseline)
  ck320_delay : keep the ck320 stop/restart but sleep 2 ms after each (settle) -- the GATE-GREEN fix
  no_ck320    : skip the toggle (known reliable, but drops ops -> breaks the gate)

If ck320_delay matches no_ck320/skip, the fix is a settle delay (faithful + gate stays green).
Passive (RX only).  uv run python scripts/rtl8821cu_dkms/dc_ck320.py [rounds] [dwell_s] [rest_s]
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import libusb_package
import usb.core

from wifit3.chips.rtl8821cu_dkms import dm as dm_mod
from wifit3.chips.rtl8821cu_dkms.driver import Rtl8821cuDkmsDriver

CH = 36
ARMS = ("full", "ck320_delay", "no_ck320")
_O_CK = dm_mod._stop_ck320
_SETTLE_S = 0.002


def _ck320_delayed(t, enable):
    _O_CK(t, enable)
    time.sleep(_SETTLE_S)       # let the 320 MHz clock settle/re-lock after stop and after restart


def _patch(arm: str) -> None:
    if arm == "ck320_delay":
        dm_mod._stop_ck320 = _ck320_delayed
    elif arm == "no_ck320":
        dm_mod._stop_ck320 = lambda *a, **k: None
    else:
        dm_mod._stop_ck320 = _O_CK


async def one(dev, arm: str, dwell: float) -> int:
    _patch(arm)
    try:
        drv = Rtl8821cuDkmsDriver(dev)
        n = [0]
        drv.register_rx_callback(lambda p: n.__setitem__(0, n[0] + 1))
        await drv.connect()
        await drv.set_channel(CH)
        await asyncio.sleep(dwell)
        frames = n[0]
        await drv.close()
        return frames
    finally:
        dm_mod._stop_ck320 = _O_CK


async def run(rounds: int, dwell: float, rest: float) -> int:
    backend = libusb_package.get_libusb1_backend()
    tally = {a: [] for a in ARMS}
    for k in range(rounds):
        arms = ARMS if k % 2 == 0 else ARMS[::-1]
        for arm in arms:
            dev = usb.core.find(idVendor=0x0BDA, idProduct=0xC820, backend=backend)
            if dev is None:
                print("no 0bda:c820 device")
                return 1
            try:
                frames = await one(dev, arm, dwell)
            except Exception as e:  # noqa: BLE001
                print(f"r{k} {arm:11s}: EXCEPTION {type(e).__name__}: {e}")
                await asyncio.sleep(rest)
                continue
            tally[arm].append(frames)
            print(f"r{k} {arm:11s}: {'GOOD' if frames >= 10 else 'DEAD'} ch36={frames}")
            await asyncio.sleep(rest)

    print(f"\n=== 5 GHz ch{CH}: dead rate + median by arm (settle={_SETTLE_S*1e3:.0f}ms) ===")
    for a in ARMS:
        xs = tally[a]
        if not xs:
            continue
        dead = sum(1 for x in xs if x < 10)
        med = sorted(xs)[len(xs) // 2]
        print(f"  {a:11s}: {len(xs)-dead} GOOD / {dead} dead  median={med:4d}  range={min(xs)}-{max(xs)}")
    return 0


if __name__ == "__main__":
    rd = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    dw = float(sys.argv[2]) if len(sys.argv) > 2 else 2.5
    rs = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
    raise SystemExit(asyncio.run(run(rd, dw, rs)))
