import asyncio
import logging
import struct
import usb.core
from typing import Dict, List, Callable, Tuple, Optional

from .protocol.htc import HTCProtocol
from .constants import *

logger = logging.getLogger(__name__)

class CreditManager:
    """
    Manages AR9271 buffer credits to prevent device-side overflow.
    """
    def __init__(self):
        self._credits: Dict[int, int] = {0: 512} # EP 0 (Control) has virtual infinite credits
        self._condition = asyncio.Condition()

    async def update(self, ep_id: int, count: int):
        """Replenishes credits for a specific endpoint."""
        async with self._condition:
            self._credits[ep_id] = self._credits.get(ep_id, 0) + count
            logger.debug(f"Credits replenished for EP {ep_id}: +{count} (Total: {self._credits[ep_id]})")
            self._condition.notify_all()

    async def acquire(self, ep_id: int):
        """Blocks until a credit is available for the given endpoint."""
        async with self._condition:
            while self._credits.get(ep_id, 0) <= 0:
                logger.debug(f"Waiting for credits on EP {ep_id}...")
                await self._condition.wait()
            self._credits[ep_id] -= 1

    def set_initial(self, ep_id: int, count: int):
        """Force-sets the credit count (used during READY handshake)."""
        self._credits[ep_id] = count
        logger.info(f"Initial credits set for EP {ep_id}: {count}")

