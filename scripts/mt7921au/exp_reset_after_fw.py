"""
THE key untested lead — run this on a FRESH replug (cold device).

The chip goes silent right after FW_START. Hypothesis: the firmware *is* booting,
but its boot disrupts the USB link in a way userland doesn't re-establish (the
kernel's USB-core does). So: do the faithful load (NO pre-reset, to keep the
clean upload), and the instant FW_START is sent, issue a usb_reset_device(),
re-acquire the device, and poll FW_N9_RDY on the fresh handle. If the firmware
survives the USB reset, FW_N9_RDY comes up and we've booted it.

Important: run on a freshly REPLUGGED (cold) device on the USB-2 path. A USB reset
does NOT clear the MCU's cold-boot state, so a second run needs a physical replug.

Usage: uv run python scripts/mt7921au/exp_reset_after_fw.py [--debug]
"""
import asyncio
import argparse
import logging
import struct
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
# ruff: noqa: F403, F405
from wifit3.chips.mt7921au.constants import (
    MT_CONN_ON_MISC, MT_TOP_MISC2_FW_N9_RDY,
    MT_VEND_READ_RECIPIENT, MT_VEND_READ_REG_REQ,
)

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


class ResetAfterFwLoader(MT7921AUFirmwareLoader):
    """Reset the USB link right after FW_START, re-acquire, and let the FW_N9_RDY
    poll (in load_firmware) run on the fresh handle."""

    def __init__(self, *a, backend=None, delay=0.0, **kw):
        super().__init__(*a, **kw)
        self._backend = backend
        self._delay = delay
        self.reacquired = None

    async def _load_ram(self) -> bool:
        r = await super()._load_ram()   # uploads regions + sends FW_START
        if self._delay:
            logger.info(f"FW_START sent — waiting {self._delay}s (let the firmware boot) before reset...")
            await asyncio.sleep(self._delay)
        else:
            logger.info("FW_START sent — issuing usb_reset_device() immediately...")
        try:
            self.transport.dev.reset()
        except Exception as e:
            logger.warning(f"post-FW_START reset raised {type(e).__name__}: {e}")
        usb.util.dispose_resources(self.transport.dev)

        # Re-acquire, retrying through the Windows re-bind window (Errno 13) and the
        # post-reset settle. Probe each candidate handle with a chip-id read; the
        # device is "back" only when EP0 actually answers.
        d = None
        for i in range(40):
            await asyncio.sleep(0.5)
            cand = find_dev(self._backend)
            if cand is None:
                continue
            try:
                cand.ctrl_transfer(0xC0, MT_VEND_READ_REG_REQ, 0x7001, 0x0200, 4, timeout=500)
                d = cand
                logger.info(f"re-acquired after {0.5*(i+1):.1f}s: bus {d.bus} addr {d.address} "
                            f"speed={getattr(d,'speed',None)}")
                break
            except Exception:
                usb.util.dispose_resources(cand)
        if d is None:
            logger.error("device never answered EP0 after post-FW_START reset (still wedged)")
            return r

        claim_vendor_iface(d)
        self.transport.dev = d          # FW_N9_RDY poll + drainer now use the fresh handle
        self.reacquired = d
        try:
            wv, wi = (MT_CONN_ON_MISC >> 16) & 0xFFFF, MT_CONN_ON_MISC & 0xFFFF
            res = d.ctrl_transfer(MT_VEND_READ_RECIPIENT, MT_VEND_READ_REG_REQ, wv, wi, 4, timeout=800)
            v = struct.unpack("<I", bytes(res))[0]
            logger.info(f"immediate MT_CONN_ON_MISC=0x{v:08x} "
                        f"FW_N9_RDY={'SET' if v & MT_TOP_MISC2_FW_N9_RDY else 'clear'}")
        except Exception as e:
            logger.info(f"immediate post-reset read failed: {type(e).__name__}: {e}")
        return r


async def main(debug, delay):
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s", datefmt="%H:%M:%S")

    backend = libusb_package.get_libusb1_backend()
    dev = find_dev(backend)
    if dev is None:
        logger.error("MT7921AU not found.")
        return 1
    sp = getattr(dev, "speed", None)
    logger.info(f"Found bus {dev.bus} addr {dev.address} speed={sp}")
    if sp != 3:
        logger.warning("NOT on USB-2 HighSpeed (speed!=3) — upload will hit the USB-3 "
                       "4-packet stall before FW_START. Use the half-32X chain.")
    claim_vendor_iface(dev)

    transport = MT7921AUTransport(dev)
    loader = ResetAfterFwLoader(transport, Path(mt_pkg.__file__).parent / "assets",
                                backend=backend, delay=delay)

    logger.info("=== Experiment: faithful load + usb_reset AFTER FW_START ===")
    t0 = time.monotonic()
    ok = await loader.load_firmware()
    logger.info(f"=== load_firmware() -> {ok} in {time.monotonic()-t0:.1f}s ===")
    logger.info("RESULT: " + ("FW_N9_RDY REACHED — the firmware survived the reset, WE BOOTED IT!"
                              if ok else "no boot — firmware did not survive the USB reset either"))
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--delay", type=float, default=0.0,
                    help="seconds to wait after FW_START before the reset (let FW boot first)")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.debug, args.delay)))
