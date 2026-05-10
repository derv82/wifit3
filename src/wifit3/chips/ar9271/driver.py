import asyncio
import logging
import struct
import usb.core
from typing import Optional, Dict

from .transport import AR9271USBTransport
from .protocol.wmi import WMIProtocol
from .protocol.metadata import AthMetadataLayer
from .constants import *

logger = logging.getLogger(__name__)

class AR9271Driver:
    """
    Main Driver for the Atheros AR9271.
    Orchestrates protocols and device state using the USBTransport abstraction.
    """
    
    def __init__(self, dev: usb.core.Device):
        self.transport = AR9271USBTransport(dev)
        self.wmi = WMIProtocol()
        self.meta = AthMetadataLayer()
        
        # Handshake Events
        self._htc_ready_event = asyncio.Event()
        self._htc_config_done_event = asyncio.Event()
        self._wmi_ready_event = asyncio.Event()
        
        # Mapping for ACKs and WMI Events
        self._event_queues: Dict[int, asyncio.Queue] = {}
        
        # State tracking
        self.wmi_endpoint_id = HTC_ENDPOINT_WMI # Updated after connect
        self.mac_address = None
        self.total_credits = 0

        # Subscribe to transport packets
        self.transport.subscribe(0, self._on_htc_control)

    async def connect(self):
        """
        Performs the event-driven HTC/WMI handshake using the 'Golden Query' sequence.
        """
        logger.info("Starting AR9271 Handshake via USBTransport (Golden Query)...")
        
        # 1. Start Transport
        await self.transport.start()
        
        # 2. Wait for HTC Ready (ID 1)
        logger.info("[1/6] Waiting for HTC Ready...")
        try:
            await asyncio.wait_for(self._htc_ready_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.error("Timed out waiting for HTC Ready signal.")
            return False

        # 3. Connect Nine Services (TShark Verified Pipes)
        # WMI (0x0100) -> DL=3, UL=4
        # Others -> DL=2, UL=1
        logger.info("[2/6] Connecting 9 services...")
        
        # WMI Service
        wmi_payload = struct.pack(">HHHBBBB", 0x0002, 0x0100, 0, 3, 4, 0, 0)
        await self.transport.send(0, wmi_payload, is_wmi=False)
        await asyncio.sleep(0.01)
        
        others = [0x0101, 0x0102, 0x0103, 0x0104, 0x0107, 0x0108, 0x0106, 0x0105]
        for svc_id in others:
            payload = struct.pack(">HHHBBBB", 0x0002, svc_id, 0, 2, 1, 0, 0)
            await self.transport.send(0, payload, is_wmi=False)
            await asyncio.sleep(0.005)

        # 4. Config Pipe (ID 5) - Assign credits to Pipe 1
        logger.info("[3/6] Configuring HTC Pipe (Pipe 1 -> 33 Credits)...")
        config_pipe = struct.pack(">HBB", 0x0005, 1, self.total_credits)
        await self.transport.send(0, config_pipe, is_wmi=False)
        await asyncio.sleep(0.01)

        # 5. Send Setup Complete (ID 4)
        logger.info("[4/6] Completing HTC Handshake...")
        setup_done = struct.pack(">H", 0x0004)
        await self.transport.send(0, setup_done, is_wmi=False)
        
        await asyncio.sleep(0.02) 

        # 6. WMI Handshake
        logger.info("[5/6] Probing Hardware (REG_READ 0x4020)...")
        # WMI is now active on its assigned EP
        self.transport.subscribe(self.wmi_endpoint_id, self._on_wmi_packet)
        
        # Seq 1: The 'Golden Query'
        if not await self.send_wmi_command(WMI_REG_READ_CMDID, struct.pack(">I", 0x00004020)):
            logger.error("Golden Register Query (0x4020) failed. Subsystem is non-responsive.")
            return False
            
        # Seq 2: Version Query
        logger.info("[6/6] Querying Firmware Version...")
        if not await self.send_wmi_command(WMI_GET_FW_VERSION, b""):
            return False
            
        # Seq 3: Radio Init
        logger.info("Initializing Radio core...")
        if not await self.send_wmi_command(0x0006, b""):
            return False
            
        logger.info("AR9271 Driver successfully connected.")
        return True

    def _on_htc_control(self, payload: bytes):
        """Callback for EP 0 packets."""
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
        """Callback for WMI packets."""
        try:
            ev_id, seq, wmi_payload = self.wmi.unpack_event(payload)
            
            # ACK matching
            if seq in self._event_queues:
                self._event_queues[seq].put_nowait((ev_id, wmi_payload))
                return
                
            # Async Events
            if ev_id == WMI_READY_EVENTID:
                self.mac_address = ":".join(f"{b:02x}" for b in wmi_payload[:6])
                logger.info(f"Verified AR9271 MAC Address: {self.mac_address}")
                self._wmi_ready_event.set()
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
                res_ev_id, res_payload = await asyncio.wait_for(ack_queue.get(), timeout=1.0)
                # Log register values for the Golden Query
                if command_id == WMI_REG_READ_CMDID and len(res_payload) >= 4:
                    val = struct.unpack(">I", res_payload[-4:])[0]
                    logger.info(f"Register Read Response: {hex(val)}")
                return True
            except asyncio.TimeoutError:
                logger.warning(f"Timeout waiting for ACK for WMI Command {hex(command_id)} (Seq {seq})")
                return False
            finally:
                if seq in self._event_queues:
                    del self._event_queues[seq]
        return True

    async def set_channel(self, channel: int):
        from .sequences.tuning import get_channel_hop_sequence
        logger.info(f"Tuning AR9271 to Channel {channel}...")
        sequence = get_channel_hop_sequence(channel)
        for cmd_id, payload in sequence:
            success = await self.send_wmi_command(cmd_id, payload)
            if not success: return False
        return True

    async def close(self):
        await self.transport.stop()
        logger.info("AR9271 Driver closed.")
