import usb.core
import logging
import asyncio
import struct
from typing import Optional

# Star-imports the chip's register/PHY constants; the names resolve at runtime
# but ruff can't see them statically, so suppress the import-* lints file-wide.
# ruff: noqa: F403, F405
from .constants import *

logger = logging.getLogger(__name__)

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
        self._loop = asyncio.get_event_loop()
        self._rx_task: Optional[asyncio.Task] = None
        self._mcu_rx_task: Optional[asyncio.Task] = None
        self._pkt_rx_task: Optional[asyncio.Task] = None
        self._mcu_rx_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._mcu_drainer_running = False
        self._callback = None
        self._is_running = False
        # MCU sequence counter — Linux uses 4-bit wrap, skips 0
        self._mcu_seq = 0

    def subscribe(self, callback):
        self._callback = callback

    async def start(self):
        if self._is_running:
            return
        self._is_running = True
        self._rx_task = asyncio.create_task(self._poll_loop())
        logger.info("MT7921AU Transport started.")

    async def stop(self):
        self._is_running = False
        if self._rx_task:
            self._rx_task.cancel()
            try:
                await self._rx_task
            except asyncio.CancelledError:
                pass
        logger.info("MT7921AU Transport stopped.")

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

    async def _poll_loop(self):
        while self._is_running:
            try:
                data = await self._loop.run_in_executor(
                    None, lambda: self.dev.read(EP_IN_BULK, 4096, timeout=100)
                )
                if data and self._callback:
                    self._callback(bytes(data))
            except usb.core.USBTimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Transport read error: {e}")
                await asyncio.sleep(0.1)

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

    def _build_mcu_frame(self, cid: int, payload: bytes,
                         set_query: int = MCU_Q_NA, ext_cid: int = 0) -> tuple[bytes, int]:
        """
        Builds the complete on-wire bytes for an MCU command:
          [ 4B SDIO HDR ][ 64B mt76_connac2_mcu_txd ][ payload ][ pad ]

        Returns (frame_bytes, seq) so the caller can match a response later.
        Layout decoded from capture-3 frame 14182 (PATCH_SEM_CONTROL).
        """
        seq = self._next_mcu_seq()
        payload_len = len(payload)
        skb_len = MCU_TXD_SIZE + payload_len  # bytes that go to MT_TXD0_TX_BYTES + SDIO HDR

        frame = bytearray(SDIO_HDR_SIZE + MCU_TXD_SIZE + payload_len)

        # 4-byte SDIO HDR: tx_bytes (15:0) | pkt_type (17:16)=0
        struct.pack_into("<I", frame, 0, skb_len & 0xFFFF)

        # 64-byte mt76_connac2_mcu_txd starts at offset 4
        txd_off = SDIO_HDR_SIZE

        # txd[0]: TXD0_BASE | skb->len (lower 16 bits)
        struct.pack_into("<I", frame, txd_off + 0, TXD0_BASE | (skb_len & 0xFFFF))
        # txd[1]: LONG_FORMAT | HDR_FORMAT_CMD
        struct.pack_into("<I", frame, txd_off + 4, TXD1_CMD)
        # txd[2..7] left as zeros

        # mcu_txd metadata (offset 32 within the TXD = offset 36 in frame)
        meta = txd_off + 32
        struct.pack_into("<H", frame, meta + 0, 32 + payload_len)   # len = skb_len - sizeof(txd[8])
        struct.pack_into("<H", frame, meta + 2, MCU_PQ_ID)          # pq_id = 0x8000
        frame[meta + 4] = cid & 0xFF                                 # cid
        frame[meta + 5] = MCU_PKT_ID                                 # pkt_type
        frame[meta + 6] = set_query & 0xFF                           # set_query
        frame[meta + 7] = seq & 0xFF                                 # seq
        frame[meta + 8] = 0                                          # uc_d2b0_rev
        frame[meta + 9] = ext_cid & 0xFF                             # ext_cid
        frame[meta + 10] = MCU_S2D_H2N                               # s2d_index
        frame[meta + 11] = 1 if ext_cid else 0                       # ext_cid_ack
        # bytes meta+12..meta+31 (rsv[5]) stay zero

        # Payload
        frame[SDIO_HDR_SIZE + MCU_TXD_SIZE:] = payload

        # Round up to 4-byte alignment, then add 4 trailing pad bytes
        # (matches mt7921u_mcu_send_message: pad = round_up(len,4)+4 - len)
        pad = ((len(frame) + 3) & ~3) + 4 - len(frame)
        frame.extend(b"\x00" * pad)

        return bytes(frame), seq

    async def start_mcu_drainer(self):
        """
        Background readers on EP 0x85 (MCU responses) and EP 0x84 (RX packets).
        Linux keeps 128 URBs queued on EACH of these. The device may rely on
        URB pressure to maintain USB state machine — without it, bulk OUT
        stalls after the first few packets.
        """
        if self._mcu_drainer_running:
            return
        self._mcu_drainer_running = True
        self._mcu_rx_task = asyncio.create_task(self._mcu_drain_loop())
        self._pkt_rx_task = asyncio.create_task(self._pkt_drain_loop())

    async def stop_mcu_drainer(self):
        self._mcu_drainer_running = False
        for task in (self._mcu_rx_task, getattr(self, "_pkt_rx_task", None)):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._mcu_rx_task = None
        self._pkt_rx_task = None
        # Drain any leftover queued responses.
        while not self._mcu_rx_queue.empty():
            try:
                self._mcu_rx_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _pkt_drain_loop(self):
        """Continuously read EP 0x84 (PKT RX) so URBs are always pending there."""
        while self._mcu_drainer_running:
            try:
                data = await self._loop.run_in_executor(
                    None, lambda: self.dev.read(EP_IN_BULK, 4096, timeout=100)
                )
                if data:
                    logger.debug(f"EP 0x84 drain RX len={len(data)} bytes[:32]={bytes(data[:32]).hex()}")
            except usb.core.USBTimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"EP 0x84 drainer error: {e}")
                await asyncio.sleep(0.01)

    async def _mcu_drain_loop(self):
        while self._mcu_drainer_running:
            try:
                data = await self._loop.run_in_executor(
                    None, lambda: self.dev.read(EP_IN_MCU, 2048, timeout=100)
                )
                if data:
                    payload = bytes(data)
                    logger.debug(f"MCU drain RX len={len(payload)} bytes[:32]={payload[:32].hex()}")
                    await self._mcu_rx_queue.put(payload)
            except usb.core.USBTimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"MCU drainer error: {e}")
                await asyncio.sleep(0.01)

    async def send_mcu_command(self, cid: int, payload: bytes,
                               set_query: int = MCU_Q_NA, ext_cid: int = 0,
                               wait_resp: bool = True,
                               resp_timeout_ms: int = 2000) -> Optional[bytes]:
        """
        Sends an MCU command on EP_OUT_MCU (0x08). If wait_resp, waits for a
        response from the MCU drain queue (fed by start_mcu_drainer).
        Returns None if wait_resp is False or wait times out.
        """
        frame, seq = self._build_mcu_frame(cid, payload, set_query=set_query, ext_cid=ext_cid)
        logger.debug(f"MCU TX cid=0x{cid:02x} seq=0x{seq:02x} len={len(frame)} payload={payload.hex()}")

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
                logger.warning(f"MCU send_bulk timed out (cid=0x{cid:02x} seq=0x{seq:02x}); continuing (fire-and-forget)")
                return None
            logger.error(f"MCU send_bulk failed (cid=0x{cid:02x} seq=0x{seq:02x})")
            return None

        if not wait_resp:
            return None

        try:
            data = await asyncio.wait_for(self._mcu_rx_queue.get(),
                                          timeout=resp_timeout_ms / 1000)
            logger.debug(f"MCU RX cid=0x{cid:02x} seq=0x{seq:02x} len={len(data)} bytes[:64]={data[:64].hex()}")
            return data
        except asyncio.TimeoutError:
            logger.warning(f"MCU response timeout (cid=0x{cid:02x} seq=0x{seq:02x})")
            return None

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
