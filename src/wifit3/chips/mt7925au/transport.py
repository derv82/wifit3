"""MT7925AU USB transport: vendor-control register access, MCU command plumbing,
and the FW_SCATTER bulk sender.

Register access is a port of the mt792x USB bus ops (mt792x_usb.c): the unified
bus (bRequest 0x63/0x66) for normal registers, the UHW bus (bRequest 0x01/0x02)
for the SSUSB endpoint-control CSR. Every RMW is a read immediately followed by a
write, matching the wire.
"""
import asyncio
import logging
import struct
from typing import Callable, Optional

import usb.core

from . import mcu, rx
from ..rx_reader import RxReaderThread
# ruff: noqa: F403, F405
from .constants import *

logger = logging.getLogger(__name__)

RX_READ_SIZE = 4096        # max bulk-IN buffer
RX_READ_TIMEOUT_MS = 100   # benign-timeout poll interval

# Register address encodes as wValue = addr>>16, wIndex = addr & 0xFFFF
# (___mt76u_rr/___mt76u_wr, usb.c:71-72,118-119).


class MT7925AUTransport:
    """Vendor control transfers for register access + MCU commands over the
    mt76-USB connac3 endpoints."""

    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self._rx: Optional[RxReaderThread] = None
        self._mcu_rx_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._callback = None
        self._on_fatal: Optional[Callable[[Exception], None]] = None
        self._mcu_seq = 0

    @property
    def _loop(self) -> asyncio.AbstractEventLoop:
        """The event loop, resolved lazily so construction needs no running loop."""
        return asyncio.get_event_loop()

    def subscribe(self, callback):
        self._callback = callback

    # ---- RX reader --------------------------------------------------------

    def start_rx(self):
        """Start the single RX reader thread on EP 0x84 (idempotent). It demuxes the
        connac3 rxd pkt_type (rx.classify): MCU responses feed the seq-matched
        _mcu_rx_queue, 802.11 frames go to the callback. dma_init sets RXEVT_EP4_EN,
        so MCU responses also arrive on EP 0x84 (EP 0x85 gets no completions)."""
        if self._rx is not None:
            return
        self._rx = RxReaderThread(self._loop, self._read_once, self._dispatch,
                                  name="mt7925au-rx", on_fatal=self._on_fatal)
        self._rx.start()

    async def stop_rx(self):
        if self._rx is not None:
            reader, self._rx = self._rx, None
            await reader.stop()

    def _read_once(self) -> Optional[bytes]:
        """One blocking bulk read on EP 0x84 (runs on the reader thread)."""
        try:
            data = self.dev.read(EP_IN_BULK, RX_READ_SIZE, timeout=RX_READ_TIMEOUT_MS)
        except usb.core.USBTimeoutError:
            return None
        return bytes(data) if data else None

    def drain_rx(self, max_iters: int = 8, timeout_ms: int = 20) -> int:
        """Discard any bulk-IN buffered from a prior session, direct (reader thread not yet
        started). A warm chip's EP 0x84 still holds the previous process's un-read RX/MCU
        frames; draining them keeps the first seq-matched MCU response from being shadowed
        by stale data. A timeout (empty pipe) or USB error ends the drain."""
        n = 0
        for _ in range(max_iters):
            try:
                data = self.dev.read(EP_IN_BULK, RX_READ_SIZE, timeout=timeout_ms)
            except usb.core.USBError:
                break
            if not data:
                break
            n += 1
        return n

    def _dispatch(self, data: bytes):
        """Runs on the event loop. MCU response -> the command queue; frame -> callback."""
        if rx.classify(data) == "mcu":
            self._mcu_rx_queue.put_nowait(data)
        elif self._callback is not None:
            self._callback(data)

    # ---- Bulk OUT ---------------------------------------------------------

    async def send_bulk_checked(self, data: bytes, ep: int, timeout: int = 2000) -> bool:
        """Bulk write. False on USB error OR short write (PyUSB on Windows can return a
        partial byte count without raising)."""
        try:
            written = await self._loop.run_in_executor(
                None, lambda: self.dev.write(ep, data, timeout=timeout)
            )
            if written != len(data):
                logger.error(f"Short bulk write on EP {hex(ep)}: {written}/{len(data)} bytes")
                return False
            return True
        except usb.core.USBError as e:
            logger.debug(f"Bulk write failed on EP {hex(ep)}: {e}")
            return False

    # ---- Vendor control transfers -----------------------------------------

    def _ctrl_write(self, bmreq: int, breq: int, addr: int, value: int):
        self.dev.ctrl_transfer(bmRequestType=bmreq, bRequest=breq,
                               wValue=(addr >> 16) & 0xFFFF, wIndex=addr & 0xFFFF,
                               data_or_wLength=struct.pack("<I", value))

    def _ctrl_read(self, bmreq: int, breq: int, addr: int, timeout: int = 1000) -> int:
        res = self.dev.ctrl_transfer(bmRequestType=bmreq, bRequest=breq,
                                     wValue=(addr >> 16) & 0xFFFF, wIndex=addr & 0xFFFF,
                                     data_or_wLength=4, timeout=timeout)
        return struct.unpack("<I", res)[0] if len(res) >= 4 else 0

    def read_reg32(self, addr: int) -> int:
        """Unified-bus register read (mt792xu_rr): bRequest 0x63, bmRequestType 0xDF."""
        return self._ctrl_read(MT_REQ_IN_VENDOR, MT_VEND_READ_EXT, addr)

    def write_reg32(self, addr: int, value: int):
        """Unified-bus register write (mt792xu_wr): bRequest 0x66, bmRequestType 0x5F."""
        self._ctrl_write(MT_REQ_OUT_VENDOR, MT_VEND_WRITE_EXT, addr, value)

    def rmw(self, addr: int, clear_mask: int, set_mask: int):
        """Read-modify-write (mt792xu_rmw): read then write, matching the wire order."""
        val = self.read_reg32(addr)
        self.write_reg32(addr, (val & ~clear_mask) | set_mask)

    def set_bits(self, addr: int, mask: int):
        """mt76_set: RMW OR-ing ``mask`` (read + write)."""
        self.rmw(addr, 0, mask)

    def clear_bits(self, addr: int, mask: int):
        """mt76_clear: RMW AND-ing ``~mask`` (read + write)."""
        self.rmw(addr, mask, 0)

    def read_reg32_uhw(self, addr: int) -> int:
        """UHW-bus read (mt792xu_uhw_rr): bRequest 0x01, bmRequestType 0xDE."""
        return self._ctrl_read(MT_REQ_IN_UHW_VENDOR, MT_VEND_DEV_MODE, addr)

    def write_reg32_uhw(self, addr: int, value: int):
        """UHW-bus write (mt792xu_uhw_wr): bRequest 0x02, bmRequestType 0x5E."""
        self._ctrl_write(MT_REQ_OUT_UHW_VENDOR, MT_VEND_WRITE, addr, value)

    def clear_halt(self, ep: int):
        try:
            self.dev.clear_halt(ep)
        except usb.core.USBError as e:
            logger.debug(f"Failed to clear halt on EP {hex(ep)}: {e}")

    # ---- connac3 MCU command plumbing -------------------------------------

    def _next_mcu_seq(self) -> int:
        """4-bit sequence, never 0 (mt76_mcu msg_seq)."""
        self._mcu_seq = (self._mcu_seq + 1) & 0x0F
        if self._mcu_seq == 0:
            self._mcu_seq = 1
        return self._mcu_seq

    async def send_mcu_command(self, cmd: int, payload: bytes = b"",
                               wait_resp: bool = True,
                               resp_timeout_ms: int = 2000) -> Optional[bytes]:
        """Send an MCU command on EP_OUT_MCU (0x08), framed by mcu.build_mcu_frame.
        If wait_resp, wait for the response whose mt7925_mcu_rxd seq (offset 33)
        matches this command. Returns None on no wait or timeout."""
        seq = self._next_mcu_seq()
        frame = mcu.build_mcu_frame(cmd, payload, seq)
        ok = await self.send_bulk_checked(frame, EP_OUT_MCU, timeout=2000)
        if not ok:
            if not wait_resp:
                logger.warning(f"MCU send timed out (cmd=0x{cmd:x} seq=0x{seq:02x}); continuing")
                return None
            logger.error(f"MCU send failed (cmd=0x{cmd:x} seq=0x{seq:02x})")
            return None
        if not wait_resp:
            return None

        deadline = self._loop.time() + resp_timeout_ms / 1000
        while True:
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                logger.warning(f"MCU response timeout (cmd=0x{cmd:x} seq=0x{seq:02x})")
                return None
            try:
                data = await asyncio.wait_for(self._mcu_rx_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                logger.warning(f"MCU response timeout (cmd=0x{cmd:x} seq=0x{seq:02x})")
                return None
            rseq = data[MT7925_RXD_SEQ_OFF] if len(data) > MT7925_RXD_SEQ_OFF else None
            if rseq == seq:
                return data

    async def send_fw_chunk(self, chunk: bytes, timeout_ms: int = 1000) -> bool:
        """One FW_SCATTER chunk on EP_OUT_FW (0x04): [4B SDIO hdr][chunk][pad to 4B +4].
        FW_SCATTER prepends no MCU txd (mt7925_mcu_fill_message:3476)."""
        frame = bytearray(SDIO_HDR_SIZE + len(chunk))
        struct.pack_into("<I", frame, 0, len(chunk) & 0xFFFF)
        frame[SDIO_HDR_SIZE:] = chunk
        pad = ((len(frame) + 3) & ~3) + 4 - len(frame)
        frame.extend(b"\x00" * pad)

        ok = await self.send_bulk_checked(bytes(frame), EP_OUT_FW, timeout=timeout_ms)
        if not ok:
            logger.error(f"FW_SCATTER bulk write failed (chunk_len={len(chunk)})")
            return False
        # ZLP terminator when the transfer aligned to a max-packet boundary (WinUSB/SS).
        if len(frame) % 1024 == 0:
            try:
                await self._loop.run_in_executor(
                    None, lambda: self.dev.write(EP_OUT_FW, b"", timeout=100)
                )
            except usb.core.USBError as e:
                logger.debug(f"FW_SCATTER ZLP write failed: {e}")
        return True
