"""RTL8814AU — pinpoint the register(s) behind the hop→dwell 2.4 GHz RX wedge.

rx_death_repro.py proved the wedge and that a re-tune / the DIG watchdog's writes cure it.
This finds WHICH register state is wrong: it snapshots the BB RX registers while RX is
healthy, again once wedged (DIG frozen so the wedge is stable), and again after the curing
re-tune — then diffs. A register that differs in the wedged snapshot but matches in both
healthy ones is a prime suspect for the missing periodic phydm_watchdog member.

Runs on our port (card wifit3-bound). --ref pins a 2.4 GHz AP to confirm the RX state at each
snapshot (healthy = beacons flowing, wedged = ~0).

    uv run python scripts/rtl8814au_dkms/rx_wedge_regdiff.py --ref <BSSID>
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

# BB register banks that carry the 2.4 GHz RX path: OFDM/DIG (0x800), CCK (0xA00), path-A (0xC00).
_ADDRS = ([a for a in range(0x800, 0x900, 4)]
          + [a for a in range(0xA00, 0xB00, 4)]
          + [a for a in range(0xC00, 0xD00, 4)])


async def run(args: argparse.Namespace) -> int:
    ref = args.ref.lower() if args.ref else None
    mgr = WlanDeviceManager()
    ifaces = await mgr.refresh()
    if not ifaces:
        print("[-] no device", file=sys.stderr)
        return 1
    iface = ifaces[0]
    iface.driver.enable_dig = False        # freeze the watchdog so the wedge is stable
    print(f"[*] bring up {iface.name} (DIG frozen)", file=sys.stderr)
    if not await iface.connect(progress_cb=lambda p, m: None):
        print("[-] bring-up failed", file=sys.stderr)
        await mgr.close_all()
        return 1
    t = iface.driver.transport
    beacons: deque = deque(maxlen=100_000)

    def on_rx(raw: bytes, rssi: int, ts: float) -> None:
        try:
            p = WlanFrameParser.parse_80211_frame(raw, rssi)
        except Exception:  # noqa: BLE001
            return
        if p and p.type == "beacon" and p.bssid:
            beacons.append((time.monotonic(), p.bssid.lower()))
    iface.register_rx_callback(on_rx)

    async def rate(secs: float) -> tuple[float, float]:
        beacons.clear()
        await asyncio.sleep(secs)
        now = time.monotonic()
        win = [b for (ts_, b) in beacons if ts_ > now - secs]
        rf = sum(1 for b in win if b == ref) / secs if ref else 0.0
        return len(win) / secs, rf

    def snap() -> dict[int, int]:
        return {a: t.read32(a) for a in _ADDRS}

    try:
        await iface.set_channel(1)
        allr, refr = await rate(8)
        print(f"[HEALTHY-cold] ch1: {allr:.1f}/s all, {refr:.1f}/s ref", file=sys.stderr)
        healthy1 = snap()

        wedged = None
        for attempt in range(4):            # hop 60s@0.5s, sit, retry until RX confirmed dead
            for i in range(120):
                await iface.set_channel(1 if i % 2 == 0 else 149)
                await asyncio.sleep(0.5)
            await iface.set_channel(1)      # land on 2.4 and sit
            allr, refr = await rate(8)
            print(f"[wedge attempt {attempt + 1}] ch1 after hop: {allr:.1f}/s all, {refr:.1f}/s ref",
                  file=sys.stderr)
            if allr < 1:
                print("  WEDGE CONFIRMED", file=sys.stderr)
                wedged = snap()
                break
        if wedged is None:
            print("  could not induce the wedge — snapshot is inconclusive", file=sys.stderr)
            wedged = snap()

        await iface.set_channel(1)          # the curing re-tune
        allr, refr = await rate(8)
        print(f"[HEALTHY-2 ] ch1 after re-tune: {allr:.1f}/s all, {refr:.1f}/s ref", file=sys.stderr)
        healthy2 = snap()

        print("\n=== registers differing in the WEDGED snapshot ===", file=sys.stderr)
        hits = 0
        for a in _ADDRS:
            w, h1, h2 = wedged[a], healthy1[a], healthy2[a]
            if w != h1 or w != h2:
                tag = " <== suspect (wedged differs, both healthy agree)" if (h1 == h2 and w != h1) else ""
                print(f"  0x{a:04x}: healthy1={h1:08x} wedged={w:08x} healthy2={h2:08x}{tag}",
                      file=sys.stderr)
                hits += 1
        if not hits:
            print("  (no BB-register differences — the wedge is elsewhere: RF, MAC, or pipe)",
                  file=sys.stderr)
    finally:
        await mgr.close_all()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Diff BB RX registers healthy vs wedged (8814au hop→dwell).")
    p.add_argument("--ref", default=None, help="pin a 2.4 GHz reference BSSID (runtime only).")
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
