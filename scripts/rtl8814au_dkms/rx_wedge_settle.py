"""RTL8814AU — is the 5->2 wedge fixed by a SETTLE in the band switch?

rx_wedge_cure.py showed re-issuing switch_wireless_band_2g's writes revives a wedge 100%, so the
first pass from the 5 GHz analog state intermittently fails to 'take'. This tests the settling
hypothesis directly: monkeypatch a delay around switch_wireless_band_2g (no driver edit), induce
N 5->2 crosses, and report the wedge rate vs the delay. If a delay drops the rate to 0, the wedge
is a settle race and the fix is that delay inside the band switch.

  --at after  : delay AFTER the band switch (between clock-gate re-enable and channel select)
  --at before : delay BEFORE it (let the prior 5 GHz state settle first)

    uv run python scripts/rtl8814au_dkms/rx_wedge_settle.py --delay-ms 10 --at after --trials 15
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wifit3.chips.rtl8814au_dkms import chan  # noqa: E402
from wifit3.chips.rtl8814au_dkms.chan import switch_wireless_band_2g  # noqa: E402
from wifit3.chips.rtl8814au_dkms.constants import CHANNELS_5G_NON_DFS  # noqa: E402
from wifit3.wlan.manager import WlanDeviceManager  # noqa: E402

_HOP5 = list(CHANNELS_5G_NON_DFS)
_LAST5 = 149


def install_delay(delay_s: float, at: str, reissue: int) -> None:
    orig = chan.switch_wireless_band_2g

    def wrapped(t, bb_swing):
        if at == "before" and delay_s:
            time.sleep(delay_s)
        for _ in range(reissue):               # reissue>1 = run the band-switch writes N times back-to-back
            orig(t, bb_swing)
        if at == "after" and delay_s:
            time.sleep(delay_s)
    chan.switch_wireless_band_2g = wrapped     # phy_sw_band resolves this module global at call time


async def run(args: argparse.Namespace) -> int:
    if args.delay_ms or args.reissue > 1:
        install_delay(args.delay_ms / 1000.0, args.at, args.reissue)
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
    loop = asyncio.get_running_loop()
    cnt = {"frames": 0}
    iface.register_rx_callback(lambda raw, rssi, ts: cnt.__setitem__("frames", cnt["frames"] + 1))
    dch = args.dwell_ch
    print(f"[*] delay={args.delay_ms}ms at={args.at if args.delay_ms else '-'} "
          f"reissue={args.reissue} dwell_ch={dch} trials={args.trials}\n", file=sys.stderr)

    wedged = 0
    try:
        for trial in range(1, args.trials + 1):
            i = 0
            end = loop.time() + args.hop5_secs
            while loop.time() < end:
                await iface.set_channel(_HOP5[i % len(_HOP5)], scan=True)
                await asyncio.sleep(0.5)
                i += 1
            await iface.set_channel(_LAST5, scan=True)
            await asyncio.sleep(0.3)
            await iface.set_channel(dch, scan=True)          # THE 5->2 cross (with any injected settle)
            if args.fix_after:      # re-issue the band switch as the FINAL step(s), after channel-select
                async with iface.driver._io_lock:
                    for _ in range(args.fix_after):
                        await loop.run_in_executor(
                            None, switch_wireless_band_2g, iface.driver.transport, iface.driver._bb_swing_2g)
            f0 = cnt["frames"]
            await asyncio.sleep(3.0)
            got = cnt["frames"] - f0
            dead = got < args.wedge_th
            wedged += dead
            print(f"  trial {trial:>2}: {got:>4}f/3s  {'WEDGED' if dead else 'ok'}", file=sys.stderr)
    finally:
        await mgr.close_all()
    print(f"\n=== WEDGE RATE: {wedged}/{args.trials}  (delay={args.delay_ms}ms at={args.at} "
          f"reissue={args.reissue}) ===", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="8814au: test whether a settle in the band switch fixes the 5->2 wedge.")
    p.add_argument("--delay-ms", type=float, default=0.0)
    p.add_argument("--at", choices=("before", "after"), default="after")
    p.add_argument("--reissue", type=int, default=1,
                   help="run the band-switch writes N times back-to-back (2 = preventive double, before channel-select).")
    p.add_argument("--fix-after", type=int, default=0, metavar="N",
                   help="re-issue the band switch N times as the FINAL tune step, after channel-select (candidate fix).")
    p.add_argument("--dwell-ch", type=int, default=1)
    p.add_argument("--trials", type=int, default=15)
    p.add_argument("--hop5-secs", type=float, default=7.0)
    p.add_argument("--wedge-th", type=int, default=10)
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
