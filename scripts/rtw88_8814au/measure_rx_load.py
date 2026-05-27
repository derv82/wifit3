"""Measure RX dispatch load for the RTL8814AU reader-thread path.

Reproduces the *product* path (driver.connect() + RxReaderThread dispatching on
the asyncio loop) WITHOUT the TUI, to confirm/deny the hypothesis that the
event loop can't drain RX buffers as fast as the reader thread produces them
on this 4T4R promiscuous card (→ periodic UI lag + apparent "card death").

Run:  WIFIT3_RX_STATS=1 uv run scripts/rtw88_8814au/measure_rx_load.py [seconds]

Watch the per-2s STATS line from RxReaderThread:
  - pending hwm climbing toward the cap (256) + dropped>0  => loop can't keep up
  - avg_dispatch high (ms)                                 => parse/callback heavy
The frames/s + a simulated-UI-stall probe are printed here.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time

import libusb_package
import usb.core

from wifit3.chips.rtw88_8814au.driver import RTL8814AUDriver
from wifit3.wlan.interface import WlanInterface
from wifit3.wlan.packet import WlanFrameParser


async def main(run_s: float, hop: bool) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    backend = libusb_package.get_libusb1_backend()
    dev = None
    for did in RTL8814AUDriver.SUPPORTED_IDS:
        dev = usb.core.find(idVendor=did.vid, idProduct=did.pid, backend=backend)
        if dev:
            break
    if dev is None:
        print("No RTL8814AU found. Plug in the AWUS1900.")
        return 1

    driver = RTL8814AUDriver.from_usb_device(dev, None)

    # Drive the REAL UI consumer path: WlanInterface._on_frame_parsed does the
    # full AP/client registry update + sibling recompute + WEP tally per frame.
    # That is what actually runs on the loop in the TUI; a bare counter would
    # under-report the dispatch cost.
    iface = WlanInterface(driver, "measure", "rtl8814au")
    counts = {"frames": 0, "beacons": 0}

    def on_frame(parsed: dict) -> None:
        counts["frames"] += 1
        if parsed.get("subtype_id") == WlanFrameParser.SUBTYPE_BEACON:
            counts["beacons"] += 1
        iface._on_frame_parsed(parsed)

    driver.register_rx_callback(on_frame)

    print("Connecting (bring-up + RF-deaf retry)…")
    if not await driver.connect():
        print("connect() failed.")
        return 1
    print(f"Online. MAC={driver.mac_address}. Measuring {run_s:.0f}s…\n")

    # Probe event-loop responsiveness: a 100 ms timer that records how late it
    # actually fires. If dispatch hogs the loop, these ticks slip — that slip IS
    # the UI lag the user feels (Textual's render timer slips the same way).
    max_slip = {"ms": 0.0}
    last_frames = 0

    async def loop_probe() -> None:
        nonlocal last_frames
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(0.1)
            slip = (time.perf_counter() - t0 - 0.1) * 1e3
            max_slip["ms"] = max(max_slip["ms"], slip)

    last_tune = {"ms": 0.0, "ch": 0}

    async def per_second() -> None:
        nonlocal last_frames
        while True:
            await asyncio.sleep(1.0)
            fps = counts["frames"] - last_frames
            last_frames = counts["frames"]
            dead = "  <<< NO FRAMES (death?)" if fps == 0 else ""
            print(f"  frames/s={fps:5d}  total={counts['frames']:6d}  "
                  f"APs={len(iface.access_points):4d} clients={len(iface.clients):4d}  "
                  f"max_loop_slip={max_slip['ms']:7.1f}ms  "
                  f"last_tune=ch{last_tune['ch']}:{last_tune['ms']:.0f}ms{dead}")
            max_slip["ms"] = 0.0

    async def hopper() -> None:
        """Mimic the UI channel hopper at the REAL 0.25s interval, across 2.4
        and 5 GHz, to expose the PLL-relock 'death' (RX stops for seconds after
        a retune lands the RF deaf; set_channel never re-verifies it)."""
        if not hop:
            return
        chans = [1, 6, 11, 36, 1, 11, 6, 1]
        i = 0
        while True:
            await asyncio.sleep(0.25)
            ch = chans[i % len(chans)]
            i += 1
            t0 = time.perf_counter()
            ok = await driver.set_channel(ch)
            last_tune["ms"] = (time.perf_counter() - t0) * 1e3
            last_tune["ch"] = ch if ok else -ch

    probe = asyncio.ensure_future(loop_probe())
    ticker = asyncio.ensure_future(per_second())
    hoptask = asyncio.ensure_future(hopper())
    await asyncio.sleep(run_s)
    probe.cancel()
    ticker.cancel()
    hoptask.cancel()

    await driver.close()
    print(f"\nDone. total_frames={counts['frames']} beacons={counts['beacons']}")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    secs = float(args[0]) if args else 20.0
    hop = "--hop" in sys.argv
    raise SystemExit(asyncio.run(main(secs, hop)))
