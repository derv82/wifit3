"""AR9271 poll-gap load test (live hardware).

ar9271 is the one driver still doing on-loop read+parse (never moved to the
shared RxReaderThread, because its HTC dispatch is async). Hypothesis: under
the app's event-loop load (10 Hz Focus render + registry build), the on-loop
RX is starved and drops fast bursts (the 4-way) while steady traffic trickles
through — explaining "lots of data frames, zero handshakes" in the app even
though a no-load probe captures fine.

Test: count RX frames on a busy channel for N seconds WITHOUT load, then WITH a
synthetic loop hog (busy-spin ~25 ms every 100 ms, ~10 Hz — mimicking heavy UI
ticks). A large drop ⇒ poll gap is real for ar9271 ⇒ the reader-thread port is
the fix.

Run:  uv run python scripts/ar9271/test_pollgap_load.py
"""
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.parent / "src"))

from wifit3.wlan.manager import WlanDeviceManager  # noqa: E402

BUSY_CHANNEL = 1   # 208 unicast-others/7s in the probe — busiest
MEASURE_S = 8


async def _loop_hog(stop_evt: asyncio.Event):
    """Every 100 ms, block the event loop for ~25 ms (synthetic heavy UI tick)."""
    while not stop_evt.is_set():
        end = time.perf_counter() + 0.025
        while time.perf_counter() < end:
            pass  # busy-spin ON the loop thread — nothing else runs
        await asyncio.sleep(0.075)


async def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    mgr = WlanDeviceManager()
    ifaces = await mgr.refresh()
    if not ifaces:
        print("[-] no interface")
        return
    iface = ifaces[0]
    print(f"[+] {iface.name} ({iface.description})")
    if not await iface.connect():
        print("[-] connect failed")
        return

    n = [0]
    iface.driver.register_rx_callback(lambda parsed: n.__setitem__(0, n[0] + 1))

    await iface.set_channel(BUSY_CHANNEL)

    n[0] = 0
    await asyncio.sleep(MEASURE_S)
    baseline = n[0]
    print(f"[*] NO-LOAD : {baseline} frames in {MEASURE_S}s ({baseline/MEASURE_S:.0f}/s)")

    stop = asyncio.Event()
    hog = asyncio.create_task(_loop_hog(stop))
    n[0] = 0
    await asyncio.sleep(MEASURE_S)
    loaded = n[0]
    stop.set()
    await hog
    print(f"[*] LOADED  : {loaded} frames in {MEASURE_S}s ({loaded/MEASURE_S:.0f}/s)")

    if baseline:
        drop = 100 * (baseline - loaded) / baseline
        print(f"[=] frame drop under loop load: {drop:.0f}%  "
              f"({'POLL GAP CONFIRMED' if drop >= 50 else 'modest — poll gap unlikely the sole cause'})")

    await iface.close()
    print("[+] done")


if __name__ == "__main__":
    try:
        asyncio.run(asyncio.wait_for(main(), timeout=90))
    except asyncio.TimeoutError:
        print("[-] overall timeout")
    except Exception as e:
        import traceback
        print(f"[-] error: {e}")
        traceback.print_exc()
