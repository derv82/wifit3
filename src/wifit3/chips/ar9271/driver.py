import asyncio
import logging
import struct
import json
import os
import usb.core
from typing import Optional, Dict, List, Tuple
from pathlib import Path

from .transport import AR9271USBTransport
from .protocol.wmi import WMIProtocol
from .protocol.metadata import AthMetadataLayer
from .constants import *
from wifit3.wlan.packet import WlanFrameParser

logger = logging.getLogger(__name__)

class AR9271Driver:
    """
    Main Driver for the Atheros AR9271.
    Orchestrates protocols and device state using the USBTransport abstraction.
    """
    
    def __init__(self, dev: usb.core.Device, is_warm: bool = False):
        self.transport = AR9271USBTransport(dev)
        self.wmi = WMIProtocol()
        self.meta = AthMetadataLayer()
        self.is_warm = is_warm
        
        # Handshake Events
        self._htc_ready_event = asyncio.Event()
        self._htc_config_done_event = asyncio.Event()
        self._wmi_ready_event = asyncio.Event()
        
        # Mapping for ACKs and WMI Events
        self._event_queues: Dict[int, asyncio.Queue] = {}
        
        # State tracking
        self.wmi_endpoint_id = HTC_ENDPOINT_WMI
        self.data_endpoint_id = 5
        self.mac_address = None
        self.total_credits = 0
        self._rx_callback = None

        # Subscribe to transport packets
        # EP 0 for HTC/WMI Management, EP 1 for WMI Commands/Events
        self.transport.subscribe(0, self._on_htc_control)

    def register_rx_callback(self, cb):
        self._rx_callback = cb

    async def connect(self):
        """
        Performs the event-driven HTC/WMI handshake and 'Ultimate Marathon' replay.
        """
        if self.is_warm:
            logger.info("Device is already WARM. Re-attaching to existing HTC/WMI streams...")
            self.total_credits = 33 # Assume default from previous handshake
            self.wmi_endpoint_id = HTC_ENDPOINT_WMI # WMI is mapped to HTC endpoint 1
            self.data_endpoint_id = 5 # Default Data EP
            
            # Seed the CreditManager to prevent deadlocks since we skipped the HTC_READY handshake
            self.transport.credit_manager.set_initial(self.wmi_endpoint_id, self.total_credits)
            self.transport.credit_manager.set_initial(self.data_endpoint_id, self.total_credits)
            
            self._htc_ready_event.set()
            self._htc_config_done_event.set()
            self._wmi_ready_event.set()
            
            # We MUST clear the halt/toggle bits on the pipes so Bulk OUT doesn't drop packets
            self.transport.reset_pipes()
            await asyncio.sleep(0.05)
            
            await self.transport.start()
            
            # Subscribe to the data streams
            self.transport.subscribe(0, self._on_wmi_packet)
            self.transport.subscribe(1, self._on_wmi_packet)
            self.transport.subscribe(3, self._on_wmi_packet)
            logger.info("AR9271 Driver successfully re-attached to WARM device.")
            return True

        logger.info("Starting AR9271 Handshake via USBTransport (Final Marathon)...")
        
        await self.transport.start()
        
        logger.info("[1/5] Waiting for HTC Ready...")
        try:
            await asyncio.wait_for(self._htc_ready_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.error("Timed out waiting for HTC Ready signal.")
            return False

        logger.info("[2/5] Connecting 9 services...")
        # WMI Service (0x0100) -> DL=3, UL=4
        wmi_payload = struct.pack(">HHHBBBB", 0x0002, 0x0100, 0, 3, 4, 0, 0)
        await self.transport.send(0, wmi_payload, is_wmi=False)
        await asyncio.sleep(0.001) # Accelerated
        
        others = [0x0101, 0x0102, 0x0103, 0x0104, 0x0107, 0x0108, 0x0106, 0x0105]
        for svc_id in others:
            payload = struct.pack(">HHHBBBB", 0x0002, svc_id, 0, 2, 1, 0, 0)
            await self.transport.send(0, payload, is_wmi=False)
            await asyncio.sleep(0.001) # Accelerated

        logger.info("[3/5] Configuring HTC Pipe (Pipe 1 -> 33 Credits)...")
        config_pipe = struct.pack(">HBB", 0x0005, 1, self.total_credits)
        await self.transport.send(0, config_pipe, is_wmi=False)
        await asyncio.sleep(0.005)

        logger.info("[4/5] Completing HTC Handshake...")
        setup_done = struct.pack(">H", 0x0004)
        await self.transport.send(0, setup_done, is_wmi=False)
        
        self.transport.reset_pipes()
        await asyncio.sleep(0.05) # Half-breath

        # 6. Ultimate Calibration Marathon
        logger.info("[5/5] Replaying 1,200-Packet Calibration Table...")
        # Subscribe to Management (0), WMI (1), and CAB (3) for the Bulk stream
        self.transport.subscribe(0, self._on_wmi_packet)
        self.transport.subscribe(1, self._on_wmi_packet)
        self.transport.subscribe(3, self._on_wmi_packet)
        
        cal_path = Path(__file__).parent / "assets" / "ar9271_v14_init.json"
        try:
            with open(cal_path, 'r') as f:
                payloads = json.load(f)
            
            logger.info(f"Replaying {len(payloads)} packets sequentially...")
            for i, p_hex in enumerate(payloads):
                raw_wmi = bytes.fromhex(p_hex)
                cmd_id, p_seq = struct.unpack(">HH", raw_wmi[:4])
                payload_data = raw_wmi[4:]
                
                # Rigid Stop-and-Wait for 1-slot mailbox
                success = await self.send_wmi_command(cmd_id, payload_data, wait_for_ack=True)
                
                if not success:
                    logger.warning(f"Timeout at packet {i} (Cmd {hex(cmd_id)}). Continuing...")
                
                if i > 0 and i % 200 == 0:
                    logger.info(f"Progress: {i}/{len(payloads)} registers processed.")
                    
            logger.info("Calibration marathon complete. Triggering Radio Init...")
            
            # Final Trigger Sequence
            await self.send_wmi_command(WMI_SET_MODE_CMDID, struct.pack(">H", 0x0001))
            await self.send_wmi_command(0x0006, b"") # INIT
            await self.send_wmi_command(WMI_START_RECV_CMDID, b"")
            
        except FileNotFoundError:
            logger.error(f"Calibration file not found at {cal_path}")
            return False

        logger.info("Waiting for Target Ready and MAC verification...")
        try:
            # Accelerated wait: v1.4 usually responds within 500ms post-marathon
            await asyncio.wait_for(self._wmi_ready_event.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("Timed out waiting for WMI Target Ready event. Proceeding anyway...")
        
        logger.info("AR9271 Driver successfully connected.")
        return True

    def _on_htc_control(self, payload: bytes):
        """Callback for EP 0 packets arriving on Interrupt pipe (0x83)."""
        if len(payload) < 2: return
        msg_id = struct.unpack(">H", payload[:2])[0]
        if msg_id == 0x0001: # READY
            credits = struct.unpack(">H", payload[2:4])[0]
            self.total_credits = credits
            self._htc_ready_event.set()
        elif msg_id == 0x0003: # CONNECT_RSP
            svc_id = struct.unpack(">H", payload[2:4])[0]
            status, epid = struct.unpack(">BB", payload[4:6])
            if status == 0:
                if svc_id == 0x0100:
                    self.wmi_endpoint_id = epid
                logger.debug(f"Service {hex(svc_id)} connected on EP {epid}")
        elif msg_id == 0x0006: # CONFIG_PIPE_RSP
            self._htc_config_done_event.set()

    def _on_wmi_packet(self, payload: bytes):
        """Callback for WMI and Management packets arriving on Bulk pipe (0x82)."""
        try:
            ev_id, seq, wmi_payload = self.wmi.unpack_event(payload)
            
            # 1. ACK matching for synchronous commands
            if seq in self._event_queues:
                self._event_queues[seq].put_nowait((ev_id, wmi_payload))
                return
            
            # 2. Async Target Ready - can arrive on EP 0 or 1
            if ev_id == WMI_READY_EVENTID or ev_id == WMI_READY_EP0_ID:
                self.mac_address = ":".join(f"{b:02x}" for b in wmi_payload[:6])
                logger.info(f"[!!!] AR9271 SILICON MAC VERIFIED: {self.mac_address}")
                self._wmi_ready_event.set()
                
            # 3. Handle live RX traffic (Beacons, Data, etc.)
            elif ev_id in [WMI_RECV_PDU_EVENTID, WMI_RECV_PDU_V14_ID, WMI_RECV_PDU_V14_BCN_ID]:
                parsed = WlanFrameParser.parse_wmi_rx(wmi_payload)
                if parsed and self._rx_callback:
                    self._rx_callback(parsed)
            
            elif ev_id & 0x1000:
                logger.debug(f"Async WMI Event: ID={hex(ev_id)} LEN={len(wmi_payload)}")
                
        except Exception as e:
            logger.debug(f"WMI Unpack fail: {e}")

    async def send_wmi_command(self, command_id: int, payload: bytes, wait_for_ack: bool = True):
        wmi_payload, seq = self.wmi.pack_command(command_id, payload)
        if wait_for_ack:
            ack_queue = asyncio.Queue()
            self._event_queues[seq] = ack_queue
            
        await self.transport.send(self.wmi_endpoint_id, wmi_payload, is_wmi=True)
        
        if wait_for_ack:
            try:
                # Register ACKs are near-instant (<5ms)
                res_ev_id, res_payload = await asyncio.wait_for(ack_queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                logger.debug(f"Timeout waiting for ACK on Cmd {hex(command_id)}. Resending to sync toggle bit...")
                # The hardware often ignores clear_halt(), leaving the USB toggle bit desynced.
                # The first packet is dropped, but the host advances its toggle bit.
                # Resending it forces a match!
                await self.transport.send(self.wmi_endpoint_id, wmi_payload, is_wmi=True)
                try:
                    res_ev_id, res_payload = await asyncio.wait_for(ack_queue.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    return False
            finally:
                if seq in self._event_queues:
                    del self._event_queues[seq]
                    
            if command_id == WMI_REG_READ_CMDID and len(res_payload) >= 4:
                val = struct.unpack(">I", res_payload[-4:])[0]
                #logger.info(f"Register Read Response: {hex(val)}")
                
        return True

    async def set_channel(self, channel: int):
        from .sequences.tuning import get_channel_hop_sequence
        logger.info(f"Tuning AR9271 to Channel {channel}...")
        sequence = get_channel_hop_sequence(channel)
        for cmd_id, payload in sequence:
            success = await self.send_wmi_command(cmd_id, payload)
            if not success: return False
        return True

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        """
        Injects a raw 802.11 frame onto the air.
        Wraps the frame in the `ath_tx_status` hardware descriptor.
        """
        # Pack the hardware TX descriptor (rate 0x0B, no_ack flag)
        tx_payload = self.meta.pack_tx(frame_bytes, rate_idx=0x0B, no_ack=use_no_ack)
        
        # Dispatch to the dynamic Data endpoint (usually EP 5)
        # is_wmi=False means it uses the standard 8-byte HTC header, not the WMI variant
        # is_data=True means it must go to Bulk OUT (EP 0x01) with a 4-byte HIF header!
        await self.transport.send(self.data_endpoint_id, tx_payload, is_wmi=False, is_data=True)
        return True

    async def close(self):
        await self.transport.stop()
        logger.info("AR9271 Driver closed.")
