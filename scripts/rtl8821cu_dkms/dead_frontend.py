"""Catch DEAD 5 GHz launches and instrument the analog front-end on them.

The 5 GHz coin toss is not in the digital read/write trace (good-vs-dead final register state is
byte-identical bar RXFF_PTR/IGI) and not timing — so it's an analog front-end bring-up. This drives
the REAL driver (reader thread running), tunes 5 GHz ch36, dwells counting frames, then dumps a
front-end snapshot (RF synth/lock word + lock bits, DIG/IGI, RX-path enable, false-alarm counters,
RXFF ptr) every launch. At the end it prints each register's value set GOOD-vs-DEAD: the register(s)
that differ on DEAD launches are the analog signature of the failure.

Run a big batch (rapid re-inits raise the dead rate, which is what we want here). Passive (RX only).
    uv run python scripts/rtl8821cu_dkms/dead_frontend.py [iters] [dwell_s] [rest_s]
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

CH = 36

# (name, kind, addr, mask) — kind 'rf' via read_rf, else transport read of that width.
_SNAP = [
    ("RF18", "rf", 0x18, 0xFFFFF),     # channel/band word; [15] = synth lock-progress/cal
    ("RFca", "rf", 0xCA, 0xFFFFF),     # [12] AACK busy (LCK lock-detect)
    ("RFb0", "rf", 0xB0, 0xFFFFF),     # PLL
    ("RFb8", "rf", 0xB8, 0xFFFFF),     # PLL / LDO
    ("IGI",  "r4", 0x0C50, 0x7F),      # DIG initial gain
    ("RXFF", "r4", 0x1118, 0xFFFFFFFF),
    ("ofdmFA", "r4", 0x0F48, 0xFFFF),
    ("cckFA", "r4", 0x0A5C, 0xFFFF),
    ("r808", "r4", 0x0808, 0xFFFFFFFF),
    ("r838", "r4", 0x0838, 0xFFFFFFFF),
    ("rCR",  "r2", 0x0100, 0xFFFF),
    ("rRCR", "r4", 0x0608, 0xFFFFFFFF),
]


def _snap(t) -> dict:
    out = {}
    for name, kind, addr, mask in _SNAP:
        try:
            if kind == "rf":
                v = read_rf(t, addr)
            else:
                v = {"r2": t.read16, "r4": t.read32}[kind](addr)
            out[name] = v & mask
        except Exception:  # noqa: BLE001
            out[name] = None
    return out


async def one(dev, dwell: float) -> tuple[int, dict]:
    drv = Rtl8821cuDkmsDriver(dev)
    n = [0]
    drv.register_rx_callback(lambda p: n.__setitem__(0, n[0] + 1))
    await drv.connect()
    t = drv.transport
    await drv.set_channel(CH)
    await asyncio.sleep(dwell)
    frames = n[0]
    snap = await asyncio.get_running_loop().run_in_executor(None, _snap, t)
    await drv.close()
    return frames, snap


async def run(iters: int, dwell: float, rest: float) -> int:
    backend = libusb_package.get_libusb1_backend()
    rows = []
    for k in range(iters):
        dev = usb.core.find(idVendor=0x0BDA, idProduct=0xC820, backend=backend)
        if dev is None:
            print("no 0bda:c820 device")
            return 1
        try:
            frames, snap = await one(dev, dwell)
        except Exception as e:  # noqa: BLE001
            print(f"launch {k}: EXCEPTION {type(e).__name__}: {e}")
            await asyncio.sleep(rest)
            continue
        verdict = "GOOD" if frames >= 10 else "DEAD"
        rows.append((verdict, snap))
        cells = "  ".join(f"{nm}={'--' if snap[nm] is None else f'{snap[nm]:x}'}"
                          for nm, *_ in _SNAP)
        print(f"launch {k}: {verdict:4s} f={frames:5d}  {cells}")
        await asyncio.sleep(rest)

    good = [s for v, s in rows if v == "GOOD"]
    dead = [s for v, s in rows if v == "DEAD"]
    print(f"\n=== {len(good)} GOOD / {len(dead)} DEAD on 5 GHz ch{CH} ===")
    if not (good and dead):
        print("need both GOOD and DEAD launches — rerun (bigger batch / shorter rest)")
        return 0
    print("front-end register value sets (look for a register that separates GOOD from DEAD):")
    for nm, *_ in _SNAP:
        gv = sorted({f"{s[nm]:x}" if s[nm] is not None else "--" for s in good})
        dv = sorted({f"{s[nm]:x}" if s[nm] is not None else "--" for s in dead})
        sep = "  <<< SEPARATES" if set(gv).isdisjoint(dv) else ""
        print(f"  {nm:7s} good={gv}  dead={dv}{sep}")
    return 0


if __name__ == "__main__":
    it = int(sys.argv[1]) if len(sys.argv) > 1 else 16
    dw = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
    rs = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
    raise SystemExit(asyncio.run(run(it, dw, rs)))
