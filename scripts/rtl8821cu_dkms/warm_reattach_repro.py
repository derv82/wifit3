"""RTL8821CU 2.4 GHz-deafness repro: does landing on 2.4 GHz after 5 GHz come up deaf?

Background (driver.py:_prime_2g_band): after a 5 GHz tune the RF18 BIT16 5G-band bit can read
stuck-SET and the 2.4 GHz demod decodes 0 frames. Only a real 5G->2.4G band switch re-locks the
LO, and that RF18 write is DROPPED if the bulk-IN RX reader runs concurrently. The cold path primes
2.4 GHz with the reader stopped, so a fresh connect self-heals. This script hunts the two ways the
field bug (BUGS.md) can still bite, with the reader LIVE throughout:

  * runtime    one bring-up, then repeatedly 5G->2.4G (a Focus view pinned to 2.4 GHz never hops)
  * reconnect  close the driver while on 5G (or 2.4G, control), warm-reattach, land on 2.4 GHz

Each 2.4 GHz landing is scored by two independent signals, so a quiet RF room can't fake a pass:
  * beacons   frames heard on the 2.4 GHz channel in a fixed window (healthy here is hundreds)
  * rf18b16   RF18 BIT16 after the tune; SET means the 5G band bit never cleared (LO not relocked)

Each mode alternates a 5G pre-band against a 2.4G pre-band control, so the correlation (deaf only
when the prior band was 5 GHz) is what proves the mechanism, not one card's mood.

Bring-up + RX only, no TX: safe to run unattended on hardware.

    uv run python scripts/rtl8821cu_dkms/warm_reattach_repro.py --mode runtime --iters 12
    uv run python scripts/rtl8821cu_dkms/warm_reattach_repro.py --mode reconnect --iters 6
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wifit3.chips.rtl8821cu_dkms.rf import read_rf
from wifit3.wlan.discovery import build_interface, find_devices

_CHIPSET = "RTL8821CU"
_RF18_5G_BIT = 1 << 16


class BeaconCounter:
    """RX callback: count beacon frames since the last reset()."""

    def __init__(self) -> None:
        self.n = 0

    def reset(self) -> None:
        self.n = 0

    def __call__(self, pkt) -> None:
        if pkt and pkt.type == "beacon":
            self.n += 1


def _find_devid():
    return next((d for d in find_devices() if d.chipset == _CHIPSET), None)


async def _rf18_bit16(iface) -> Optional[bool]:
    """RF18 BIT16 (the 5 GHz band bit) as a bool, or None if the read faults. Read the same way the
    driver does during a channel switch: a control transfer with the RX reader running."""
    loop = asyncio.get_running_loop()
    try:
        rf18 = await loop.run_in_executor(None, read_rf, iface.driver.transport, 0x18)
        return bool(rf18 & _RF18_5G_BIT)
    except Exception as e:  # noqa: BLE001 — a faulted RF read must not abort the soak
        print(f"    [!] RF18 read faulted: {type(e).__name__}: {e}")
        return None


async def _land_2g(iface, counter: BeaconCounter, ch_2g: int, watch: float):
    """Tune to the 2.4 GHz channel, watch beacons for `watch`s, snapshot RF18 BIT16."""
    await iface.set_channel(ch_2g)
    counter.reset()
    await asyncio.sleep(watch)
    beacons = counter.n
    bit16 = await _rf18_bit16(iface)
    return beacons, bit16


def _is_deaf(beacons: int, bit16: Optional[bool], thresh: int) -> bool:
    return beacons <= thresh or bit16 is True


def _row(i, iters, cond, pre_ch, pre_bit16, ch_2g, beacons, bit16, deaf) -> str:
    band = "5G->2.4G" if cond == "5g" else "2.4G->2.4G ctrl"
    return (f"  [{i:2}/{iters}] {band:<15} pre=ch{pre_ch:<3}(bit16={pre_bit16}) -> "
            f"2.4G ch{ch_2g}: beacons={beacons:>4} rf18b16={bit16!s:<5} "
            f"{'DEAF' if deaf else 'ok'}")


def _summary(mode: str, results: dict, thresh: int) -> None:
    print(f"\n=== {mode} summary (deaf = beacons<={thresh} OR RF18 BIT16 stuck-set) ===")
    for cond in ("5g", "2g"):
        rows = results[cond]
        if not rows:
            continue
        n = len(rows)
        deaf = sum(1 for *_, d in rows if d)
        bit16_set = sum(1 for _, b16, _ in rows if b16 is True)
        zero_bcn = sum(1 for bcn, *_ in rows if bcn <= thresh)
        mean_bcn = sum(bcn for bcn, *_ in rows) / n
        label = "5G->2.4G (cross-band)" if cond == "5g" else "2.4G->2.4G (control)"
        print(f"  pre={cond} {label:<22}: DEAF {deaf}/{n}  "
              f"(RF18 BIT16 stuck {bit16_set}/{n}, beacons<={thresh} {zero_bcn}/{n}, "
              f"mean beacons {mean_bcn:.0f})")


async def _bringup(devid):
    iface = build_interface(devid)
    if iface is None:
        raise RuntimeError("build_interface returned None")
    if not await iface.connect():
        raise RuntimeError("connect() returned False")
    return iface


async def run_runtime(args) -> int:
    devid = _find_devid()
    if devid is None:
        print(f"[-] no {_CHIPSET} on the bus")
        return 1
    print(f"[*] runtime mode: one bring-up, {args.iters} iters x (5G pre, 2.4G-control pre)")
    iface = await _bringup(devid)
    counter = BeaconCounter()
    iface.register_rx_callback(counter)
    results = {"5g": [], "2g": []}
    try:
        for i in range(1, args.iters + 1):
            for cond, pre_ch in (("5g", args.ch_5g), ("2g", args.ch_2g_pre)):
                await iface.set_channel(pre_ch)
                pre_bit16 = await _rf18_bit16(iface)     # 5G pre should read SET; 2.4G pre clear
                await asyncio.sleep(args.settle)
                beacons, bit16 = await _land_2g(iface, counter, args.ch_2g, args.watch)
                deaf = _is_deaf(beacons, bit16, args.thresh)
                results[cond].append((beacons, bit16, deaf))
                print(_row(i, args.iters, cond, pre_ch, pre_bit16, args.ch_2g, beacons, bit16, deaf))
    finally:
        await iface.close()
    _summary("runtime", results, args.thresh)
    return 0


def _spawn_worker(action: str, args, **extra) -> Optional[dict]:
    """Run one bring-up in a fresh process (a real app-session boundary: process exit fully releases
    the WinUSB handle, which an in-process close/reopen does NOT on Windows). Returns the worker's
    RESULT dict, or None if it failed. One retry after a longer settle covers a transient open race."""
    argv = [sys.executable, str(Path(__file__).resolve()), "--worker", action,
            "--watch", str(args.watch), "--ch-2g", str(args.ch_2g)]
    for k, v in extra.items():
        argv += [f"--{k.replace('_', '-')}", str(v)]
    for attempt in (1, 2):
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return None
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])
        if attempt == 1:
            import time
            time.sleep(args.reconnect_settle + 2.0)   # let Windows release the handle, retry once
    return None


def run_reconnect(args) -> int:
    print(f"[*] reconnect mode: {args.iters} iters x (close-on-5G, close-on-2.4G control), "
          "each attach in its own process")
    results = {"5g": [], "2g": []}
    for i in range(1, args.iters + 1):
        for cond, pre_ch in (("5g", args.ch_5g), ("2g", args.ch_2g_pre)):
            # session #1: bring up, park on the pre-band, exit (leaves the card warm on pre_ch).
            parked = _spawn_worker("park", args, pre_ch=pre_ch)
            if parked is None:
                print(f"  [{i:2}/{args.iters}] {cond}: park session FAILED (see worker)")
                continue
            # session #2: warm reattach, land on 2.4 GHz, measure.
            m = _spawn_worker("measure", args)
            if m is None:
                print(f"  [{i:2}/{args.iters}] {cond}: measure session FAILED")
                continue
            beacons, bit16 = m["beacons"], m["bit16"]
            deaf = _is_deaf(beacons, bit16, args.thresh)
            results[cond].append((beacons, bit16, deaf))
            print(_row(i, args.iters, cond, pre_ch, "n/a", args.ch_2g, beacons, bit16, deaf))
    _summary("reconnect", results, args.thresh)
    return 0


async def _worker_park(args) -> int:
    """One session that parks the card on --pre-ch and exits, leaving it warm on that band."""
    devid = _find_devid()
    if devid is None:
        return 1
    iface = await _bringup(devid)
    await iface.set_channel(args.pre_ch)
    await asyncio.sleep(args.settle)
    # Intentionally no close(): process exit is the session boundary and releases the handle.
    print(f"RESULT {json.dumps({'parked': args.pre_ch})}")
    return 0


async def _worker_measure(args) -> int:
    """One warm-reattach session: bring up, land on 2.4 GHz, report beacons + RF18 BIT16."""
    devid = _find_devid()
    if devid is None:
        return 1
    iface = await _bringup(devid)
    counter = BeaconCounter()
    iface.register_rx_callback(counter)
    beacons, bit16 = await _land_2g(iface, counter, args.ch_2g, args.watch)
    print(f"RESULT {json.dumps({'beacons': beacons, 'bit16': bit16})}")
    await iface.close()
    return 0


async def main() -> int:
    p = argparse.ArgumentParser(description="RTL8821CU 2.4 GHz-after-5 GHz deafness repro.")
    p.add_argument("--mode", choices=("runtime", "reconnect", "both"), default="both")
    p.add_argument("--iters", type=int, default=10, help="iterations per condition. Default 10.")
    p.add_argument("--watch", type=float, default=4.0, help="2.4 GHz beacon window (s). Default 4.")
    p.add_argument("--settle", type=float, default=1.5, help="dwell on the pre-band (s). Default 1.5.")
    p.add_argument("--reconnect-settle", type=float, default=1.5,
                   help="(reconnect) gap around close/reattach (s). Default 1.5.")
    p.add_argument("--ch-5g", type=int, default=149, help="5 GHz pre-band channel. Default 149.")
    p.add_argument("--ch-2g", type=int, default=1, help="2.4 GHz target channel. Default 1.")
    p.add_argument("--ch-2g-pre", type=int, default=11,
                   help="2.4 GHz control pre-band channel (in-band move). Default 11.")
    p.add_argument("--thresh", type=int, default=5,
                   help="beacons<=thresh counts as deaf. Default 5.")
    p.add_argument("--worker", choices=("park", "measure"), default=None,
                   help="internal: run a single-session worker (spawned by reconnect mode).")
    p.add_argument("--pre-ch", type=int, default=None, help="internal: park worker's channel.")
    args = p.parse_args()

    if args.worker == "park":
        return await _worker_park(args)
    if args.worker == "measure":
        return await _worker_measure(args)

    rc = 0
    if args.mode in ("runtime", "both"):
        rc = await run_runtime(args) or rc
    if args.mode in ("reconnect", "both"):
        rc = run_reconnect(args) or rc
    return rc


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[!] Interrupted.")
        raise SystemExit(130)
