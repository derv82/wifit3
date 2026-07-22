"""RTL8814AU — localize the 5->2 wedge by finding the MINIMAL cure.

rx_scan_wedge.py proved the wedge is a reader-independent, ~40%-probabilistic hazard of the
5->2 band cross that kills all RX (frames=0, fa=0, RF 0x00=0). This pins WHERE the fault is by
inducing a wedge, then applying one candidate cure mid-dwell and measuring whether RX recovers:

  * retune     — set_channel(dwell_ch) again (same band: re-runs channel-select ONLY, not the
                 band switch). Recovery here => the fault is in the channel-select block.
  * bandswitch — re-issue switch_wireless_band_2g's writes ONLY (no channel change). Recovery
                 here => the fault is the band-switch writes not fully landing (a settle issue).
  * recross    — full set_channel(5GHz) then set_channel(dwell_ch). The known-good control.

Read-only apart from the cure's own register writes (no TX). Reports cure rate + RF 0x00 before/
after, so we also learn whether RF 0x00 tracks RX health (symptom) or is independently stuck.

    uv run python scripts/rtl8814au_dkms/rx_wedge_cure.py --cure bandswitch --wedges 6
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wifit3.chips.rtl8814au_dkms.chan import switch_wireless_band_2g  # noqa: E402
from wifit3.chips.rtl8814au_dkms.constants import CHANNELS_5G_NON_DFS  # noqa: E402
from wifit3.chips.rtl8814au_dkms.rf import _rf_read  # noqa: E402
from wifit3.wlan.manager import WlanDeviceManager  # noqa: E402

_HOP5 = list(CHANNELS_5G_NON_DFS)
_LAST5 = 149


async def run(args: argparse.Namespace) -> int:
    mgr = WlanDeviceManager()
    ifaces = await mgr.refresh()
    if not ifaces:
        print("[-] no device", file=sys.stderr)
        return 1
    iface = ifaces[0]
    drv = iface.driver
    if not await iface.connect(progress_cb=lambda p, m: None):
        print("[-] bring-up failed", file=sys.stderr)
        await mgr.close_all()
        return 1
    t = drv.transport
    loop = asyncio.get_running_loop()
    cnt = {"frames": 0}
    iface.register_rx_callback(lambda pkt: cnt.__setitem__("frames", cnt["frames"] + 1))
    dch = args.dwell_ch

    async def rf00() -> int:
        async with drv._io_lock:
            return await loop.run_in_executor(None, _rf_read, t, "a", 0x00)

    async def frames_in(secs: float) -> int:
        f0 = cnt["frames"]
        await asyncio.sleep(secs)
        return cnt["frames"] - f0

    async def apply_cure() -> None:
        if args.cure == "retune":
            await iface.set_channel(dch, scan=True)
        elif args.cure == "bandswitch":
            async with drv._io_lock:
                await loop.run_in_executor(None, switch_wireless_band_2g, t, drv._bb_swing_2g)
        elif args.cure == "recross":
            await iface.set_channel(_LAST5, scan=True)
            await asyncio.sleep(0.3)
            await iface.set_channel(dch, scan=True)

    print(f"[*] cure={args.cure} dwell_ch={dch}; collecting {args.wedges} wedges\n", file=sys.stderr)
    cured = 0
    wedges = 0
    attempts = 0
    try:
        while wedges < args.wedges and attempts < args.wedges * 6:
            attempts += 1
            i = 0
            end = loop.time() + args.hop5_secs
            while loop.time() < end:
                await iface.set_channel(_HOP5[i % len(_HOP5)], scan=True)
                await asyncio.sleep(0.5)
                i += 1
            await iface.set_channel(_LAST5, scan=True)
            await asyncio.sleep(0.3)
            await iface.set_channel(dch, scan=True)      # THE 5->2 cross
            pre = await frames_in(3.0)
            if pre >= args.wedge_th:
                continue                                  # no wedge this attempt
            wedges += 1
            rf_before = await rf00()
            await apply_cure()
            post = await frames_in(3.0)
            rf_after = await rf00()
            ok = post >= args.wedge_th
            cured += ok
            print(f"  wedge {wedges:>2}: pre={pre:>3}f/3s  RF00={rf_before:#07x} -> "
                  f"[{args.cure}] -> post={post:>4}f/3s RF00={rf_after:#07x}  "
                  f"{'CURED' if ok else 'still dead'}", file=sys.stderr)
    finally:
        await mgr.close_all()
    print(f"\n=== cure '{args.cure}': {cured}/{wedges} wedges revived "
          f"({attempts} attempts) ===", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="8814au: localize the 5->2 wedge by minimal cure.")
    p.add_argument("--cure", choices=("retune", "bandswitch", "recross"), required=True)
    p.add_argument("--dwell-ch", type=int, default=1)
    p.add_argument("--wedges", type=int, default=6, help="collect this many wedges before reporting.")
    p.add_argument("--hop5-secs", type=float, default=8.0)
    p.add_argument("--wedge-th", type=int, default=10, help="frames in 3s below this = dead.")
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
