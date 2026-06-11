"""
Lead #1 from the pcap investigation: the kernel does usb_reset_device() at probe
start (mt7921/usb.c:201) — the device enumerates TWICE before firmware load. We
never reset it. This replicates that: reset the device first, re-acquire it, then
run the faithful production load. If FW_START survives, the missing clean-USB-state
reset was the blocker.

Run on the USB-2 path (so the upload completes to the FW_START handoff).
Usage: uv run python scripts/mt7921au/exp_usbreset.py [--debug]
"""
import asyncio
import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import libusb_package
import usb.core
import usb.util

import wifit3.chips.mt7921au as mt_pkg
from wifit3.chips.mt7921au.firmware import MT7921AUFirmwareLoader
from wifit3.chips.mt7921au.transport import MT7921AUTransport

VID, PID = 0x0E8D, 0x7961
logger = logging.getLogger("exp")


def find_dev(backend):
    return usb.core.find(idVendor=VID, idProduct=PID, backend=backend)


def claim_vendor_iface(dev):
    for intf in dev.get_active_configuration():
        if intf.bInterfaceClass == 0xFF:
            try:
                usb.util.claim_interface(dev, intf.bInterfaceNumber)
            except Exception as e:
                logger.debug(f"claim: {e}")
            return intf.bInterfaceNumber


async def main(debug):
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s", datefmt="%H:%M:%S")

    backend = libusb_package.get_libusb1_backend()
    dev = find_dev(backend)
    if dev is None:
        logger.error("MT7921AU not found.")
        return 1
    logger.info(f"Found MT7921AU bus {dev.bus} addr {dev.address} speed={getattr(dev,'speed',None)}")

    # --- the lead: usb_reset_device(), as the kernel does at probe start ---
    logger.info("Issuing usb_reset_device() (libusb_reset_device)...")
    try:
        dev.reset()
        logger.info("reset OK")
    except Exception as e:
        logger.warning(f"dev.reset() raised {type(e).__name__}: {e} — re-finding the device")
    usb.util.dispose_resources(dev)

    # The handle is invalid after a reset/re-enumeration — re-acquire.
    await asyncio.sleep(1.0)
    dev = None
    for _ in range(30):
        dev = find_dev(backend)
        if dev is not None:
            break
        await asyncio.sleep(0.2)
    if dev is None:
        logger.error("device did not re-appear after reset")
        return 1
    logger.info(f"re-acquired: bus {dev.bus} addr {dev.address} speed={getattr(dev,'speed',None)}")
    claim_vendor_iface(dev)

    transport = MT7921AUTransport(dev)
    loader = MT7921AUFirmwareLoader(transport, Path(mt_pkg.__file__).parent / "assets")

    logger.info("=== Experiment: usb_reset_device() then faithful load ===")
    t0 = time.monotonic()
    ok = await loader.load_firmware()
    logger.info(f"=== load_firmware() returned {ok} in {time.monotonic()-t0:.1f}s ===")
    if ok:
        logger.info("RESULT: FW_N9_RDY reached. usb_reset_device WAS the missing piece!")
        return 0
    logger.info("RESULT: still no boot — usb_reset_device is not the (sole) fix.")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.debug)))
