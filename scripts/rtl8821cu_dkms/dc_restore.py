"""Localize WHICH analog step dm._dc_cancellation fails to restore (the thing that breaks RX).

dc_ab.py proved: running _dc_cancellation intermittently kills RX on both bands; skipping it fixes
both; disabling only the comp output does NOT — so an analog-RESTORE step is left wrong. This dumps
the registers the cal disturbs-and-should-restore, AFTER a full connect(), in normal vs skip_dc mode.
In skip_dc the cal never ran, so those regs sit at their pristine post-init/tune values; any reg that
reads DIFFERENT in normal mode is one the cal failed to restore = the bug locus. Also correlates with
the 5 GHz frame count so we can see which reg's bad value tracks dead RX. Passive (RX only).

    uv run python scripts/rtl8821cu_dkms/dc_restore.py [iters] [dwell_s] [rest_s]
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import libusb_package
import usb.core

from wifit3.chips.rtl8821cu_dkms import dm as dm_mod
from wifit3.chips.rtl8821cu_dkms.driver import Rtl8821cuDkmsDriver
from wifit3.chips.rtl8821cu_dkms.rf import read_rf

CH = 36
_ORIG_DC = dm_mod._dc_cancellation

# (name, addr, mask) — what _dc_cancellation stops then should restart/restore.
_REGS = [
    ("c00_3wireA", 0x0C00, 0xF),    # phydm_stop_3_wire revert -> 0x7 (run); 0x4 = stuck stopped
    ("e00_3wireB", 0x0E00, 0xF),
    ("8b4_ck320", 0x08B4, 1 << 6),  # stop_ck320(false) -> 0 (running)
    ("c50_igi", 0x0C50, 0x7F),      # write_dig restore -> 0x20
    ("a78_dcnf", 0x0A78, 0xFF00),   # CCK DCNF byte1 (cal sets 0)
    ("a9c_dcen", 0x0A9C, 1 << 20),  # DC-comp enable
    ("808_cck", 0x0808, 1 << 28),   # CCK block enable (stop_ic_trx)
]
_RF = [("rf_ef", 0xEF), ("rf_ee", 0xEE), ("rf_3f", 0x3F)]   # LNA-setting banked regs


def _snap(t) -> dict:
    out = {}
    for name, addr, mask in _REGS:
        try:
            sh = (mask & -mask).bit_length() - 1
            out[name] = (t.read32(addr) & mask) >> sh
        except Exception:  # noqa: BLE001
            out[name] = None
    for name, addr in _RF:
        try:
            out[name] = read_rf(t, addr)
        except Exception:  # noqa: BLE001
            out[name] = None
    return out


async def one(dev, mode: str, dwell: float) -> tuple[int, dict]:
    dm_mod._dc_cancellation = (lambda *a, **k: None) if mode == "skip_dc" else _ORIG_DC
    drv = Rtl8821cuDkmsDriver(dev)
    n = [0]
    drv.register_rx_callback(lambda p: n.__setitem__(0, n[0] + 1))
    await drv.connect()
    await drv.set_channel(CH)
    snap = await asyncio.get_running_loop().run_in_executor(None, _snap, drv.transport)
    await asyncio.sleep(dwell)
    frames = n[0]
    await drv.close()
    return frames, snap


async def run(iters: int, dwell: float, rest: float) -> int:
    backend = libusb_package.get_libusb1_backend()
    rows = []
    try:
        for k in range(iters):
            mode = "normal" if k % 2 == 0 else "skip_dc"
            dev = usb.core.find(idVendor=0x0BDA, idProduct=0xC820, backend=backend)
            if dev is None:
                print("no 0bda:c820 device")
                return 1
            try:
                frames, snap = await one(dev, mode, dwell)
            except Exception as e:  # noqa: BLE001
                print(f"{k} {mode}: EXCEPTION {type(e).__name__}: {e}")
                await asyncio.sleep(rest)
                continue
            verdict = "GOOD" if frames >= 10 else "DEAD"
            rows.append((mode, verdict, snap))
            cells = " ".join(f"{nm}={'--' if snap[nm] is None else f'{snap[nm]:x}'}"
                             for nm, *_ in (_REGS + [(n, a) for n, a in _RF]))
            print(f"{k:2d} {mode:7s} {verdict} f={frames:4d}  {cells}")
            await asyncio.sleep(rest)
    finally:
        dm_mod._dc_cancellation = _ORIG_DC

    names = [nm for nm, *_ in _REGS] + [nm for nm, _ in _RF]
    print("\n=== which restored reg differs normal-vs-skip (the cal's failed restore) ===")
    for nm in names:
        nv = sorted({f"{s[nm]:x}" if s[nm] is not None else "--"
                     for m, _, s in rows if m == "normal"})
        sv = sorted({f"{s[nm]:x}" if s[nm] is not None else "--"
                     for m, _, s in rows if m == "skip_dc"})
        flag = "  <<<" if set(nv) != set(sv) else ""
        print(f"  {nm:11s} normal={nv}  skip={sv}{flag}")
    return 0


if __name__ == "__main__":
    it = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    dw = float(sys.argv[2]) if len(sys.argv) > 2 else 2.5
    rs = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
    raise SystemExit(asyncio.run(run(it, dw, rs)))
