"""Run the production bring-up path (manager → iface.connect) headless and print the outcome
and exception type. Optional arg = seconds to channel-hop and count APs (passive RX, no TX).

    uv run python scripts/rtl8187/headless_bringup.py [watch_seconds]

Exit 0 = up; 2 = BringUpPermissionsError; 3 = BringUpError; 4 = other.
"""
from __future__ import annotations

import asyncio
import sys

from wifit3.errors import BringUpError, BringUpPermissionsError
from wifit3.wlan.manager import WlanDeviceManager


def _p(pct: float, msg: str) -> None:
    print(f"  [{int(pct * 100):3d}%] {msg}")


async def main() -> int:
    mgr = WlanDeviceManager()
    ifaces = await mgr.refresh()
    target = next((i for i in ifaces if i.vid == 0x0BDA and i.pid == 0x8187), None)
    if target is None:
        print("RTL8187 (0bda:8187) not discovered on the bus.")
        return 4
    node = mgr.usb_node_path(target)
    print(f"found {target.description} @ node {node}  (needs_permission="
          f"{mgr.linux_needs_permission(target)})")
    try:
        ok = await target.connect(progress_cb=_p)
    except BringUpPermissionsError as e:
        print(f"\n==> BringUpPermissionsError (stage={e.stage!r}): {e.detail}")
        print("    splash would: offer the udev-rule install.")
        return 2
    except BringUpError as e:
        print(f"\n==> BringUpError (stage={e.stage!r}): {e.detail}")
        print("    splash would: surface a real fault (NOT a permission prompt).")
        return 3
    except Exception as e:  # noqa: BLE001
        print(f"\n==> {type(e).__name__}: {e}")
        return 4
    else:
        if ok and len(sys.argv) > 1:
            secs = float(sys.argv[1])
            print(f"\n  passive RX check: hopping 2.4GHz for {secs:.0f}s (no TX)…")
            await target.start_hopping([1, 6, 11])
            await asyncio.sleep(secs)
            await target.stop_hopping()
            aps = target.get_access_points()
            print(f"  RX result: {len(aps)} AP(s) seen")
            for ap in aps[:8]:
                print(f"    {getattr(ap, 'bssid', '?')}  ch{getattr(ap, 'channel', '?'):>3}  "
                      f"{getattr(ap, 'ssid', '') or '<hidden>'}")
    finally:
        await target.close()
    print(f"\n==> connect() returned {ok}")
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
