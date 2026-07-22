"""RTL8814AU — find which intervention revives the hop→dwell wedged 2.4 GHz RX.

The register diff showed the BB *control* regs are identical healthy vs wedged (only CCK FA
counters differ), so the wedge is a stuck AGC/front-end state, not a wrong threshold. This
induces the wedge (DIG frozen, so it's stable) then tries interventions one at a time, measuring
beacon recovery after each — the first that revives RX names the fix:

  1. raise IGI to 0x2a (all 4 paths)            — de-sensitise (anti-saturation)
  2. raise CCK-PD to LV_1 (0xa0a=0x83)          — CCK de-sensitise
  3. one full watchdog tick (DIG+CCK-PD+...)    — the vendor's per-tick refresh
  4. re-tune set_channel(1)                     — the known (if flaky) cure

    uv run python scripts/rtl8814au_dkms/rx_wedge_poke.py --ref <BSSID>
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
from wifit3.chips.rtl8814au_dkms.dig import _REG_IGI, _IGI_MASK  # noqa: E402
from wifit3.chips.rtl8814au_dkms.watchdog import _REG_CCK_PD, tick as wd_tick  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    ref = args.ref.lower() if args.ref else None
    mgr = WlanDeviceManager()
    ifaces = await mgr.refresh()
    if not ifaces:
        print("[-] no device", file=sys.stderr)
        return 1
    iface = ifaces[0]
    iface.driver.enable_dig = False
    if not await iface.connect(progress_cb=lambda p, m: None):
        print("[-] bring-up failed", file=sys.stderr)
        await mgr.close_all()
        return 1
    t = iface.driver.transport
    st = iface.driver._wd_state
    beacons: deque = deque(maxlen=100_000)

    def on_rx(pkt) -> None:
        if pkt and pkt.type == "beacon" and pkt.bssid:
            beacons.append((time.monotonic(), pkt.bssid.lower()))
    iface.register_rx_callback(on_rx)

    async def rate(secs: float) -> float:
        beacons.clear()
        await asyncio.sleep(secs)
        return len(beacons) / secs

    def set_igi(v: int) -> None:
        for reg in _REG_IGI:
            cur = t.read32(reg)
            t.write32(reg, (cur & ~_IGI_MASK) | (v & _IGI_MASK))

    def rewrite_igi_same() -> None:
        for reg in _REG_IGI:           # write each IGI reg back with its exact current value
            t.write32(reg, t.read32(reg))

    async def induce_wedge() -> bool:
        for attempt in range(4):
            for i in range(120):
                await iface.set_channel(1 if i % 2 == 0 else 149)
                await asyncio.sleep(0.5)
            await iface.set_channel(1)
            r = await rate(8)
            print(f"[wedge attempt {attempt + 1}] {r:.1f}/s all", file=sys.stderr)
            if r < 1:
                return True
        return False

    try:
        await iface.set_channel(1)
        print(f"[healthy-cold] {await rate(6):.1f}/s", file=sys.stderr)
        if not await induce_wedge():
            print("[-] could not induce the wedge", file=sys.stderr)
            return 1
        print("  WEDGE CONFIRMED\n", file=sys.stderr)

        interventions = [
            ("re-write IGI (same value)", rewrite_igi_same),
            ("raise IGI->0x2a", lambda: set_igi(0x2A)),
            ("CCK-PD->LV_1(0x83)", lambda: t.write8(_REG_CCK_PD, 0x83)),
            ("one watchdog tick", lambda: wd_tick(t, st, 1)),
            ("re-tune set_channel(1)", None),
        ]
        for name, fn in interventions:
            if fn is None:
                await iface.set_channel(1)
            else:
                fn()
            r = await rate(6)
            verdict = "REVIVED RX  <===" if r > 3 else "no effect"
            print(f"[intervention] {name:26s} -> {r:.1f}/s  {verdict}", file=sys.stderr)
            if r > 3:
                break
    finally:
        await mgr.close_all()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Poke the wedged 8814au RX to find the reviving action.")
    p.add_argument("--ref", default=None, help="pin a 2.4 GHz reference BSSID (runtime only).")
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
