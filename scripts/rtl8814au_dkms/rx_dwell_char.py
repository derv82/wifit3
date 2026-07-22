"""RTL8814AU — characterize the 2.4 GHz dwell un-stick across channels (noisy vs quiet, 5 GHz).

The dwell fix forces IGI to _IGI_MAX (0x2a) — the busy-CH1 steady state. This measures whether
that's an over-correction elsewhere: it dwells a sequence of channels (optionally hopping between,
to mimic scan↔dwell), logging per bucket the carried IGI, beacon rate, and breadth (unique BSSIDs
in the window). A breadth that *ramps up* as IGI walks down = weak APs missed while IGI sat too
high = over-correction. --no-unstick runs the natural DIG settle for the A/B.

    # noisy vs quiet 2.4, then a 5 GHz dwell right after a 2.4 dwell (Q: does it inherit a bad IGI?)
    uv run python scripts/rtl8814au_dkms/rx_dwell_char.py --dwells 1,11,6,149 --hop-between --ref <BSSID>
    uv run python scripts/rtl8814au_dkms/rx_dwell_char.py --dwells 1,11,6,149 --hop-between --no-unstick
    uv run python scripts/rtl8814au_dkms/rx_dwell_char.py --dwells 1,149 --ref <BSSID>   # 2.4->5 direct
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wifit3.wlan.manager import WlanDeviceManager  # noqa: E402

_HOP = [1, 149]


async def run(args: argparse.Namespace) -> int:
    ref = args.ref.lower() if args.ref else None
    dwells = [int(c) for c in args.dwells.split(",") if c.strip()]
    mgr = WlanDeviceManager()
    ifaces = await mgr.refresh()
    if not ifaces:
        print("[-] no device", file=sys.stderr)
        return 1
    iface = ifaces[0]
    if not await iface.connect(progress_cb=lambda p, m: None):
        print("[-] bring-up failed", file=sys.stderr)
        await mgr.close_all()
        return 1
    iface.driver.unstick_2g_igi = not args.no_unstick
    print(f"[*] un-stick {'OFF (natural DIG)' if args.no_unstick else 'ON (force IGI 0x2a on 2.4 dwell)'}",
          file=sys.stderr)
    beacons: deque = deque(maxlen=200_000)

    def on_rx(pkt) -> None:
        if pkt and pkt.type == "beacon" and pkt.bssid:
            beacons.append((time.monotonic(), pkt.bssid.lower()))
    iface.register_rx_callback(on_rx)

    def igi() -> int:
        st = iface.driver._wd_state
        return getattr(st, "cur_ig_value", 0) if st else 0

    async def hop(secs: float) -> None:
        t_end = time.monotonic() + secs
        i = 0
        while time.monotonic() < t_end:
            await iface.set_channel(_HOP[i % 2], scan=True)
            await asyncio.sleep(0.5)
            i += 1

    try:
        for di, ch in enumerate(dwells):
            if args.hop_between:
                await hop(args.hop_secs)
            await iface.set_channel(ch, scan=False)      # dwell — the fix fires here if 2.4 GHz
            band = "2.4" if ch <= 14 else "5"
            print(f"\n[dwell {di + 1}: ch{ch} ({band} GHz)]  IGI@land=0x{igi():02x}"
                  f"   {'t(s)':>4} {'IGI':>5} {'ref/s':>6} {'all/s':>6} {'nBSSID':>6}", file=sys.stderr)
            t0 = time.monotonic()
            while time.monotonic() - t0 < args.dwell_secs:
                await asyncio.sleep(args.sample_secs)
                now = time.monotonic()
                win = [(t, b) for (t, b) in list(beacons) if t > now - args.sample_secs]
                rf = sum(1 for (_, b) in win if b == ref) / args.sample_secs if ref else 0.0
                nb = len({b for (_, b) in win})
                pad = " " * 22
                print(f"{pad}{now - t0:4.0f} {'0x%02x' % igi():>5} {rf:6.1f} "
                      f"{len(win) / args.sample_secs:6.1f} {nb:>6}", file=sys.stderr)
    finally:
        await mgr.close_all()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Characterize the 8814au 2.4 GHz dwell un-stick per channel.")
    p.add_argument("--dwells", default="1,11,6,149", help="channels to dwell on, in order.")
    p.add_argument("--dwell-secs", type=float, default=30.0)
    p.add_argument("--hop-between", action="store_true", help="hop 2.4<->5 before each dwell (scan sim).")
    p.add_argument("--hop-secs", type=float, default=15.0)
    p.add_argument("--sample-secs", type=float, default=2.0)
    p.add_argument("--no-unstick", action="store_true", help="disable the dwell IGI un-stick (A/B).")
    p.add_argument("--ref", default=None, help="pin a 2.4 GHz reference BSSID (runtime only).")
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
