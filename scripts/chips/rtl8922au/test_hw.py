"""
Minimal RTL8922AU (Realtek RTL8922A, 802.11be, USB) live bring-up smoke test.

De-risks the PyUSB -> device path on real silicon, which pcap-verify cannot: verify only
proves our byte order matches a recording, never that the device answers in real time.

Validates, driving RTL8922AUDriver directly (no device manager):
  1. Device found on USB (matched by SUPPORTED_IDS VID:PID, so a co-plugged card is untouched)
  2. A single chip-version read returns non-garbage (control-IN works)
  3. driver.connect() completes: firmware upload (bulk-OUT) + power-on + MAC/BB init +
     mac80211 add-interface, all with poll loops converging on live hardware
  4. set_channel() completes for 2.4 GHz and 5 GHz channels (the per-channel tune + RFK)

RX is NOT exercised (the bulk-IN reader / RX descriptor decode is not ported), so no frames
arrive. This script transmits NOTHING.

At SuperSpeed (USB 3) the rtw89 mode switch is skipped. On a USB-2 path the mode switch fires
a force-mode write that RE-ENUMERATES the device to SuperSpeed; the current driver does not
re-acquire the handle, so the FIRST connect() on a fresh USB-2 plug hangs. Re-run once (the
card is now on USB 3) or use a USB-3 port. Verified working at SuperSpeed on the ASUS USB-BE93.

Usage (card plugged in; kernel driver unbound or a wifit3 udev rule + replug):
    uv run python scripts/chips/rtl8922au/test_hw.py [--debug]
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

import libusb_package
import usb.core

from wifit3.chips.rtl8922au.driver import RTL8922AUDriver
from wifit3.chips.rtl8922au import mac

CONNECT_TIMEOUT = 90


def setup_logging(debug: bool):
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def step(label: str):
    print(f"\n--- {label} ---")


def ok(msg: str):
    print(f"[PASS] {msg}")


def fail(msg: str):
    print(f"[FAIL] {msg}")
    sys.exit(1)


def _find_device():
    backend = libusb_package.get_libusb1_backend()
    for did in RTL8922AUDriver.SUPPORTED_IDS:
        dev = usb.core.find(idVendor=did.vid, idProduct=did.pid, backend=backend)
        if dev is not None:
            return dev, did
    return None, None


async def main(args):
    setup_logging(args.debug)

    step("USB Discovery")
    dev, did = _find_device()
    if dev is None:
        ids = ", ".join(f"{d.vid:04x}:{d.pid:04x}" for d in RTL8922AUDriver.SUPPORTED_IDS)
        fail(f"RTL8922AU not found (looked for {ids}). Plug in + unbind the kernel driver.")
    speed = getattr(dev, "speed", None)
    ok(f"Found {did.vid:04x}:{did.pid:04x} at bus {dev.bus}, address {dev.address}, speed={speed}")

    driver = RTL8922AUDriver.from_usb_device(dev, did)

    step("chip-version read (control-IN sanity)")
    try:
        driver._claim_vendor_interface()
        ver = mac.read_chip_ver(driver.transport)
    except Exception as e:  # noqa: BLE001
        fail(f"chip-version read raised {type(e).__name__}: {e} (PyUSB cannot talk to the device)")
    if ver["cid"] in (0x0, 0xFFFF, 0xFFFFFFFF):
        fail(f"chip id reads garbage: {ver} (device not responding)")
    ok(f"cv=0x{ver['cv']:x} acv=0x{ver['acv']:x} cid=0x{ver['cid']:x} aid=0x{ver['aid']:x}")

    step(f"connect() [{CONNECT_TIMEOUT}s timeout]")
    try:
        success = await asyncio.wait_for(
            driver.connect(progress_cb=lambda p, m: print(f"  [{p*100:5.1f}%] {m}")),
            timeout=CONNECT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        fail(f"connect() timed out after {CONNECT_TIMEOUT}s (device hung during init)")
    except Exception as e:  # noqa: BLE001
        fail(f"connect() raised {type(e).__name__}: {e}")
    if not success:
        fail("connect() returned False (check logs above)")
    ok("connect() succeeded: firmware upload + MAC/BB init + add-interface completed on live silicon")

    step("set_channel() [per-channel tune + RFK, 2.4 GHz and 5 GHz]")
    for ch in (1, 6, 36):
        try:
            await asyncio.wait_for(driver.set_channel(ch), timeout=30)
        except asyncio.TimeoutError:
            fail(f"set_channel({ch}) timed out (device hung during the tune)")
        except Exception as e:  # noqa: BLE001
            fail(f"set_channel({ch}) raised {type(e).__name__}: {e}")
        ok(f"set_channel({ch}) completed on live silicon")

    step("Cleanup")
    await driver.close()
    print("\n=== CONNECT + CHANNEL-TUNE BRING-UP PASSED (RX awaits the bulk-IN reader port) ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RTL8922AU live connect() smoke test")
    parser.add_argument("--debug", action="store_true", help="DEBUG-level logging")
    try:
        asyncio.run(main(parser.parse_args()))
    except KeyboardInterrupt:
        print("\n[ABORTED] Keyboard interrupt")
        sys.exit(1)
