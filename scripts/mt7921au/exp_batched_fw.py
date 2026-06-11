"""
Cheap de-risk experiment for the MT7921AU USB-3 FW_SCATTER 4-packet stall.

Hypothesis under test: the stall comes from our one-bulk-write-at-a-time loop
leaving gaps (and intermediate short packets) between FW_SCATTER chunks, which
USB-3 flow control chokes on. The kernel avoids it with a deep async URB pool
that keeps the pipe continuously full.

This experiment is the poor-man's version: build the FW_SCATTER stream for a
whole region and hand it to WinUSB as ONE (or a few) large bulk transfer(s),
so the host streams 1024-byte packets back-to-back with no inter-chunk gap.

It reuses the REAL firmware loader's setup (power-on, DMA init, patch session,
TARGET_ADDR_LEN_REQ, FW_START_REQ, FW_N9_RDY poll) and only overrides how the
FW_SCATTER bytes are pushed onto EP 0x04. So everything up to and through the
wall is byte-faithful; only the USB-transfer batching changes.

Usage:
    uv run python scripts/mt7921au/exp_batched_fw.py                # one write per region
    uv run python scripts/mt7921au/exp_batched_fw.py --chunks 8     # 8 chunks per write
    uv run python scripts/mt7921au/exp_batched_fw.py --debug
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

from wifit3.chips.mt7921au.driver import MT7921AUDriver  # noqa: F401 (ensures pkg import path)
from wifit3.chips.mt7921au.firmware import MT7921AUFirmwareLoader
from wifit3.chips.mt7921au.transport import MT7921AUTransport
# ruff: noqa: F403, F405
from wifit3.chips.mt7921au.constants import *

VID, PID = 0x0E8D, 0x7961
logger = logging.getLogger("exp")


def _build_scatter_frame(chunk: bytes) -> bytes:
    """Byte-identical to transport.send_fw_chunk's framing: [4B SDIO hdr][chunk][pad]."""
    frame = bytearray(SDIO_HDR_SIZE + len(chunk))
    struct.pack_into("<I", frame, 0, len(chunk) & 0xFFFF)
    frame[SDIO_HDR_SIZE:] = chunk
    pad = ((len(frame) + 3) & ~3) + 4 - len(frame)
    frame.extend(b"\x00" * pad)
    return bytes(frame)


class BatchedFirmwareLoader(MT7921AUFirmwareLoader):
    """Overrides only the FW_SCATTER transmission to batch chunks per USB write."""

    def __init__(self, *a, chunks_per_write: int = 0, **kw):
        super().__init__(*a, **kw)
        # 0 == "all chunks of the region in a single write".
        self.chunks_per_write = chunks_per_write

    async def _send_fw_chunks(self, blob: bytes, offset: int, length: int, label: str) -> bool:
        # Build the full per-chunk frame list (same chunking the stock loader uses).
        frames = []
        sent = 0
        while sent < length:
            cur = min(MAX_FW_CHUNK, length - sent)
            frames.append(_build_scatter_frame(blob[offset + sent: offset + sent + cur]))
            sent += cur

        per_write = self.chunks_per_write if self.chunks_per_write > 0 else len(frames)
        loop = asyncio.get_event_loop()
        dev = self.transport.dev

        widx = 0
        batch_no = 0
        while widx < len(frames):
            batch = b"".join(frames[widx: widx + per_write])
            t0 = time.monotonic()
            try:
                written = await loop.run_in_executor(
                    None, lambda b=batch: dev.write(EP_OUT_FW, b, timeout=15000)
                )
            except usb.core.USBError as e:
                dt = (time.monotonic() - t0) * 1000
                logger.error(f"{label} batch {batch_no} ({len(batch)}B): USBError after "
                             f"{dt:.0f}ms: {e}")
                return False
            dt = (time.monotonic() - t0) * 1000
            if written != len(batch):
                logger.error(f"{label} batch {batch_no}: SHORT WRITE {written}/{len(batch)}B "
                             f"after {dt:.0f}ms  <-- still walled")
                return False
            logger.info(f"{label} batch {batch_no}: {written}B "
                        f"({widx}..{widx+per_write} of {len(frames)} chunks) in {dt:.0f}ms  OK")

            # ZLP terminator if the batch landed exactly on a 1024 boundary.
            if len(batch) % 1024 == 0:
                try:
                    await loop.run_in_executor(None, lambda: dev.write(EP_OUT_FW, b"", timeout=200))
                except usb.core.USBError:
                    pass

            widx += per_write
            batch_no += 1
            await asyncio.sleep(0)

        logger.info(f"FW_SCATTER {label}: {length} bytes via {batch_no} batched write(s)  PASSED")
        return True


def claim_vendor_iface(dev):
    """The PAU0F puts the vendor-specific WiFi function on interface 0 (the AXML used 3).
    Detect it by class 0xFF and claim it explicitly so the stock loader's hardcoded
    'claim interface 3' (which fails harmlessly here) isn't load-bearing."""
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


async def main(chunks_per_write: int, debug: bool):
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    backend = libusb_package.get_libusb1_backend()
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    if dev is None:
        logger.error("MT7921AU not found — plug in + Zadig/WinUSB, then retry.")
        return 1
    logger.info(f"Found MT7921AU bus {dev.bus} addr {dev.address} speed={getattr(dev,'speed',None)}")
    claim_vendor_iface(dev)

    import wifit3.chips.mt7921au as mt_pkg
    assets_dir = Path(mt_pkg.__file__).parent / "assets"

    transport = MT7921AUTransport(dev)
    loader = BatchedFirmwareLoader(transport, assets_dir, chunks_per_write=chunks_per_write)

    mode = "ONE write/region" if chunks_per_write == 0 else f"{chunks_per_write} chunks/write"
    logger.info(f"=== Experiment: batched FW_SCATTER ({mode}) ===")
    t0 = time.monotonic()
    ok = await loader.load_firmware()
    dt = time.monotonic() - t0
    logger.info(f"=== load_firmware() returned {ok} in {dt:.1f}s ===")
    if ok:
        logger.info("RESULT: firmware reached FW_N9_RDY. THE WALL IS BROKEN.")
        return 0
    logger.info("RESULT: did not reach FW_N9_RDY — see the last logged step for how far it got.")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", type=int, default=0,
                    help="chunks per USB write (0 = whole region in one write)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.chunks, args.debug)))
