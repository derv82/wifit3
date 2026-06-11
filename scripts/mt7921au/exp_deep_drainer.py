"""
Cheap de-risk #2 for the MT7921AU FW_SCATTER 4-packet stall.

De-risk #1 (exp_batched_fw.py) proved the stall is NOT about OUT batching: the
device hard-stops at 4096 bytes whether we send 23 small writes or one 90 KB
write. That implicates the IN side — the kernel keeps a DEEP pool of IN URBs
posted on EP 0x84/0x85 (mt76u_alloc_queues ~128 each); our stock drainer posts
just one read at a time.

This experiment runs the STOCK firmware loader UNCHANGED, but swaps the
transport's drainer for one that posts N concurrent IN reads per endpoint (N
real outstanding URBs). If a deep IN pool is what the device's USB-3 flow
control needs, the stock per-chunk OUT writes will get PAST chunk 0.

Faithfulness note: the IN reads are blocking libusb calls run on a wide thread
pool. libusb submits each transfer up front (the URB is posted to the device
immediately); only completion *handling* is serialized — so the on-wire URB
depth the device sees is real, which is exactly what we're testing.

Usage:
    uv run python scripts/mt7921au/exp_deep_drainer.py            # pool=16
    uv run python scripts/mt7921au/exp_deep_drainer.py --pool 32
    uv run python scripts/mt7921au/exp_deep_drainer.py --debug
"""
import asyncio
import argparse
import concurrent.futures
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
# ruff: noqa: F403, F405
from wifit3.chips.mt7921au.constants import EP_IN_BULK, EP_IN_MCU

VID, PID = 0x0E8D, 0x7961
logger = logging.getLogger("exp")


class DeepDrainTransport(MT7921AUTransport):
    """Same transport, but the drainer posts `pool` concurrent IN reads per
    endpoint instead of one — to test whether on-wire IN-URB depth is what
    unblocks the OUT pipe during firmware download."""

    def __init__(self, dev, pool: int = 16):
        super().__init__(dev)
        self._pool = pool
        self._reader_tasks: list[asyncio.Task] = []

    async def start_mcu_drainer(self):
        if self._mcu_drainer_running:
            return
        self._mcu_drainer_running = True
        for _ in range(self._pool):
            self._reader_tasks.append(asyncio.create_task(self._reader(EP_IN_MCU, to_queue=True)))
            self._reader_tasks.append(asyncio.create_task(self._reader(EP_IN_BULK, to_queue=False)))
        logger.info(f"deep drainer: {self._pool} IN URBs posted on EP 0x{EP_IN_MCU:02x} "
                    f"and EP 0x{EP_IN_BULK:02x}")

    async def _reader(self, ep: int, to_queue: bool):
        while self._mcu_drainer_running:
            try:
                data = await self._loop.run_in_executor(
                    None, lambda: self.dev.read(ep, 2048, timeout=100)
                )
                if data and to_queue:
                    await self._mcu_rx_queue.put(bytes(data))
            except usb.core.USBTimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.005)

    async def stop_mcu_drainer(self):
        self._mcu_drainer_running = False
        for t in self._reader_tasks:
            t.cancel()
        for t in self._reader_tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._reader_tasks = []
        while not self._mcu_rx_queue.empty():
            try:
                self._mcu_rx_queue.get_nowait()
            except asyncio.QueueEmpty:
                break


def claim_vendor_iface(dev):
    cfg = dev.get_active_configuration()
    for intf in cfg:
        if intf.bInterfaceClass == 0xFF:
            try:
                usb.util.claim_interface(dev, intf.bInterfaceNumber)
                logger.info(f"Claimed vendor-specific interface {intf.bInterfaceNumber}")
            except Exception as e:
                logger.debug(f"claim iface {intf.bInterfaceNumber}: {e}")
            return intf.bInterfaceNumber
    return None


async def main(pool: int, debug: bool):
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Wide executor so `pool` IN reads + OUT writes + control reads don't starve.
    loop = asyncio.get_running_loop()
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=2 * pool + 12))

    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    if dev is None:
        logger.error("MT7921AU not found — plug in + Zadig/WinUSB, then retry.")
        return 1
    logger.info(f"Found MT7921AU bus {dev.bus} addr {dev.address} speed={getattr(dev,'speed',None)}")
    claim_vendor_iface(dev)

    assets_dir = Path(mt_pkg.__file__).parent / "assets"
    transport = DeepDrainTransport(dev, pool=pool)
    loader = MT7921AUFirmwareLoader(transport, assets_dir)

    logger.info(f"=== Experiment: stock loader + deep IN drainer (pool={pool}) ===")
    t0 = time.monotonic()
    ok = await loader.load_firmware()
    dt = time.monotonic() - t0
    logger.info(f"=== load_firmware() returned {ok} in {dt:.1f}s ===")
    if ok:
        logger.info("RESULT: FW_N9_RDY reached. THE WALL IS BROKEN — deep IN pool is the fix.")
        return 0
    logger.info("RESULT: did not reach FW_N9_RDY — see the last logged step for how far it got "
                "(getting PAST 'patch sec 0 chunk 0' is already a big signal).")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=16, help="IN URBs posted per endpoint")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.pool, args.debug)))
