"""RTL8814AU — is the 5→2 dwell wedge a bulk-IN PIPE stall or a BB/AGC block?

Decisive diagnostic. Hops 2.4↔5 GHz, lands on 5 GHz, then dwells ch1 (scan=False) and logs,
per second: raw USB buffers delivered (RxReaderThread._n_produced delta), total parsed frames
(any type, via the rx callback), and ref-AP beacons. During the wedge:
  * raw_bufs ~0             -> the bulk-IN PIPE is stalled (no USB data) — an RX-plumbing fix.
  * raw_bufs>0, frames ~0   -> USB data flows but demod is dead — a BB/AGC state, not the pipe.

    uv run python scripts/rtl8814au_dkms/rx_pipe_probe.py --ref <BSSID>
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wifit3.wlan.manager import WlanDeviceManager  # noqa: E402
from wifit3.dot11.parser import WlanFrameParser  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    ref = args.ref.lower()
    hop = [1, 149]
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
    cnt = {"frames": 0, "ref": 0}

    def on_rx(raw: bytes, rssi: int, ts: float) -> None:
        cnt["frames"] += 1                     # every delivered+parsed frame, any type
        try:
            p = WlanFrameParser.parse_80211_frame(raw, rssi)
        except Exception:  # noqa: BLE001
            return
        if p and p.type == "beacon" and p.bssid and p.bssid.lower() == ref:
            cnt["ref"] += 1
    iface.register_rx_callback(on_rx)
    reader = iface.driver._reader

    def produced() -> int:
        return getattr(reader, "_n_produced", 0) if reader else 0

    try:
        t_end = time.monotonic() + args.hop_secs
        i = 0
        while time.monotonic() < t_end:
            await iface.set_channel(hop[i % 2], scan=True)
            await asyncio.sleep(0.5)
            i += 1
        await iface.set_channel(149, scan=True)            # land on 5 GHz
        await asyncio.sleep(0.5)
        print(f"[*] dwell ch1 (scan=False) after 5 GHz, {int(args.dwell_secs)}s\n"
              f"  {'sec':>3} {'raw_bufs/s':>10} {'frames/s':>9} {'ref_bcn/s':>10}", file=sys.stderr)
        await iface.set_channel(1, scan=False)             # the dwell tune (band switch)
        p0, f0, r0 = produced(), cnt["frames"], cnt["ref"]
        for s in range(int(args.dwell_secs)):
            await asyncio.sleep(1.0)
            p1, f1, r1 = produced(), cnt["frames"], cnt["ref"]
            print(f"  {s + 1:>3} {p1 - p0:>10} {f1 - f0:>9} {r1 - r0:>10}", file=sys.stderr)
            p0, f0, r0 = p1, f1, r1
    finally:
        await mgr.close_all()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="8814au: pipe-stall vs BB/AGC-block diagnostic for the 5->2 wedge.")
    p.add_argument("--ref", required=True)
    p.add_argument("--hop-secs", type=float, default=40.0)
    p.add_argument("--dwell-secs", type=float, default=25.0)
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
