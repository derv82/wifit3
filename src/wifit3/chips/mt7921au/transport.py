import usb.core
import logging
import asyncio
import struct
from typing import Callable, Optional

from . import mcu, rx
from ..rx_reader import RxReaderThread
# Star-imports the chip's register/PHY constants; the names resolve at runtime
# but ruff can't see them statically, so suppress the import-* lints file-wide.
# ruff: noqa: F403, F405
from .constants import *

logger = logging.getLogger(__name__)

RX_READ_SIZE = 4096        # max bulk-IN buffer (capture's largest single read = 1620 B)
RX_READ_TIMEOUT_MS = 100   # benign-timeout poll interval

# All register access encodes the full 32-bit address as:
#   wValue = (addr >> 16) & 0xFFFF  (upper 16 bits)
#   wIndex = addr & 0xFFFF          (lower 16 bits)
# Confirmed from pcap: frame 112 reads wValue=0x7001, wIndex=0x0200 → 0x70010200 (MT_HW_CHIPID).

class MT7921AUTransport:
    """
    Transport layer for MT7921AU.
    Handles Vendor Control Transfers for register access and MCU commands.
    """
    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self._rx: Optional[RxReaderThread] = None
        self._mcu_rx_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._callback = None
        # Terminal RX-reader failure sink (unplug). The driver sets this via its
        # register_disconnect_callback; start_rx hands it to the RxReaderThread.
        self._on_fatal: Optional[Callable[[Exception], None]] = None
        # MCU sequence counter: Linux uses 4-bit wrap, skips 0
        self._mcu_seq = 0

    @property
    def _loop(self) -> asyncio.AbstractEventLoop:
        """The event loop, resolved lazily so construction needs no running loop."""
        return asyncio.get_event_loop()

    def subscribe(self, callback):
        self._callback = callback

    def start_rx(self):
        """Start the single RX reader thread on EP 0x84 (idempotent).

        One thread keeps a read posted at all times (RxReaderThread), demuxing
        the connac2 rxd packet type (mt7921_queue_rx_skb): MCU responses feed the
        seq-matched _mcu_rx_queue — so the FW-load handshake, post-boot init and
        set_channel all get their acks — and 802.11 frames go to the callback.

        EP 0x85 is not read: dma_init sets RXEVT_EP4_EN, routing every MCU
        response to EP 0x84 (the capture shows EP 0x85 gets zero completions)."""
        if self._rx is not None:
            return
        self._rx = RxReaderThread(self._loop, self._read_once, self._dispatch,
                                  name="mt7921au-rx", on_fatal=self._on_fatal)
        self._rx.start()

    async def stop_rx(self):
        if self._rx is not None:
            reader, self._rx = self._rx, None
            await reader.stop()

    def _read_once(self) -> Optional[bytes]:
        """One blocking bulk read on EP 0x84 (runs on the reader thread). Returns
        the buffer, None on a benign timeout, or raises on a real USB error — the
        reader counts consecutive errors and gives up, so a wedged pipe is not
        hammered indefinitely."""
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
        """Runs on the event loop (RxReaderThread hands off via call_soon_threadsafe).
        MCU response -> the command-response queue; 802.11 frame -> the callback."""
        if rx.classify(data) == "mcu":
            self._mcu_rx_queue.put_nowait(data)
        elif self._callback is not None:
            self._callback(data)

    async def send_bulk(self, data: bytes, ep: int, timeout: int = 2000):
        """Sends a raw packet to the specified Bulk OUT endpoint."""
        try:
            await self._loop.run_in_executor(
                None, lambda: self.dev.write(ep, data, timeout=timeout)
            )
        except usb.core.USBError as e:
            logger.error(f"Failed to send bulk data on EP {hex(ep)}: {e}")

    async def send_bulk_checked(self, data: bytes, ep: int, timeout: int = 2000) -> bool:
        """
        Bulk write. Returns False on USB error OR short write (bytes_written < len(data)).
        PyUSB on Windows can return a partial byte count without raising on timeout,
        so we have to check it explicitly.
        """
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

    def send_vendor_request(self, bmRequestType: int, bRequest: int, wValue: int, wIndex: int, data: bytes = b"", timeout: int = 1000):
        """Sends a vendor-specific control transfer."""
        try:
            self.dev.ctrl_transfer(
                bmRequestType=bmRequestType,
                bRequest=bRequest,
                wValue=wValue,
                wIndex=wIndex,
                data_or_wLength=data,
                timeout=timeout
            )
        except usb.core.USBError as e:
            logger.debug(f"Vendor request failed ({hex(bmRequestType)} {hex(bRequest)}): {e}")

    def read_vendor_request(self, bmRequestType: int, bRequest: int, wValue: int, wIndex: int, wLength: int, timeout: int = 1000) -> bytes:
        """Reads data via vendor-specific control transfer."""
        try:
            return bytes(self.dev.ctrl_transfer(
                bmRequestType=bmRequestType,
                bRequest=bRequest,
                wValue=wValue,
                wIndex=wIndex,
                data_or_wLength=wLength,
                timeout=timeout
            ))
        except usb.core.USBError as e:
            logger.debug(f"Vendor read failed ({hex(bmRequestType)} {hex(bRequest)}): {e}")
            return b""

    def write_reg32(self, addr: int, value: int):
        """Standard bus register write (bmRequestType=0x40, bRequest=0x66)."""
        wValue = (addr >> 16) & 0xFFFF
        wIndex = addr & 0xFFFF
        self.dev.ctrl_transfer(
            bmRequestType=0x40, bRequest=MT_VEND_WRITE_REG_REQ,
            wValue=wValue, wIndex=wIndex,
            data_or_wLength=struct.pack("<I", value)
        )

    def read_reg32(self, addr: int) -> int:
        """Standard bus register read (bmRequestType=0xC0, bRequest=0x63)."""
        wValue = (addr >> 16) & 0xFFFF
        wIndex = addr & 0xFFFF
        res = self.dev.ctrl_transfer(
            bmRequestType=0xC0, bRequest=MT_VEND_READ_REG_REQ,
            wValue=wValue, wIndex=wIndex,
            data_or_wLength=4
        )
        if len(res) < 4:
            return 0
        return struct.unpack("<I", res)[0]

    def read_boot_status(self, length: int = 64) -> bytes:
        """Queries the 64-byte boot status (wValue=0x0030 as seen in pcap)."""
        return self.read_vendor_request(MT_VEND_REQ_IN, MT_VEND_REQ_BOOT_STATUS, 0x0030, 0, length)

    def write_reg32_unified(self, addr: int, value: int):
        """Unified Bus register write (bmRequestType=0x5F, bRequest=0x66)."""
        wValue = (addr >> 16) & 0xFFFF
        wIndex = addr & 0xFFFF
        self.send_vendor_request(MT_VEND_WRITE_RECIPIENT, MT_VEND_WRITE_REG_REQ, wValue, wIndex, struct.pack("<I", value))

    def read_reg32_unified(self, addr: int) -> int:
        """Unified Bus register read (bmRequestType=0xDF, bRequest=0x63)."""
        wValue = (addr >> 16) & 0xFFFF
        wIndex = addr & 0xFFFF
        res = self.read_vendor_request(MT_VEND_READ_RECIPIENT, MT_VEND_READ_REG_REQ, wValue, wIndex, 4)
        if len(res) < 4:
            return 0
        return struct.unpack("<I", res)[0]

    def write_reg32_uhw(self, addr: int, value: int):
        """UHW (USB Host Wrapper) bus write — bmRequestType=0x5E, bRequest=0x02."""
        wValue = (addr >> 16) & 0xFFFF
        wIndex = addr & 0xFFFF
        self.send_vendor_request(MT_UHW_WRITE_RECIPIENT, MT_VEND_WRITE, wValue, wIndex, struct.pack("<I", value))

    def read_reg32_uhw(self, addr: int) -> int:
        """UHW (USB Host Wrapper) bus read — bmRequestType=0xDE, bRequest=0x01."""
        wValue = (addr >> 16) & 0xFFFF
        wIndex = addr & 0xFFFF
        res = self.read_vendor_request(MT_UHW_READ_RECIPIENT, MT_VEND_DEV_MODE, wValue, wIndex, 4)
        if len(res) < 4:
            return 0
        return struct.unpack("<I", res)[0]

    def clear_halt(self, ep: int):
        """Clears the stall/halt condition on an endpoint."""
        try:
            self.dev.clear_halt(ep)
        except usb.core.USBError as e:
            logger.debug(f"Failed to clear halt on EP {hex(ep)}: {e}")

    # ------------------------------------------------------------------
    # connac2 MCU command plumbing (verified against capture-3.pcap)
    # ------------------------------------------------------------------

    def _next_mcu_seq(self) -> int:
        """4-bit sequence, never 0 (matches Linux mt76_mcu.msg_seq behavior)."""
        self._mcu_seq = (self._mcu_seq + 1) & 0x0F
        if self._mcu_seq == 0:
            self._mcu_seq = 1
        return self._mcu_seq

    def _build_mcu_frame(self, cmd: int, payload: bytes) -> tuple[bytes, int]:
        """Stamp the next seq and frame an encoded ``cmd`` + payload via the
        mcu.build_mcu_frame (mt76_connac2_mcu_fill_message). Returns
        (frame_bytes, seq) so the caller can match the device's seq-echoed reply."""
        seq = self._next_mcu_seq()
        return mcu.build_mcu_frame(cmd, payload, seq), seq

    async def send_mcu_command(self, cmd: int, payload: bytes = b"",
                               wait_resp: bool = True,
                               resp_timeout_ms: int = 2000) -> Optional[bytes]:
        """
        Sends an MCU command on EP_OUT_MCU (0x08). ``cmd`` is an encoded command
        int (mcu.MCU_CMD / MCU_EXT_CMD / MCU_UNI_CMD / MCU_CE_CMD); the txd shape,
        ext_cid and set_query are derived from it. If wait_resp, waits for the
        seq-matched response from the response queue (fed by the RX reader thread).
        Returns None if wait_resp is False or the wait times out.
        """
        frame, seq = self._build_mcu_frame(cmd, payload)
        ok = await self.send_bulk_checked(frame, EP_OUT_MCU, timeout=2000)
        if not ok:
            # On USB 2.0 the device sometimes takes >2s to ACK the URB
            # completion (probably busy processing the previous region's
            # FW_SCATTER data). The bytes appear to have been transferred even
            # when libusb reports timeout — subsequent FW_SCATTER chunks land
            # at the correct addr. So for fire-and-forget commands, treat
            # timeout as "probably succeeded" and let the FW_N9_RDY poll be
            # the actual success signal.
            if not wait_resp:
                logger.warning(f"MCU send_bulk timed out (cmd=0x{cmd:x} seq=0x{seq:02x}); continuing (fire-and-forget)")
                return None
            logger.error(f"MCU send_bulk failed (cmd=0x{cmd:x} seq=0x{seq:02x})")
            return None

        if not wait_resp:
            return None

        # Wait for the response whose connac2 rxd seq (offset 29) matches THIS
        # command. The device acks each MCU command on EP 0x84, but acks can be
        # slow (the cold-boot capture shows PATCH_SEM_RELEASE acking ~250 ms later),
        # so an earlier command's ack can still be sitting in the queue. Taking the
        # FIFO head would mis-pair them — match by seq and discard stale acks for
        # already-completed commands.
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
            rseq = data[29] if len(data) > 29 else None
            if rseq == seq:
                return data

    async def send_fw_chunk(self, chunk: bytes, timeout_ms: int = 1000) -> bool:
        """
        Sends a single FW_SCATTER chunk on EP_OUT_FW (0x04):
          [ 4B SDIO HDR ][ chunk ][ pad to 4-byte align + 4 ]

        Per Linux mt76_connac2_mcu_fill_message: FW_SCATTER short-circuits the
        TXD build, so only the 4-byte SDIO header is prepended. Verified
        against capture-3 frames 14190+ (4104-byte chunks).

        Workaround for WinUSB / SuperSpeed: if the total transfer length is an
        exact multiple of wMaxPacketSize (1024 for USB 3.0), send a zero-length
        write afterward to commit the FIFO. Otherwise the MT7921 hardware
        controller holds the data open waiting for a transfer-terminator.
        """
        frame = bytearray(SDIO_HDR_SIZE + len(chunk))
        # SDIO HDR: tx_bytes = chunk size, pkt_type = 0
        struct.pack_into("<I", frame, 0, len(chunk) & 0xFFFF)
        frame[SDIO_HDR_SIZE:] = chunk
        pad = ((len(frame) + 3) & ~3) + 4 - len(frame)
        frame.extend(b"\x00" * pad)

        ok = await self.send_bulk_checked(bytes(frame), EP_OUT_FW, timeout=timeout_ms)
        if not ok:
            logger.error(f"FW_SCATTER bulk write failed (chunk_len={len(chunk)})")
            return False

        # ZLP terminator if the transfer aligned to a max-packet-size boundary.
        if len(frame) % 1024 == 0:
            try:
                await self._loop.run_in_executor(
                    None, lambda: self.dev.write(EP_OUT_FW, b"", timeout=100)
                )
            except usb.core.USBError as e:
                logger.debug(f"FW_SCATTER ZLP write failed: {e}")
        return True
