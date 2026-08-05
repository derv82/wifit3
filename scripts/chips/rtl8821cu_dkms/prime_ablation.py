"""RTL8821CU prime-bounce ablation: is the 2.4 GHz-deaf bug the cold tune, not bad luck?

Theory: the cold channel tune (phase_iface set_channel(1)) runs BEFORE the antenna is switched to
WiFi (phase_monitor), so the 2.4 GHz LO never locks and RF18 BIT16 stays set. `_prime_2g_band` papers
over it with a flaky 2.4->5->2.4 toggle. If that's right, this is deterministic, not a 5% flake:

  * --mode off     _prime_2g_band -> no-op          expect: ~always DEAF (cold tune is broken)
  * --mode reband  _prime_2g_band -> current_band=None + set_channel(1)   expect: ~always healthy
  * --mode stock   the shipped 5G-bounce prime       baseline

Each boot runs in its own process (a real session boundary; in-process close/reopen wedges WinUSB on
Windows). Measures the POST-connect state on ch1 with no re-tune: 2.4 GHz beacons + RF18 BIT16.

    uv run python scripts/chips/rtl8821cu_dkms/prime_ablation.py --mode off --iters 5
    uv run python scripts/chips/rtl8821cu_dkms/prime_ablation.py --mode reband --iters 5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from wifit3.chips.rtl8821cu_dkms import driver as drv
from wifit3.chips.rtl8821cu_dkms.rf import read_rf
from wifit3.device.manager import wlan_iface, devices

_CHIPSET = "RTL8821CU"
_RF18_5G_BIT = 1 << 16


class BeaconCounter:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self, pkt) -> None:
        if pkt and pkt.type == "beacon":
            self.n += 1


async def _prime_off(self, loop) -> None:
    """Ablated: skip the LO-relock prime entirely, so the cold tune's state stands."""
    return


async def _prime_reband(self, loop) -> None:
    """Candidate fix: drop the stale 2G band latch and re-tune 2.4 GHz once (now post-antenna-switch).
    set_channel(scan=False) pauses the reader across the RF18 write, so it lands. No 5 GHz detour."""
    self.transport.current_band = None
    await self.set_channel(drv._PRIME_2G_CH)


def _find_devid():
    return next((d for d in devices() if d.chipset == _CHIPSET), None)


async def _worker(args) -> int:
    if args.mode == "off":
        drv.Rtl8821cuDkmsDriver._prime_2g_band = _prime_off
    elif args.mode == "reband":
        drv.Rtl8821cuDkmsDriver._prime_2g_band = _prime_reband
    # "stock": leave the shipped prime in place.
    devid = _find_devid()
    if devid is None:
        return 1
    iface = wlan_iface(devid)
    if not await iface.connect():
        return 1
    counter = BeaconCounter()
    iface.register_rx_callback(counter)
    await asyncio.sleep(args.watch)                       # measure the post-connect state; no re-tune
    loop = asyncio.get_running_loop()
    try:
        rf18 = await loop.run_in_executor(None, read_rf, iface.driver.transport, 0x18)
        bit16 = bool(rf18 & _RF18_5G_BIT)
    except Exception:  # noqa: BLE001
        bit16 = None
    print(f"RESULT {json.dumps({'beacons': counter.n, 'bit16': bit16, 'ch': iface.current_channel})}")
    await iface.close()
    return 0


def _spawn(args) -> Optional[dict]:
    argv = [sys.executable, str(Path(__file__).resolve()), "--worker",
            "--mode", args.mode, "--watch", str(args.watch)]
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
            time.sleep(3.0)
    return None


def run(args) -> int:
    print(f"[*] prime ablation: mode={args.mode}, {args.iters} boots (each in its own process)")
    rows = []
    for i in range(1, args.iters + 1):
        r = _spawn(args)
        if r is None:
            print(f"  [{i:2}/{args.iters}] boot FAILED")
            continue
        deaf = r["beacons"] <= args.thresh or r["bit16"] is True
        rows.append((r, deaf))
        print(f"  [{i:2}/{args.iters}] ch{r['ch']}: beacons={r['beacons']:>4} "
              f"rf18b16={r['bit16']!s:<5} {'DEAF' if deaf else 'ok'}")
    n = len(rows)
    deaf = sum(1 for _, d in rows if d)
    print(f"\n=== mode={args.mode}: DEAF {deaf}/{n} "
          f"(BIT16 stuck {sum(1 for r, _ in rows if r['bit16'] is True)}/{n}) ===")
    return 0


async def main() -> int:
    p = argparse.ArgumentParser(description="RTL8821CU prime-bounce ablation.")
    p.add_argument("--mode", choices=("off", "reband", "stock"), default="off")
    p.add_argument("--iters", type=int, default=5)
    p.add_argument("--watch", type=float, default=4.0)
    p.add_argument("--thresh", type=int, default=5)
    p.add_argument("--worker", action="store_true", help="internal: run one boot + measure.")
    args = p.parse_args()
    if args.worker:
        return await _worker(args)
    return run(args)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[!] Interrupted.")
        raise SystemExit(130)
