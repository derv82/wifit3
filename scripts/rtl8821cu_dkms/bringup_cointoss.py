"""Reproduce the RTL8821CU bring-up coin toss headlessly and find the register signature that
separates a GOOD launch (RX flows) from a DEAD one (control path alive, bulk-IN delivers nothing).

Each iteration is a full soft re-init on the SAME USB enumeration (no replug) — exactly what an app
relaunch does, which is how the coin toss shows up. Per iteration: connect() (cold_bringup), dwell a
few seconds counting delivered frames, sample the RX-FIFO pointer mid-dwell to see if the chip's RX
DMA is even moving, dump a wide RX-path register set, classify good/dead, close. At the end it diffs
the register dumps of the dead launches against the good ones — the registers that differ are the
suspects for the missed reset/cache.

Passive (RX only, no TX/injection).

    uv run python scripts/rtl8821cu_dkms/bringup_cointoss.py [iterations] [dwell_s]
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import libusb_package
import usb.core

from wifit3.chips.rtl8821cu_dkms import btc
from wifit3.chips.rtl8821cu_dkms.driver import Rtl8821cuDkmsDriver
from wifit3.chips.rtl8821cu_dkms.rf import read_rf

# (addr, width, name) — the RX-path / DMA / FIFO / USB / coex state worth comparing good vs dead.
_REGS = [
    (0x0100, 2, "CR"), (0x0102, 1, "MSR"), (0x0608, 4, "RCR"),
    (0x06A0, 2, "RXFLTMAP0"), (0x06A2, 2, "RXFLTMAP1"), (0x06A4, 2, "RXFLTMAP2"),
    (0x0290, 1, "RXDMA_MODE"), (0x010C, 1, "TXDMA_PQ_MAP"),
    (0x0280, 4, "RXDMA_AGG_PG_TH"), (0x0288, 4, "RXDMA_STATUS"), (0x0210, 4, "TXDMA_STATUS"),
    (0x1118, 4, "RXFF_PTR"), (0x011C, 4, "RXFF_BNDY"), (0x0114, 4, "TRXFF_BNDY"),
    (0x0208, 4, "AUTO_LLT_V1"), (0x060F, 1, "RX_DRVINFO_SZ"), (0x060C, 1, "RX_PKT_LIMIT"),
    (0xFE11, 1, "USBSTAT"), (0x00FF, 1, "SYS_CFG2_3"), (0x0C50, 1, "IGI"),
    (0x0073, 1, "COEX_OWNER"), (0x0770, 4, "BT_HI_CTR"), (0x0774, 4, "BT_LO_CTR"),
    # RX-enable / TRX-stop state (dm.stop_ic_trx disables these on a tune and must revert them):
    (0x0808, 4, "R808_cck_rxpath"), (0x0838, 4, "R838_ofdm_cca"), (0x0A04, 4, "Ra04_ccktx"),
    (0x0520, 4, "R520_txpause"), (0x0C00, 4, "Rc00_3wireA"), (0x0900, 4, "R900_ofdm_trx"),
    # DC-offset cancellation result (dm._dc_cancellation measures live + writes these per boot):
    (0x0C10, 4, "Rc10_dcI"), (0x0C14, 4, "Rc14_dcQ"), (0x0A9C, 4, "Ra9c_dcen"), (0x0C1C, 4, "Rc1c_swing"),
]


def _dump(t) -> dict:
    out = {}
    for addr, width, name in _REGS:
        try:
            out[name] = {1: t.read8, 2: t.read16, 4: t.read32}[width](addr)
        except Exception as e:  # noqa: BLE001
            out[name] = f"ERR:{e}"
    try:
        out["RF18"] = read_rf(t, 0x18)
    except Exception as e:  # noqa: BLE001
        out["RF18"] = f"ERR:{e}"
    try:
        out["GNT38"] = btc._read_indirect(t, 0x38)
    except Exception as e:  # noqa: BLE001
        out["GNT38"] = f"ERR:{e}"
    return out


def _fmt(v) -> str:
    return v if isinstance(v, str) else f"0x{v:x}"


async def one(dev, dwell: float) -> tuple[int, list[int], dict]:
    """Run one cold_bringup, dwell, return (delivered_total, rxff_ptr_samples, reg_dump)."""
    drv = Rtl8821cuDkmsDriver(dev)
    n = [0]
    drv.register_rx_callback(lambda p: n.__setitem__(0, n[0] + 1))
    await drv.connect()
    t = drv.transport
    rxff = []
    end = time.monotonic() + dwell
    while time.monotonic() < end:
        try:
            rxff.append(t.read32(0x1118))
        except Exception:  # noqa: BLE001
            rxff.append(-1)
        await asyncio.sleep(0.7)
    dump = _dump(t)
    await drv.close()
    return n[0], rxff, dump


async def run(iters: int, dwell: float, rest: float = 0.8) -> int:
    backend = libusb_package.get_libusb1_backend()
    good, dead = [], []
    rows = []
    for i in range(iters):
        dev = usb.core.find(idVendor=0x0BDA, idProduct=0xC820, backend=backend)
        if dev is None:
            print("no 0bda:c820 device (still ZeroCD / unplugged?)")
            return 1
        try:
            delivered, rxff, dump = await one(dev, dwell)
        except Exception as e:  # noqa: BLE001
            print(f"iter {i:2d}: EXCEPTION during bringup: {e!r}")
            await asyncio.sleep(1.0)
            continue
        rate = delivered / dwell
        verdict = "GOOD" if delivered >= 10 else "DEAD"
        (good if verdict == "GOOD" else dead).append(dump)
        # RXFF pointer movement during the dwell: did the chip's RX FIFO pointer advance at all?
        moved = len({v for v in rxff if v >= 0}) > 1
        print(f"iter {i:2d}: {verdict}  delivered={delivered:5d} ({rate:6.1f}/s)  "
              f"RXFF_moved={moved}  RXFF[{_fmt(rxff[0]) if rxff else '-'}..{_fmt(rxff[-1]) if rxff else '-'}]  "
              f"RXDMA_STATUS={_fmt(dump['RXDMA_STATUS'])} RXFF_PTR={_fmt(dump['RXFF_PTR'])} "
              f"CR={_fmt(dump['CR'])} RCR={_fmt(dump['RCR'])}")
        rows.append((i, verdict, dump))
        await asyncio.sleep(rest)        # let USB / the chip settle between soft re-inits

    print(f"\n=== {len(good)} GOOD / {len(dead)} DEAD over {iters} launches ===")
    if good and dead:
        print("\nregisters that DIFFER between good and dead launches (the signature):")
        for name in good[0]:
            gvals = {_fmt(d[name]) for d in good}
            dvals = {_fmt(d[name]) for d in dead}
            if gvals != dvals:
                print(f"  {name:16s}  good={sorted(gvals)}  dead={sorted(dvals)}")
    elif not dead:
        print("no DEAD launches this run — all good (coin toss didn't trip; rerun / more iters)")
    elif not good:
        print("no GOOD launches this run — all dead (chip may be in a stuck state)")
    return 0


if __name__ == "__main__":
    it = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    dw = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
    rs = float(sys.argv[3]) if len(sys.argv) > 3 else 0.8
    raise SystemExit(asyncio.run(run(it, dw, rs)))