class AR9271USBTransport:
    """
    Handles raw USB communication, HTC encapsulation, and credit management.
    Separates the 'plumbing' from the protocol logic.
    """
    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self.htc = HTCProtocol()
        self.credit_manager = CreditManager()

        self.is_running = False
        self._listeners: List[asyncio.Task] = []
        self._subscribers: Dict[int, List[Callable[[bytes], None]]] = {}

        # Bulk-IN stream reassembly buffer — accumulates partial HIF chunks
        # across USB transfer boundaries. Mirrors the kernel's
        # ath9k_hif_usb_rx_stream / remain_skb pair (hif_usb.c:553).
        self._rx_buf = bytearray()

        # State
        self.wmi_ep_id = 1 # Default

    def subscribe(self, htc_ep_id: int, callback: Callable[[bytes], None]):
        """Registers a callback for packets arriving on a specific HTC endpoint."""
        if htc_ep_id not in self._subscribers:
            self._subscribers[htc_ep_id] = []
        self._subscribers[htc_ep_id].append(callback)

    async def start(self):
        """Spawns the background listener tasks."""
        self.is_running = True
        self._rx_buf.clear()

        self._listeners.append(asyncio.create_task(self._read_loop(USB_EP_HTC_CTRL_IN, "Control")))
        # Single Bulk-IN reader. Earlier versions spawned 4 concurrent
        # _read_loop tasks here under the banner of "Multi-URB Pressure",
        # which produced ~40 % FCS-corrupt frames in hw test (2026-05-22):
        # PyUSB's sync dev.read() through run_in_executor lets URB
        # completions race and split bundled HTC frames across consumers.
        # The kernel achieves multi-URB depth via libusb_submit_transfer
        # from ONE consumer — that's the right port, but a single reader
        # is already low-corruption enough at current traffic levels.
        self._listeners.append(asyncio.create_task(self._read_loop(USB_EP_DATA_WMI_IN, "Data")))

        logger.info("USB Transport started with 2 listeners (Control + Data).")

    def reset_pipes(self):
        """Resets toggle bits and clears stalls on critical endpoints."""
        try:
            self.dev.clear_halt(USB_EP_DATA_WMI_IN)
            self.dev.clear_halt(USB_EP_WMI_CMD_OUT)
            logger.info("USB Pipes reset (Toggle bits cleared).")
        except Exception as e:
            logger.warning(f"Failed to reset pipes: {e}")

    async def stop(self):
        """Shuts down listeners and releases resources."""
        self.is_running = False
        for task in self._listeners:
            task.cancel()
        self._listeners = []
        logger.info("USB Transport stopped.")

    async def send(self, htc_ep_id: int, payload: bytes, is_wmi: bool = True, is_data: bool = False):
        """
        Encapsulates and sends a packet, waiting for credits if necessary.
        """
        # 1. Wait for Credits
        await self.credit_manager.acquire(htc_ep_id)
        
        # 2. Pack based on service type
        if is_wmi:
            packet = self.htc.pack_wmi(htc_ep_id, payload)
        else:
            packet = self.htc.pack_control(htc_ep_id, payload)
            
        from .usb_logger import USBInterceptor
        loop = asyncio.get_running_loop()

        # 3. Route to the correct USB Endpoint
        if is_data:
            # Data frames go to the Bulk OUT endpoint (0x01) and require a 4-byte HIF header
            USB_EP_WLAN_TX = 0x01
            hif_header = struct.pack("<HH", len(packet), 0x697e)
            bulk_packet = hif_header + packet
            
            USBInterceptor.log_tx(USB_EP_WLAN_TX, bulk_packet)
            await loop.run_in_executor(None, self.dev.write, USB_EP_WLAN_TX, bulk_packet)
        else:
            # WMI Commands and HTC Management go to the Interrupt OUT endpoint (0x04)
            USBInterceptor.log_tx(USB_EP_WMI_CMD_OUT, packet)
            await loop.run_in_executor(None, self.dev.write, USB_EP_WMI_CMD_OUT, packet)

    async def _read_loop(self, ep_addr: int, name: str):
        """Continuous polling of an IN endpoint."""
        loop = asyncio.get_running_loop()
        while self.is_running:
            try:
                raw = await loop.run_in_executor(
                    None, self.dev.read, ep_addr, 4096, 100
                )
                if not raw:
                    continue
                
                data = bytes(raw)
                from .usb_logger import USBInterceptor
                USBInterceptor.log_rx(ep_addr, data)
                
                # Handle the raw packet
                await self._handle_incoming(data, ep_addr)

            except usb.core.USBError as e:
                # Suppress normal timeout (10060/110) AND Access Denied (13) during re-enumeration spam
                if e.errno not in (10060, 110, 13): 
                    logger.error(f"Transport {name} USBError: {e}")
                
                # If we get Access Denied, it's a sign the handle is stale or OS locked.
                # We sleep longer to avoid hammering the CPU.
                await asyncio.sleep(0.1 if e.errno == 13 else 0.001)
            except Exception as e:
                logger.error(f"Transport {name} Error: {e}")
                await asyncio.sleep(0.01)

    async def _handle_incoming(self, data: bytes, ep_addr: int):
        """Routes an incoming URB to the right parser.

        Bulk-IN (0x82) carries HIF-wrapped HTC frames that can be bundled in
        one URB and/or split across URBs — see _handle_bulk_in below.
        Interrupt-IN (0x83) carries one un-wrapped HTC frame per URB.
        """
        if ep_addr == USB_EP_DATA_WMI_IN:
            await self._handle_bulk_in(data)
        else:
            await self._process_htc_frame(bytes(data))

    async def _handle_bulk_in(self, data: bytes):
        """Kernel-faithful HIF stream reassembler for Bulk-IN.

        Each chunk on the wire is `[pkt_len: LE16][pkt_tag: LE16][HTC frame
        of pkt_len B][pad to 4 B]`. Multiple chunks can share a URB; a chunk
        can also straddle URB boundaries — bytes carry over in self._rx_buf
        until enough of them have arrived to decode the next chunk.

        Mirrors data_dumps/ath9k-source-v6.18/hif_usb.c:553-712. Notably, a
        bad tag means the stream is desynced — we drop the buffer (kernel
        does the same via `goto invalid_pkt`).
        """
        self._rx_buf.extend(data)

        while len(self._rx_buf) >= 4:
            pkt_len = int.from_bytes(self._rx_buf[0:2], "little")
            pkt_tag = int.from_bytes(self._rx_buf[2:4], "little")

            if pkt_tag != ATH_USB_RX_STREAM_MODE_TAG:
                logger.warning(
                    f"HIF tag mismatch: 0x{pkt_tag:04x} — dropping "
                    f"{len(self._rx_buf)} buffered bytes (stream desync)"
                )
                self._rx_buf.clear()
                return

            if pkt_len == 0 or pkt_len > 2 * HIF_MAX_RX_BUF_SIZE:
                logger.warning(f"HIF pkt_len out of range: {pkt_len} — flushing buffer")
                self._rx_buf.clear()
                return

            pad_len = (4 - (pkt_len & 0x3)) & 0x3
            total = 4 + pkt_len + pad_len

            if len(self._rx_buf) < total:
                # Frame hasn't fully landed yet — leave it in _rx_buf and
                # wait for the next URB to bring the rest.
                return

            htc_frame = bytes(self._rx_buf[4 : 4 + pkt_len])
            del self._rx_buf[:total]
            await self._process_htc_frame(htc_frame)

    async def _process_htc_frame(self, htc_frame: bytes):
        """Unpack one fully-reassembled HTC frame and dispatch."""
        if len(htc_frame) < self.htc.HTC_HDR_STD_LEN:
            return

        try:
            ep, flags, p_len, ctrl = struct.unpack(">BBH4s", htc_frame[:8])
        except struct.error as e:
            logger.debug(f"HTC unpack fail: {e}")
            return

        payload = htc_frame[8 : 8 + p_len]
        trailer_len = ctrl[0]

        actual_payload = payload
        if flags & 0x02:  # HTC_FLAGS_RECV_TRAILER
            if trailer_len > 0 and len(payload) >= trailer_len:
                trailer = payload[-trailer_len:]
                actual_payload = payload[:-trailer_len]

                # Credit-report records are 2 bytes each: [EPID][Credits].
                # The optional 0x00 0xC6 header marks "credit-report block";
                # skip past it when present.
                start_off = 0
                if len(trailer) >= 2 and trailer[0] == 0x00 and trailer[1] == 0xC6:
                    start_off = 2
                for i in range(start_off, len(trailer), 2):
                    if i + 1 < len(trailer):
                        rep_ep, rep_count = trailer[i], trailer[i + 1]
                        if rep_ep < 22:
                            await self.credit_manager.update(rep_ep, rep_count)

        if ep == 0 and len(actual_payload) >= 6:
            res = self.htc.parse_ready_msg(actual_payload)
            if res:
                credits, _ = res
                self.credit_manager.set_initial(1, credits)
                self.credit_manager.set_initial(2, credits)

        if ep in self._subscribers:
            for cb in self._subscribers[ep]:
                cb(actual_payload)
