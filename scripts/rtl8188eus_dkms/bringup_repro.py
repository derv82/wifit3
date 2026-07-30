"""RTL8188EUS bring-up repro: cold-boot the card N times and tally the flakes.

The TP-Link WN722N v2/v3 intermittently fails its cold-boot EFUSE read with libusb ENOENT.
``driver.connect`` now surfaces that as a ``BringUpError`` (not a raw ``USBError`` hard crash),
and ``soak_all`` retries it. This loop drives ``driver.connect()`` directly, N times, to measure
how often it flakes AND to confirm each failure is a clean ``BringUpError`` and never a raw
exception. A raw exception here means the wrapper regressed, so the script exits non-zero if one
appears.

One bring-up at a time (serialized), so it isolates the card's own cold-boot flakiness from any
bus contention. Passive: it connects, tallies, and closes; it does not soak or TX.

    uv run python scripts/rtl8188eus_dkms/bringup_repro.py            # 20 bring-ups
    uv run python scripts/rtl8188eus_dkms/bringup_repro.py 50 --settle 2
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wifit3.errors import BringUpError
from wifit3.wlan.discovery import build_interface, find_devices

_CHIPSET = "RTL8188EUS"


async def _one(settle: float) -> tuple[bool | None, str]:
    """Discover + cold-boot the 8188 once. Returns (ok, detail); ok is None when the card isn't
    on the bus. Closes the card either way, then settles."""
    dev_id = next((d for d in find_devices() if d.chipset == _CHIPSET), None)
    if dev_id is None:
        return None, f"no {_CHIPSET} on the bus"
    iface = build_interface(dev_id)
    if iface is None:
        return None, "build_interface returned None"
    try:
        ok = await iface.driver.connect()
        return bool(ok), "connected" if ok else "connect() returned False"
    except BringUpError as e:
        return False, f"BringUpError({e.stage}): {e.detail}"
    except Exception as e:              # noqa: BLE001 — a raw exception here is the regression
        return False, f"HARD CRASH {type(e).__name__}: {e}"
    finally:
        try:
            await iface.close()
        except Exception:              # noqa: BLE001 — teardown fault must not skew the tally
            pass
        if settle:
            await asyncio.sleep(settle)


async def main() -> int:
    p = argparse.ArgumentParser(description="Cold-boot the RTL8188EUS N times and tally the flakes.")
    p.add_argument("n", nargs="?", type=int, default=20, help="number of bring-ups (default: 20)")
    p.add_argument("--settle", type=float, default=1.5,
                   help="seconds between bring-ups, to let the card cold-settle. Default: 1.5.")
    p.add_argument("--reset-settle", type=float, default=None,
                   help="override the driver's post-8051-reset settle (seconds), to sweep it.")
    args = p.parse_args()

    if args.reset_settle is not None:
        from wifit3.chips.rtl8188eus_dkms import firmware
        firmware._POST_RESET_SETTLE_S = args.reset_settle
        print(f"[*] post-reset settle overridden to {args.reset_settle}s")

    ok = fail = crash = 0
    for i in range(1, args.n + 1):
        good, detail = await _one(args.settle)
        if good is None:
            print(f"[{i:3d}/{args.n}] SKIP  {detail}")
            return 1
        if good:
            ok += 1
            tag = "OK   "
        else:
            fail += 1
            if detail.startswith("HARD CRASH"):
                crash += 1
                tag = "CRASH"
            else:
                tag = "FAIL "
        print(f"[{i:3d}/{args.n}] {tag} {detail}")

    pct = 100.0 * fail / args.n if args.n else 0.0
    print(f"\n=== {ok} ok / {fail} failed ({crash} hard crash) over {args.n} bring-ups "
          f"({pct:.0f}% flake) ===")
    if crash:
        print("[-] a raw exception leaked: the BringUpError wrapper regressed.")
    return 2 if crash else 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[!] Interrupted.")
        raise SystemExit(130)
