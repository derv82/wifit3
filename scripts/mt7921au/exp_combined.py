"""
Combined experiment: deep IN URB pool posted BEFORE the GLO_CFG DMA-enable block.

Kernel probe order (mt7921/usb.c): power_on -> mt76u_alloc_queues (deep IN URB
pool) -> mt792xu_dma_init (whose GLO_CFG sets RX_DMA_EN). So the RX pool is
already posted when RX DMA is enabled.

Tonight's separate tests each failed for a reason this combination addresses:
  - GLO_CFG with a shallow IN pool stalled the UPLOAD at 2560B (RX DMA on, no
    RX URBs posted -> RX backs up -> TX stalls).
  - deep IN pool without GLO_CFG uploaded but never booted (DMA engines off).

This posts a deep IN pool FIRST, then runs the loader with the GLO_CFG/prefetch
block added to _dma_init. Run on the USB-2 path.

Usage: uv run python scripts/mt7921au/exp_combined.py [--pool 16] [--debug]
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
from wifit3.chips.mt7921au.constants import (
    EP_IN_BULK, EP_IN_MCU,
    MT_UWFDMA0_GLO_CFG,
    MT_WFDMA0_GLO_CFG_OMIT_RX_INFO,
    MT_WFDMA0_GLO_CFG_OMIT_TX_INFO,
    MT_WFDMA0_GLO_CFG_OMIT_RX_INFO_PFET2,
    MT_WFDMA0_GLO_CFG_FW_DWLD_BYPASS_DMASHDL,
    MT_WFDMA0_GLO_CFG_TX_DMA_EN,
    MT_WFDMA0_GLO_CFG_RX_DMA_EN,
)

VID, PID = 0x0E8D, 0x7961
logger = logging.getLogger("exp")

_PREFETCH = [(0, 4, 0x080), (1, 4, 0x0c0), (2, 4, 0x100), (3, 4, 0x140),
             (4, 4, 0x180), (16, 4, 0x280), (17, 4, 0x2c0)]
_GLO_CFG_SET = (MT_WFDMA0_GLO_CFG_OMIT_TX_INFO
                | MT_WFDMA0_GLO_CFG_OMIT_RX_INFO_PFET2
                | MT_WFDMA0_GLO_CFG_FW_DWLD_BYPASS_DMASHDL
                | MT_WFDMA0_GLO_CFG_TX_DMA_EN
                | MT_WFDMA0_GLO_CFG_RX_DMA_EN)


class DeepDrainTransport(MT7921AUTransport):
    def __init__(self, dev, pool=16):
        super().__init__(dev)
        self._pool = pool
        self._reader_tasks = []

    async def start_mcu_drainer(self):
        if self._mcu_drainer_running:
            return
        self._mcu_drainer_running = True
        for _ in range(self._pool):
            self._reader_tasks.append(asyncio.create_task(self._reader(EP_IN_MCU, True)))
            self._reader_tasks.append(asyncio.create_task(self._reader(EP_IN_BULK, False)))
        logger.info(f"deep drainer: {self._pool} IN URBs posted on 0x{EP_IN_MCU:02x}/0x{EP_IN_BULK:02x}")

    async def _reader(self, ep, to_queue):
        while self._mcu_drainer_running:
            try:
                data = await self._loop.run_in_executor(
                    None, lambda: self.dev.read(ep, 2048, timeout=100))
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


class GloCfgLoader(MT7921AUFirmwareLoader):
    def _dma_init(self):
        for idx, cnt, base in _PREFETCH:
            addr = 0x7c024600 + (idx << 2)
            val = (cnt & 0xFF) | ((base << 16) & 0xFFFF0000)
            self._rmw(addr, 0xFF | 0xFFFF0000, val)
        self._rmw(MT_UWFDMA0_GLO_CFG, MT_WFDMA0_GLO_CFG_OMIT_RX_INFO, 0)
        self._rmw(MT_UWFDMA0_GLO_CFG, 0, _GLO_CFG_SET)
        logger.info(f"GLO_CFG set -> 0x{_GLO_CFG_SET:08x}")
        super()._dma_init()


def claim_vendor_iface(dev):
    for intf in dev.get_active_configuration():
        if intf.bInterfaceClass == 0xFF:
            try:
                usb.util.claim_interface(dev, intf.bInterfaceNumber)
                logger.info(f"Claimed vendor-specific interface {intf.bInterfaceNumber}")
            except Exception as e:
                logger.debug(f"claim: {e}")
            return intf.bInterfaceNumber


async def main(pool, debug):
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S")
    loop = asyncio.get_running_loop()
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=2 * pool + 12))

    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    if dev is None:
        logger.error("MT7921AU not found.")
        return 1
    logger.info(f"Found MT7921AU bus {dev.bus} addr {dev.address} speed={getattr(dev,'speed',None)}")
    claim_vendor_iface(dev)

    assets_dir = Path(mt_pkg.__file__).parent / "assets"
    transport = DeepDrainTransport(dev, pool=pool)
    loader = GloCfgLoader(transport, assets_dir)

    # KERNEL ORDER: post the deep IN pool BEFORE _dma_init enables RX_DMA.
    logger.info("Posting deep IN pool BEFORE firmware/dma_init (kernel alloc_queues order)...")
    await transport.start_mcu_drainer()

    logger.info("=== Experiment: deep IN pool + GLO_CFG/prefetch ===")
    t0 = time.monotonic()
    ok = await loader.load_firmware()
    logger.info(f"=== load_firmware() returned {ok} in {time.monotonic()-t0:.1f}s ===")
    if ok:
        logger.info("RESULT: FW_N9_RDY reached. DEEP-POOL + GLO_CFG IS THE FIX.")
        return 0
    logger.info("RESULT: still no boot — log shows whether upload stalled (RX backpressure) "
                "or upload completed but FW_START didn't boot.")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=16)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.pool, args.debug)))
