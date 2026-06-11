"""
Autonomous "throw it at the wall" harness for the MT7921AU FW_START wall.

Combines the pcap-investigation leads behind flags so variants run fast. dev.reset()
is the recovery primitive (recovers a wedged chip without a physical replug), so
experiments chain. Run on USB-2.

  --reset       usb_reset_device() first (kernel does this at probe start)
  --setconfig   set_configuration() after reset (kernel does this after re-enum)
  --epctl       attempt the UHW epctl_rst_opt(false) write the kernel does (Errno 5
                on WinUSB, but try it — throw it at the wall)
  --rxbuf N     drainer IN read size (kernel uses 16384; we default 2048)

Usage: uv run python scripts/mt7921au/exp_throw.py --reset --setconfig [--epctl] [--debug]
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

# MT_SSUSB_EPCTL_CSR_EP_RST_OPT (from pcap UHW RMW + mt792x_usb.c epctl_rst_opt).
EPCTL_RST_OPT = 0x74011890
EPCTL_CLEAR = (0x3F << 4) | (0x7 << 20)   # GENMASK(9,4) | GENMASK(22,20)


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


class ThrowLoader(MT7921AUFirmwareLoader):
    def __init__(self, *a, do_epctl=False, **kw):
        super().__init__(*a, **kw)
        self._do_epctl = do_epctl

    def _rx_evt_ep4(self):
        super()._rx_evt_ep4()
        if self._do_epctl:
            # epctl_rst_opt(false): clear GENMASK(9,4)|GENMASK(22,20) over UHW bus.
            v = self.transport.read_reg32_uhw(EPCTL_RST_OPT)
            new = v & ~EPCTL_CLEAR
            self.transport.write_reg32_uhw(EPCTL_RST_OPT, new)
            logger.info(f"epctl_rst_opt UHW: read 0x{v:08x} -> wrote 0x{new:08x} "
                        f"(if read=0x0 the UHW bus is Errno-5 dead as expected)")


async def main(args):
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s", datefmt="%H:%M:%S")

    backend = libusb_package.get_libusb1_backend()
    dev = find_dev(backend)
    if dev is None:
        logger.error("MT7921AU not found.")
        return 1
    logger.info(f"Found bus {dev.bus} addr {dev.address} speed={getattr(dev,'speed',None)}")

    if args.reset:
        logger.info("usb_reset_device()...")
        try:
            dev.reset()
        except Exception as e:
            logger.warning(f"reset: {e}")
        usb.util.dispose_resources(dev)
        await asyncio.sleep(1.0)
        for _ in range(30):
            dev = find_dev(backend)
            if dev is not None:
                break
            await asyncio.sleep(0.2)
        if dev is None:
            logger.error("device gone after reset")
            return 1
        logger.info(f"re-acquired bus {dev.bus} addr {dev.address}")

    if args.setconfig:
        try:
            dev.set_configuration()
            logger.info("set_configuration() OK")
        except Exception as e:
            logger.warning(f"set_configuration: {e}")

    claim_vendor_iface(dev)
    transport = MT7921AUTransport(dev)
    loader = ThrowLoader(transport, Path(mt_pkg.__file__).parent / "assets", do_epctl=args.epctl)

    flags = [k for k in ("reset", "setconfig", "epctl") if getattr(args, k)]
    logger.info(f"=== throw: {flags or ['baseline']} ===")
    t0 = time.monotonic()
    ok = await loader.load_firmware()
    logger.info(f"=== load_firmware() -> {ok} in {time.monotonic()-t0:.1f}s ===")
    logger.info("RESULT: " + ("FW_N9_RDY REACHED — STUCK!" if ok else "no boot"))
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--setconfig", action="store_true")
    ap.add_argument("--epctl", action="store_true")
    ap.add_argument("--rxbuf", type=int, default=0)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args)))
