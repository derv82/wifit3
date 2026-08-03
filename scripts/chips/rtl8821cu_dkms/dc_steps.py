"""Localize WHICH analog step of _dc_cancellation destabilizes RX (subtractive A/B).

dc_ab proved running _dc_cancellation breaks RX (both bands); skipping it fixes it; it's the analog
steps, not the comp. _dc_cancellation's vendor-faithful wire ops do: stop_ic_trx, IGI->0x7e
(_write_dig), LNA off (_lna_setting), 3-wire stop (_stop_3_wire), ck320 stop (_stop_ck320), measure,
then restore each. This runs connect() with one helper monkeypatched to a no-op per arm and compares
the 5 GHz dead rate. The arm whose removal restores RX (matches 'skip_all') is the destabilizer:

  full      : unmodified _dc_cancellation (baseline erratic)
  skip_all  : _dc_cancellation no-op entirely (known reliable)
  no_lna    : run cal but skip the LNA off/on toggle (leaves LNA at config_radioa's gain)
  no_3wire  : run cal but skip the path-A 3-wire stop/restart
  no_ck320  : run cal but skip the ck320 stop/restart

Passive (RX only).  uv run python scripts/chips/rtl8821cu_dkms/dc_steps.py [rounds] [dwell_s] [rest_s]
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import libusb_package
import usb.core

from wifit3.chips.rtl8821cu_dkms import dm as dm_mod
from wifit3.chips.rtl8821cu_dkms.driver import Rtl8821cuDkmsDriver

CH = 36
ARMS = ("full", "skip_all", "no_lna", "no_3wire", "no_ck320")
_O = {"dc": dm_mod._dc_cancellation, "lna": dm_mod._lna_setting,
      "3wire": dm_mod._stop_3_wire, "ck320": dm_mod._stop_ck320}
_NOP = lambda *a, **k: None  # noqa: E731


def _patch(arm: str) -> None:
    dm_mod._dc_cancellation = _NOP if arm == "skip_all" else _O["dc"]
    dm_mod._lna_setting = _NOP if arm == "no_lna" else _O["lna"]
    dm_mod._stop_3_wire = _NOP if arm == "no_3wire" else _O["3wire"]
    dm_mod._stop_ck320 = _NOP if arm == "no_ck320" else _O["ck320"]


def _restore() -> None:
    dm_mod._dc_cancellation, dm_mod._lna_setting = _O["dc"], _O["lna"]
    dm_mod._stop_3_wire, dm_mod._stop_ck320 = _O["3wire"], _O["ck320"]


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
        _restore()


async def run(rounds: int, dwell: float, rest: float) -> int:
    backend = libusb_package.get_libusb1_backend()
    tally = {a: [] for a in ARMS}
    for k in range(rounds):
        arms = ARMS if k % 2 == 0 else ARMS[::-1]      # alternate order (warm-up control)
        for arm in arms:
            dev = usb.core.find(idVendor=0x0BDA, idProduct=0xC820, backend=backend)
            if dev is None:
                print("no 0bda:c820 device")
                return 1
            try:
                frames = await one(dev, arm, dwell)
            except Exception as e:  # noqa: BLE001
                print(f"r{k} {arm:9s}: EXCEPTION {type(e).__name__}: {e}")
                await asyncio.sleep(rest)
                continue
            tally[arm].append(frames)
            print(f"r{k} {arm:9s}: {'GOOD' if frames >= 10 else 'DEAD'} ch36={frames}")
            await asyncio.sleep(rest)

    print(f"\n=== 5 GHz ch{CH}: dead rate + median by arm ===")
    for a in ARMS:
        xs = tally[a]
        if not xs:
            continue
        dead = sum(1 for x in xs if x < 10)
        med = sorted(xs)[len(xs) // 2]
        print(f"  {a:9s}: {len(xs)-dead} GOOD / {dead} dead  median={med:4d}  range={min(xs)}-{max(xs)}")
    return 0


if __name__ == "__main__":
    rd = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    dw = float(sys.argv[2]) if len(sys.argv) > 2 else 2.5
    rs = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
    raise SystemExit(asyncio.run(run(rd, dw, rs)))
