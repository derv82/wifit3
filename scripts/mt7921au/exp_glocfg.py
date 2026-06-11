"""
Targeted fix experiment: add the missing mt792xu_wfdma_init writes.

Diff of our _dma_init vs the kernel (mt792x_usb.c mt792xu_wfdma_init) found two
chunks the port never wired in (the GLO_CFG block isn't even in the 'skipped'
list, yet its constants + a warning comment ARE in constants.py — diagnosed but
never applied):

  1. mt792xu_dma_prefetch  — 7 RMWs to MT_UWFDMA0_TX_RING_EXT_CTRL(idx)
  2. MT_UWFDMA0_GLO_CFG block:
       clear OMIT_RX_INFO
       set   OMIT_TX_INFO | OMIT_RX_INFO_PFET2 | FW_DWLD_BYPASS_DMASHDL
             | TX_DMA_EN | RX_DMA_EN

GLO_CFG carries TX_DMA_EN/RX_DMA_EN (enable the WFDMA engines) and
FW_DWLD_BYPASS_DMASHDL ("firmware-download bypass DMASHDL"). Without it the FW
bytes leave the host but don't DMA into chip RAM correctly, so FW_START boots
garbage -> EP0 dies, FW_N9_RDY never sets -> exactly our symptom.

This overrides _dma_init to add prefetch + GLO_CFG in kernel order (before
DMASHDL), then runs the STOCK loader unchanged. rx_evt_ep4 / epctl_rst are still
omitted (rx_evt_ep4 reroutes MCU responses to EP 0x84, which our 0x85 state
machine relies on — test GLO_CFG in isolation first).

Run on the USB-2 path (FW must upload first). Usage:
    uv run python scripts/mt7921au/exp_glocfg.py [--debug]
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
# ruff: noqa: F403, F405
from wifit3.chips.mt7921au.constants import (
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

# mt792xu_dma_prefetch: MT_UWFDMA0_TX_RING_EXT_CTRL(n) = 0x7c024600 + (n<<2);
# RMW clears MAX_CNT (GENMASK 7:0) | BASE_PTR (GENMASK 31:16), sets cnt | base<<16.
_PREFETCH = [(0, 4, 0x080), (1, 4, 0x0c0), (2, 4, 0x100), (3, 4, 0x140),
             (4, 4, 0x180), (16, 4, 0x280), (17, 4, 0x2c0)]
_MAX_CNT_MASK = 0x000000FF
_BASE_PTR_MASK = 0xFFFF0000
_GLO_CFG_SET = (MT_WFDMA0_GLO_CFG_OMIT_TX_INFO
                | MT_WFDMA0_GLO_CFG_OMIT_RX_INFO_PFET2
                | MT_WFDMA0_GLO_CFG_FW_DWLD_BYPASS_DMASHDL
                | MT_WFDMA0_GLO_CFG_TX_DMA_EN
                | MT_WFDMA0_GLO_CFG_RX_DMA_EN)


class GloCfgLoader(MT7921AUFirmwareLoader):
    def _dma_init(self):
        # 1. mt792xu_dma_prefetch (TX ring extended control).
        for idx, cnt, base in _PREFETCH:
            addr = 0x7c024600 + (idx << 2)
            val = (cnt & _MAX_CNT_MASK) | ((base << 16) & _BASE_PTR_MASK)
            self._rmw(addr, _MAX_CNT_MASK | _BASE_PTR_MASK, val)
        # 2. MT_UWFDMA0_GLO_CFG: clear OMIT_RX_INFO, then set the DMA-enable block.
        self._rmw(MT_UWFDMA0_GLO_CFG, MT_WFDMA0_GLO_CFG_OMIT_RX_INFO, 0)
        self._rmw(MT_UWFDMA0_GLO_CFG, 0, _GLO_CFG_SET)
        logger.info(f"GLO_CFG set -> 0x{_GLO_CFG_SET:08x} (TX/RX_DMA_EN + FW_DWLD_BYPASS_DMASHDL)")
        # 3. DMASHDL + DUMMY_CR + WLCFG (the part the port already had).
        super()._dma_init()


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


async def main(debug: bool):
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    if dev is None:
        logger.error("MT7921AU not found.")
        return 1
    logger.info(f"Found MT7921AU bus {dev.bus} addr {dev.address} speed={getattr(dev,'speed',None)}")
    claim_vendor_iface(dev)

    assets_dir = Path(mt_pkg.__file__).parent / "assets"
    transport = MT7921AUTransport(dev)
    loader = GloCfgLoader(transport, assets_dir)

    logger.info("=== Experiment: stock loader + ported GLO_CFG/prefetch block ===")
    t0 = time.monotonic()
    ok = await loader.load_firmware()
    logger.info(f"=== load_firmware() returned {ok} in {time.monotonic()-t0:.1f}s ===")
    if ok:
        logger.info("RESULT: FW_N9_RDY reached. THE GLO_CFG BLOCK WAS THE MISSING PIECE.")
        return 0
    logger.info("RESULT: still no FW_N9_RDY — next try adding rx_evt_ep4 (with EP 0x84 response handling).")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.debug)))
