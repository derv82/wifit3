"""
Fully-faithful mt792xu_dma_init: deep IN pool + prefetch + GLO_CFG + rx_evt_ep4.

Completes the last piece of the kernel's dma_init we still skipped: rx_evt_ep4
(poll RX_DMA_BUSY=0; clear RX_DMA_EN; set RXEVT_EP4_EN; set RX_DMA_EN). That bit
(MT_WFDMA_HOST_CONFIG_USB_RXEVT_EP4_EN) reroutes MCU responses + the firmware-up
event to EP 0x84, so the drainer feeds the queue from BOTH 0x84 and 0x85.

Only the UHW epctl_rst remains unported (UHW bus = Errno 5 on WinUSB; the kernel
clears bits already clear on cold boot, so it's a documented no-op).

If this boots -> dma_init was the whole story. If not -> a 100%-faithful dma_init
still won't boot on WinUSB, pinning the blocker on the USB-level firmware handoff.

Usage: uv run python scripts/mt7921au/exp_full_dma.py [--pool 16] [--debug]
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
    MT_WFDMA0_GLO_CFG_OMIT_RX_INFO, MT_WFDMA0_GLO_CFG_OMIT_TX_INFO,
    MT_WFDMA0_GLO_CFG_OMIT_RX_INFO_PFET2, MT_WFDMA0_GLO_CFG_FW_DWLD_BYPASS_DMASHDL,
    MT_WFDMA0_GLO_CFG_TX_DMA_EN, MT_WFDMA0_GLO_CFG_RX_DMA_EN,
    MT_WFDMA0_GLO_CFG_RX_DMA_BUSY,
    MT_WFDMA_HOST_CONFIG, MT_WFDMA_HOST_CONFIG_USB_RXEVT_EP4_EN,
)

VID, PID = 0x0E8D, 0x7961
logger = logging.getLogger("exp")

_PREFETCH = [(0, 4, 0x080), (1, 4, 0x0c0), (2, 4, 0x100), (3, 4, 0x140),
             (4, 4, 0x180), (16, 4, 0x280), (17, 4, 0x2c0)]
_GLO_CFG_SET = (MT_WFDMA0_GLO_CFG_OMIT_TX_INFO | MT_WFDMA0_GLO_CFG_OMIT_RX_INFO_PFET2
                | MT_WFDMA0_GLO_CFG_FW_DWLD_BYPASS_DMASHDL
                | MT_WFDMA0_GLO_CFG_TX_DMA_EN | MT_WFDMA0_GLO_CFG_RX_DMA_EN)


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
            # Feed the queue from BOTH endpoints — RXEVT_EP4_EN moves responses to 0x84.
            self._reader_tasks.append(asyncio.create_task(self._reader(EP_IN_MCU, True)))
            self._reader_tasks.append(asyncio.create_task(self._reader(EP_IN_BULK, True)))
        logger.info(f"deep drainer: {self._pool} IN URBs/EP on 0x{EP_IN_MCU:02x}+0x{EP_IN_BULK:02x} (both feed queue)")

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


class FullDmaLoader(MT7921AUFirmwareLoader):
    def _dma_init(self):
        for idx, cnt, base in _PREFETCH:
            self._rmw(0x7c024600 + (idx << 2), 0xFF | 0xFFFF0000,
                      (cnt & 0xFF) | ((base << 16) & 0xFFFF0000))
        self._rmw(MT_UWFDMA0_GLO_CFG, MT_WFDMA0_GLO_CFG_OMIT_RX_INFO, 0)
        self._rmw(MT_UWFDMA0_GLO_CFG, 0, _GLO_CFG_SET)
        super()._dma_init()                    # DMASHDL + DUMMY_CR + WLCFG
        self._rx_evt_ep4()

    def _rx_evt_ep4(self):
        # poll RX_DMA_BUSY == 0
        busy = True
        for _ in range(100):
            v = self.transport.read_reg32_unified(MT_UWFDMA0_GLO_CFG)
            if not (v & MT_WFDMA0_GLO_CFG_RX_DMA_BUSY):
                busy = False
                break
        self._rmw(MT_UWFDMA0_GLO_CFG, MT_WFDMA0_GLO_CFG_RX_DMA_EN, 0)
        self._rmw(MT_WFDMA_HOST_CONFIG, 0, MT_WFDMA_HOST_CONFIG_USB_RXEVT_EP4_EN)
        self._rmw(MT_UWFDMA0_GLO_CFG, 0, MT_WFDMA0_GLO_CFG_RX_DMA_EN)
        logger.info(f"rx_evt_ep4 applied (RX_DMA_BUSY{'still set!' if busy else ' cleared'}; "
                    f"RXEVT_EP4_EN set -> responses now on EP 0x84)")


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
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s", datefmt="%H:%M:%S")
    loop = asyncio.get_running_loop()
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=2 * pool + 12))

    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    if dev is None:
        logger.error("MT7921AU not found.")
        return 1
    logger.info(f"Found MT7921AU bus {dev.bus} addr {dev.address} speed={getattr(dev,'speed',None)}")
    claim_vendor_iface(dev)

    transport = DeepDrainTransport(dev, pool=pool)
    loader = FullDmaLoader(transport, Path(mt_pkg.__file__).parent / "assets")

    logger.info("Posting deep IN pool BEFORE dma_init (kernel alloc_queues order)...")
    await transport.start_mcu_drainer()

    logger.info("=== Experiment: 100%-faithful dma_init (deep pool + GLO_CFG + rx_evt_ep4) ===")
    t0 = time.monotonic()
    ok = await loader.load_firmware()
    logger.info(f"=== load_firmware() returned {ok} in {time.monotonic()-t0:.1f}s ===")
    if ok:
        logger.info("RESULT: FW_N9_RDY reached. dma_init faithfulness was the fix.")
        return 0
    logger.info("RESULT: faithful dma_init still won't boot -> blocker is the WinUSB firmware handoff.")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=16)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.pool, args.debug)))
