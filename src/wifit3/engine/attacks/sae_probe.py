import struct
import time
import logging
import asyncio
from typing import Dict, Optional
from wifit3.engine.models import AccessPoint

logger = logging.getLogger(__name__)

class SAEGroupProbeAttack:
    """
    Probes an Access Point for vulnerable SAE Groups (like Dragonblood 22, 23, 24).
    Sends a dummy SAE Commit and listens for the status code in the response.
    """
    
    def __init__(self, iface, target: AccessPoint):
        self.iface = iface
        self.target = target
        self.results: Dict[int, str] = {}
        
    def _craft_sae_commit(self, bssid_mac: bytes, source_mac: bytes, group_id: int) -> bytes:
        # Frame Control: Mgmt (0x00), Auth (0x0B -> 11) -> 0xB0 0x00
        # Duration: 0x00 0x00
        # Addr1 (Dest): bssid_mac
        # Addr2 (Source): source_mac
        # Addr3 (BSSID): bssid_mac
        # Seq Control: 0x00 0x00
        
        fc = b'\xb0\x00'
        duration = b'\x00\x00'
        seq_ctrl = b'\x00\x00'
        
        mac_header = fc + duration + bssid_mac + source_mac + bssid_mac + seq_ctrl
        
        # Auth Body
        # Auth Algorithm: 3 (SAE) -> \x03\x00
        # Auth Transaction: 1 -> \x01\x00
        # Status Code: 0 -> \x00\x00
        # Group ID: 2 bytes
        # Dummy Scalar & Element: just some random bytes, e.g., 32 bytes each
        
        auth_algo = b'\x03\x00'
        transaction = b'\x01\x00'
        status = b'\x00\x00'
        group = struct.pack("<H", group_id)
        
        # 32 bytes scalar + 32 bytes element is fine for P-256 (Group 19).
        # We just need enough bytes so the AP parses it as a Commit, 
        # even if the math is wrong, it should still evaluate if the group is supported first.
        dummy_scalar = b'\x11' * 32
        dummy_element = b'\x22' * 32
        
        body = auth_algo + transaction + status + group + dummy_scalar + dummy_element
        return mac_header + body

    async def run(self, groups_to_test=(19, 20, 22, 23, 24), timeout=1.0):
        """
        Runs the probe against specified SAE groups.
        """
        if not self.target.wpa3:
            logger.warning(f"Target {self.target.bssid} is not marked as WPA3.")

        # Convert MAC string to bytes
        bssid_bytes = bytes(int(x, 16) for x in self.target.bssid.split(':'))
        source_mac = b'\x02\x11\x22\x33\x44\x55' 
        
        for group in groups_to_test:
            logger.info(f"Probing SAE Group {group} on {self.target.bssid}")
            frame = self._craft_sae_commit(bssid_bytes, source_mac, group)
            
            # State for callback
            response_status: Optional[int] = None
            
            def auth_callback(frame_bytes: bytes, rssi: int, ts: float):
                nonlocal response_status
                if len(frame_bytes) >= 24 + 6:
                    fc0 = frame_bytes[0]
                    # Check if Authentication frame (Type 0, Subtype 11 -> 0xB0)
                    if (fc0 & 0xFC) == 0xB0:
                        addr1 = frame_bytes[4:10]
                        addr2 = frame_bytes[10:16]
                        if addr1 == source_mac and addr2 == bssid_bytes:
                            # MAC header is typically 24 bytes
                            body = frame_bytes[24:]
                            algo = struct.unpack("<H", body[0:2])[0]
                            seq = struct.unpack("<H", body[2:4])[0]
                            if algo == 3 and seq == 1:
                                status_code = struct.unpack("<H", body[4:6])[0]
                                response_status = status_code

            # Register listener
            self.iface.register_rx_callback(auth_callback)
            
            # Send frame
            await self.iface.send_raw(frame, use_no_ack=True)
            
            # Wait for response
            start_time = time.time()
            while time.time() - start_time < timeout:
                if response_status is not None:
                    break
                await asyncio.sleep(0.05)
                
            # Unregister listener
            if hasattr(self.iface, '_rx_callbacks') and auth_callback in self.iface._rx_callbacks:
                self.iface._rx_callbacks.remove(auth_callback)
            
            if response_status == 0:
                self.results[group] = "Supported"
            elif response_status == 77:
                self.results[group] = "Rejected (Unsupported)"
            else:
                if response_status is None:
                    self.results[group] = "Timeout (Dropped)"
                else:
                    self.results[group] = f"Unknown Status ({response_status})"

        return self.results
