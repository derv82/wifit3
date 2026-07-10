"""RTL8814AU (DKMS) — 2.4 GHz RX-death reproduction harness.

The field symptom: after the scanner hops across 2.4 and 5 GHz, sitting on a 2.4 GHz
channel goes deaf — beacon rate on a known-strong 2.4 GHz AP collapses to ~0-2/s. A
30-min round-robin soak does NOT show it (each 2.4 channel is revisited every ~11 s, so
the DIG never pins), so this harness drives the specific trigger instead:

  Phase HOP  — alternate a 2.4 GHz and a 5 GHz channel (default 1 <-> 149) for --hop-secs.
  Phase SIT  — tune the 2.4 GHz channel and sit for --sit-secs.

Throughout, it samples every --sample-secs: beacons/s (all 2.4 GHz BSSIDs + the pinned
--ref AP), unique BSSID count in the window, and the live DIG IGI
(`driver._wd_state.cur_ig_value`, clamped to [0x1c, 0x2a]; 0x2a == deaf). If SIT craters
while HOP was healthy and the IGI is pinned high, the free-running DIG is the cause —
confirm by re-running with --no-dig (IGI frozen at the M3a seed).

    uv run python scripts/rtl8814au_dkms/rx_death_repro.py                 # 1<->149, sit ch1
    uv run python scripts/rtl8814au_dkms/rx_death_repro.py --ref <BSSID>   # pin a 2.4 GHz AP
    uv run python scripts/rtl8814au_dkms/rx_death_repro.py --no-dig        # DIG-frozen control
    uv run python scripts/rtl8814au_dkms/rx_death_repro.py --skip-hop      # sit-only control

Never commits a BSSID: --ref is supplied at runtime; on-screen output stays on your terminal.
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
from wifit3.wlan.packet import WlanFrameParser  # noqa: E402


def _band(ch: int) -> str:
    return "2.4" if ch <= 14 else "5"


async def run(args: argparse.Namespace) -> int:
    hop = [int(c) for c in args.hop_channels.split(",") if c.strip()]
    ref = args.ref.lower() if args.ref else None

    mgr = WlanDeviceManager()
    ifaces = await mgr.refresh()
    if not ifaces:
        print("[-] no supported device found", file=sys.stderr)
        return 1
    iface = ifaces[0]
    iface.driver.enable_dig = not args.no_dig     # set BEFORE connect (starts the DIG task)
    print(f"[*] bringing up {iface.name} ({iface.description}); "
          f"DIG {'OFF (frozen at M3a seed)' if args.no_dig else 'ON'}", file=sys.stderr)
    if not await iface.connect(progress_cb=lambda p, m: None):
        print("[-] bring-up failed", file=sys.stderr)
        await mgr.close_all()
        return 1

    # (ts, bssid) for every beacon; a diagnostic run is short so a plain deque is plenty.
    beacons: deque = deque(maxlen=200_000)

    def on_rx(raw: bytes, rssi: int, ts: float) -> None:
        try:
            p = WlanFrameParser.parse_80211_frame(raw, rssi)
        except Exception:  # noqa: BLE001
            return
        if p and p.type == "beacon" and p.bssid:
            beacons.append((time.monotonic(), p.bssid.lower()))

    iface.register_rx_callback(on_rx)

    cur = {"ch": 0, "phase": "init"}

    def igi() -> int | None:
        st = getattr(iface.driver, "_wd_state", None)
        return getattr(st, "cur_ig_value", None) if st else None

    async def sampler() -> None:
        print(f"\n{'t(s)':>5} {'phase':<5} {'ch':>4} {'band':>4} "
              f"{'ref/s':>6} {'2.4/s':>6} {'nBSSID':>6} {'IGI':>5}", file=sys.stderr)
        t0 = time.monotonic()
        while True:
            await asyncio.sleep(args.sample_secs)
            now = time.monotonic()
            lo = now - args.sample_secs
            win = [(t, b) for (t, b) in list(beacons) if t > lo]
            # count all beacons in the window as 2.4 GHz only while tuned to 2.4 (RX-death metric)
            ref_n = sum(1 for (_, b) in win if b == ref) if ref else 0
            band24 = _band(cur["ch"]) == "2.4"
            n24 = len(win) if band24 else 0
            nb = len({b for (_, b) in win})
            g = igi()
            print(f"{now - t0:5.0f} {cur['phase']:<5} {cur['ch']:>4} "
                  f"{_band(cur['ch']):>4} {ref_n / args.sample_secs:6.1f} "
                  f"{n24 / args.sample_secs:6.1f} {nb:>6} "
                  f"{('0x%02x' % g) if g is not None else '  -':>5}", file=sys.stderr)

    samp = asyncio.create_task(sampler())
    try:
        if not args.skip_hop:
            print(f"[*] HOP phase: {hop} @ {args.hop_dwell}s for {args.hop_secs}s", file=sys.stderr)
            cur["phase"] = "hop"
            t_end = time.monotonic() + args.hop_secs
            i = 0
            while time.monotonic() < t_end:
                ch = hop[i % len(hop)]
                cur["ch"] = ch
                await iface.set_channel(ch, scan=True)   # scan hop: no dwell un-stick
                await asyncio.sleep(args.hop_dwell)
                i += 1
        rt = args.sit_retune_secs
        print(f"[*] SIT phase: ch{args.sit_channel} for {args.sit_secs}s"
              f"{f' (re-tune every {rt}s)' if rt else ''}", file=sys.stderr)
        cur["phase"] = "sit"
        cur["ch"] = args.sit_channel
        await iface.set_channel(args.sit_channel)
        if rt > 0:
            t_end = time.monotonic() + args.sit_secs
            while time.monotonic() < t_end:
                await asyncio.sleep(rt)
                await iface.set_channel(args.sit_channel)   # periodic re-tune during the dwell
        else:
            await asyncio.sleep(args.sit_secs)
    finally:
        samp.cancel()
        await mgr.close_all()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="RTL8814AU 2.4 GHz RX-death repro (hop 2.4<->5, then sit).")
    p.add_argument("--hop-channels", default="1,149", help="alternated in the HOP phase (2.4,5).")
    p.add_argument("--hop-secs", type=float, default=60.0, help="HOP phase duration.")
    p.add_argument("--hop-dwell", type=float, default=0.5, help="per-channel dwell in HOP.")
    p.add_argument("--sit-channel", type=int, default=1, help="2.4 GHz channel to sit on.")
    p.add_argument("--sit-secs", type=float, default=120.0, help="SIT phase duration.")
    p.add_argument("--sample-secs", type=float, default=3.0, help="timeline sample interval.")
    p.add_argument("--ref", default=None, help="pin a 2.4 GHz reference BSSID (runtime only).")
    p.add_argument("--no-dig", action="store_true", help="freeze IGI at the M3a seed (control).")
    p.add_argument("--skip-hop", action="store_true", help="sit-only control (no HOP phase).")
    p.add_argument("--sit-retune-secs", type=float, default=0.0,
                   help="re-tune set_channel(sit) every N s during SIT (fix-test: does a re-tune "
                        "un-wedge RX?). 0 = tune once and idle.")
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
