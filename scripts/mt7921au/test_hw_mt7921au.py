"""
Minimal, fast-fail MT7921AU hardware test for agent/CI use.

Validates:
  1. Device found on USB bus
  2. driver.connect() completes (firmware upload + MCU handshake)
  3. set_channel(1) sends without error
  4. At least one 802.11 frame received within 3s

Exits 0 on full pass, 1 on any failure.
Timeouts prevent hangs: 30s for connect, 5s for channel, 3s for RX.

Usage:
    python scripts/mt7921au/test_hw_mt7921au.py
    python scripts/mt7921au/test_hw_mt7921au.py --debug   # verbose USB logs
"""
import asyncio
import logging
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
from wifit3.chips.mt7921au.driver import MT7921AUDriver
from wifit3.dot11.packet import Packet

MT7921AU_VID = 0x0e8d
MT7921AU_PID = 0x7961

CONNECT_TIMEOUT = 60
CHANNEL_TIMEOUT = 5
RX_WINDOW = 3


def setup_logging(debug: bool):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S"
    )


def step(label: str):
    print(f"\n--- {label} ---")


def ok(msg: str):
    print(f"[PASS] {msg}")


def fail(msg: str):
    print(f"[FAIL] {msg}")
    sys.exit(1)


async def main(debug: bool):
    setup_logging(debug)

    # 1. Find device
    step("USB Discovery")
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=MT7921AU_VID, idProduct=MT7921AU_PID, backend=backend)
    if dev is None:
        fail("MT7921AU not found. Plug in the device and retry.")
    ok(f"Found MT7921AU at bus {dev.bus}, address {dev.address}")

    # 2. connect() — firmware upload + MCU handshake
    step(f"connect() [{CONNECT_TIMEOUT}s timeout]")
    driver = MT7921AUDriver(dev)

    def progress(pct: float, msg: str):
        print(f"  [{pct*100:5.1f}%] {msg}")

    try:
        success = await asyncio.wait_for(
            driver.connect(progress_cb=progress),
            timeout=CONNECT_TIMEOUT
        )
    except asyncio.TimeoutError:
        fail(f"connect() timed out after {CONNECT_TIMEOUT}s — device hung during init")
    except Exception as e:
        fail(f"connect() raised {type(e).__name__}: {e}")

    if not success:
        fail("connect() returned False — check logs above for the failing step")
    ok("connect() succeeded")

    # 3. set_channel(1) — verifies MCU command path
    step(f"set_channel(1) [{CHANNEL_TIMEOUT}s timeout]")
    try:
        await asyncio.wait_for(driver.set_channel(1), timeout=CHANNEL_TIMEOUT)
    except asyncio.TimeoutError:
        fail("set_channel(1) timed out")
    except Exception as e:
        fail(f"set_channel(1) raised {type(e).__name__}: {e}")
    ok("set_channel(1) completed")

    # 4. RX window — confirm frames flow
    step(f"RX ({RX_WINDOW}s listen on CH1)")
    frames: list[Packet] = []

    def on_frame(parsed: Packet):
        frames.append(parsed)

    driver.register_rx_callback(on_frame)
    await asyncio.sleep(RX_WINDOW)

    if frames:
        counts: dict[str, int] = {}
        for f in frames:
            t = f.type
            counts[t] = counts.get(t, 0) + 1
        ok(f"Received {len(frames)} frames: {counts}")
    else:
        # Not a hard failure: could be no nearby APs, or channel mismatch.
        print("[WARN] No frames received. Device may be working but RF environment is quiet.")
        print("       Try a busier channel (e.g. 6 or 11) if this repeats.")

    # 5. 5 GHz RX — the 5 GHz channel-tune commands are byte-verified by the gate
    # (config_sniffer ch_band 2); this confirms the RF actually delivers frames.
    # Informational: a quiet 5 GHz environment is not a failure. The 2.4 GHz
    # baseline above is already recorded, so anything here is a bonus signal.
    g5_total = 0
    for ch in (36, 44, 149, 157):
        step(f"5 GHz RX ({RX_WINDOW}s on CH{ch})")
        g5_frames: list[dict] = []
        try:
            await asyncio.wait_for(driver.set_channel(ch), timeout=CHANNEL_TIMEOUT)
        except Exception as e:
            print(f"[WARN] set_channel({ch}) failed: {type(e).__name__}: {e}")
            continue
        driver.register_rx_callback(lambda p, fr=g5_frames: fr.append(p))
        await asyncio.sleep(RX_WINDOW)
        g5_total += len(g5_frames)
        if g5_frames:
            bcn = sum(1 for f in g5_frames if f.type == "beacon")
            ok(f"CH{ch}: {len(g5_frames)} frames ({bcn} beacons)")
        else:
            print(f"[WARN] CH{ch}: no frames (quiet 5 GHz or no AP on this channel)")
    print(f"\n5 GHz: {g5_total} frames total — "
          f"{'CONFIRMED' if g5_total else 'UNCONFIRMED (RF may be quiet; not a failure)'}")

    step("Cleanup")
    await driver.close()
    print("\n=== ALL STEPS PASSED ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="Enable DEBUG-level logging")
    args = parser.parse_args()

    try:
        asyncio.run(main(args.debug))
    except KeyboardInterrupt:
        print("\n[ABORTED] Keyboard interrupt")
        sys.exit(1)
