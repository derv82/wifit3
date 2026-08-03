"""A/B: does phydm_dc_cancellation's analog SIDE-EFFECTS cause the 5 GHz coin toss?

DC cancellation's OUTPUT (0xc10/0xc14) was already exonerated (byte-identical good-vs-dead). But it
also toggles the LNA off then on (RF 0x3f/0xef/0xee/0x33/0x3e), stops/restarts the path-A 3-wire,
and stops/starts ck320 — all mid-bring-up. If that restore doesn't cleanly land on some boots (RF
writes can be dropped), the RX front-end gain is left wrong -> demod floods on noise -> dead. This
runs N launches with DC cancellation RUN vs NO-OP'd (monkeypatched), interleaved, same rest, 5 GHz,
and compares the dead rate. If skipping DC cancellation collapses the dead rate, its analog
side-effects are the culprit (fix: make the LNA/3-wire/ck320 restore robust). No change -> fully
exonerated. (Skipping breaks the byte-gate; this is a hardware diagnostic only.) Passive (RX only).

    uv run python scripts/chips/rtl8821cu_dkms/dc_ab.py [pairs] [dwell_s] [rest_s]
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import libusb_package
import usb.core

from wifit3.chips.rtl8821cu_dkms import dm as dm_mod
from wifit3.chips.rtl8821cu_dkms.bb import set_bb_reg
from wifit3.chips.rtl8821cu_dkms.driver import Rtl8821cuDkmsDriver

CH = 36
MODES = ("normal", "skip_dc")    # skip_dc: no-op _dc_cancellation entirely
_ORIG_DC = dm_mod._dc_cancellation


async def one(dev, mode: str, dwell: float) -> tuple[int, int]:
    dm_mod._dc_cancellation = (lambda *a, **k: None) if mode == "skip_dc" else _ORIG_DC
    drv = Rtl8821cuDkmsDriver(dev)
    cnt = {"5g": 0, "2g": 0, "cur": "5g"}
    drv.register_rx_callback(lambda p: cnt.__setitem__(cnt["cur"], cnt[cnt["cur"]] + 1))
    await drv.connect()
    if mode == "disable_comp":         # DC cal ran; now turn OFF its compensation (0xa9c[20]=0)
        await asyncio.get_running_loop().run_in_executor(
            None, set_bb_reg, drv.transport, 0x0A9C, 1 << 20, 0)
    await drv.set_channel(36)
    cnt["cur"] = "5g"
    await asyncio.sleep(dwell)
    await drv.set_channel(1)           # 5->2.4 GHz switch clears RF18 bit16 so ch1 can RX
    cnt["cur"] = "2g"
    await asyncio.sleep(dwell)
    await drv.close()
    return cnt["5g"], cnt["2g"]


async def run(pairs: int, dwell: float, rest: float) -> int:
    backend = libusb_package.get_libusb1_backend()
    tally = {m: {"5g": [], "2g": []} for m in MODES}    # mode -> band -> [frame counts]
    try:
        for k in range(pairs):
            order = MODES if k % 2 == 0 else MODES[::-1]
            for mode in order:
                dev = usb.core.find(idVendor=0x0BDA, idProduct=0xC820, backend=backend)
                if dev is None:
                    print("no 0bda:c820 device")
                    return 1
                try:
                    n5, n2 = await one(dev, mode, dwell)
                except Exception as e:  # noqa: BLE001
                    print(f"pair {k} {mode:12s}: EXCEPTION {type(e).__name__}: {e}")
                    await asyncio.sleep(rest)
                    continue
                tally[mode]["5g"].append(n5)
                tally[mode]["2g"].append(n2)
                print(f"pair {k} {mode:12s}: 5g={n5:4d} ({'GOOD' if n5>=10 else 'DEAD'})  "
                      f"2g={n2:4d} ({'GOOD' if n2>=5 else 'dead'})")
                await asyncio.sleep(rest)
    finally:
        dm_mod._dc_cancellation = _ORIG_DC

    print("\n=== per-mode summary (frames/dwell) ===")
    for mode in MODES:
        for band, thr in (("5g", 10), ("2g", 5)):
            xs = tally[mode][band]
            if not xs:
                continue
            dead = sum(1 for x in xs if x < thr)
            xs_s = sorted(xs)
            print(f"  {mode:12s} {band}: {len(xs)-dead} GOOD / {dead} dead  "
                  f"median={xs_s[len(xs_s)//2]:4d}  range={min(xs)}-{max(xs)}")
    return 0


if __name__ == "__main__":
    pr = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    dw = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
    rs = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
    raise SystemExit(asyncio.run(run(pr, dw, rs)))
