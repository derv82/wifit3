"""Cold-boot and test bring-up across connected USB Wi-Fi interfaces."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Add src to sys.path so it works without editable install
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wifit3.device.manager import devices, wlan_iface
from wifit3.errors import BringUpError
from wifit3.models.device_id import DeviceID

logger = logging.getLogger("wifit3.bringup")


def configure_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.WARNING
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)


def filter_devices(
    devs: list[DeviceID],
    *,
    all_devs: bool = False,
    chipset: str = "",
    instance: str = "",
) -> list[DeviceID]:
    if instance:
        parts = instance.split(":")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            bus, addr = int(parts[0]), int(parts[1])
            return [d for d in devs if d.bus == bus and d.address == addr]
        print(f"[-] Invalid instance format: {instance} (expected BUS:ADDR)", file=sys.stderr)
        return []

    if all_devs:
        return devs

    if chipset:
        q = chipset.lower()
        return [
            d for d in devs
            if q in f"{d.chipset} {d.vendor or ''} {d.product_name or ''} {d.description}".lower()
        ]

    return []


async def test_bringup_once(dev_id: DeviceID) -> tuple[bool, str]:
    iface = wlan_iface(dev_id)
    if iface is None:
        return False, "wlan_iface returned None (driver unavailable or device busy)"
    try:
        ok = await iface.driver.connect()
        return bool(ok), "connected" if ok else "connect() returned False"
    except BringUpError as e:
        return False, f"BringUpError({e.stage}): {e.detail}"
    except Exception as e:  # noqa: BLE001
        logger.exception("%s @ %s:%s hard crash during connect", dev_id.chipset, dev_id.bus, dev_id.address)
        return False, f"HARD CRASH {type(e).__name__}: {e}"
    finally:
        try:
            await iface.close()
        except Exception:  # noqa: BLE001
            pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Cold-boot and test bring-up across connected USB Wi-Fi interfaces.",
    )
    p.add_argument("--all", action="store_true", help="Bring up all connected supported cards.")
    p.add_argument(
        "--chipset", "--card", "--driver",
        dest="chipset",
        default="",
        help="Filter cards by substring (matches all matching devices, e.g. '8188' or '8821').",
    )
    p.add_argument(
        "--instance",
        default="",
        help="Target exact USB instance BUS:ADDR (e.g. '1:5').",
    )
    p.add_argument(
        "-n",
        type=int,
        default=1,
        help="Number of bring-up cycles to run per card (default: 1).",
    )
    p.add_argument(
        "--settle",
        type=float,
        default=1.0,
        help="Seconds to sleep between cycles to let hardware settle (default: 1.0).",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG logging on console (shows EP0 retries and register traces).",
    )
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    configure_logging(args.debug)

    devs = devices()
    if not devs:
        print("[-] No supported USB Wi-Fi devices detected on the bus.", file=sys.stderr)
        return 1

    targets = filter_devices(devs, all_devs=args.all, chipset=args.chipset, instance=args.instance)
    if not targets:
        print("[!] No cards selected. Specify --all or filter by --chipset / --instance.\n", file=sys.stderr)
        print("Available cards:", file=sys.stderr)
        for d in devs:
            brand = " ".join(x for x in (d.vendor, d.product_name) if x)
            loc = f"{d.bus}:{d.address}" if d.bus is not None else "no WinUSB"
            print(f"  - {d.chipset:12s} {loc:8s} {brand or d.description}", file=sys.stderr)
        return 1

    print(f"[*] Testing bring-up on {len(targets)} card(s), {args.n} cycle(s) each:")
    for d in targets:
        print(f"    - {d.chipset} @ {d.bus}:{d.address} ({d.description})")

    ok = fail = crash = 0
    total = args.n * len(targets)

    for cycle in range(1, args.n + 1):
        for d in targets:
            good, detail = await test_bringup_once(d)
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
            loc = f"{d.bus}:{d.address}"
            print(f"[{cycle:3d}/{args.n}] [{d.chipset:10s} @ {loc:6s}] {tag} {detail}")

            if args.settle:
                await asyncio.sleep(args.settle)

    pct = 100.0 * fail / total if total else 0.0
    print(f"\n=== {ok} OK / {fail} failed ({crash} crashes) over {total} bring-ups ({pct:.0f}% flake) ===")
    return 2 if crash else (1 if fail else 0)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[!] Interrupted.", file=sys.stderr)
