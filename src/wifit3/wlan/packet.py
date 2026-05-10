import logging
from typing import Optional, List, Tuple

logger = logging.getLogger(__name__)

class WlanFrameParser:
    """
    Lightweight, native 802.11 parser for AR9271.
    Handles WMI_RX_STATUS metadata and basic management frame extraction.
    Avoids heavy dependencies like Scapy.
    """

    # --- 802.11 Constants ---
    TYPE_MGMT = 0x00
    SUBTYPE_BEACON = 0x08
    SUBTYPE_PROBE_RESP = 0x05

    @staticmethod
    def parse_wmi_rx(payload: bytes) -> Tuple[Optional[str], Optional[int], Optional[bytes]]:
        """
        Parses a WMI RX event payload (Stripped: Status Block + 802.11 Frame).
        Uses a high-fidelity signature hunt to find the 802.11 frame start.
        Returns: (ssid, rssi, raw_frame)
        """
        if len(payload) < 32: 
            return None, None, None

        # 1. Extract RSSI (Dynamic Strike for v1.4)
        # Verified SNR/RSSI units at Status Indices 8, 9, and 11.
        raw_rssi = max(payload[8], payload[9], payload[11])
        rssi = raw_rssi - 95 if raw_rssi > 0 else -95
        
        # 2. High-Fidelity Signature Hunt (Dynamic Offset)
        # We look for [FrameControl][Duration][BcastMAC] signature
        frame = None
        for off in [32, 36, 40, 44, 48]:
            if len(payload) >= off + 24:
                fc = payload[off]
                ftype = (fc & 0x0C) >> 2
                
                # Management Check: Signature includes Broadcast MAC at Addr1
                if ftype == 0:
                    if payload[off+4 : off+10] == b'\xff\xff\xff\xff\xff\xff':
                        frame = payload[off:]
                        break
                
                # Data Check: Usually starts with 0x08 or 0x88
                elif ftype == 2:
                    # Stricter check for data could go here
                    frame = payload[off:]
                    break
        
        if not frame:
            return None, rssi, None

        # 3. SSID Extraction (Management frames only)
        fc = frame[0]
        ftype = (fc & 0x0C) >> 2
        subtype = (fc & 0xF0) >> 4
        
        ssid = None
        if ftype == WlanFrameParser.TYPE_MGMT:
            if subtype == WlanFrameParser.SUBTYPE_BEACON or subtype == WlanFrameParser.SUBTYPE_PROBE_RESP:
                ssid = WlanFrameParser._extract_ssid(frame)

        return ssid, rssi, frame

    @staticmethod
    def _extract_ssid(frame: bytes) -> Optional[str]:
        """
        Surgically extracts SSID from Tag 0. Handles hidden SSIDs and junk data.
        """
        if len(frame) < 38: return None
        ptr = 36 # Skip 24-byte HDR + 12-byte Fixed Params

        if WlanFrameParser.is_frame_corrupt(frame, ptr):
            return None
        
        while ptr + 2 <= len(frame):
            tag_id = frame[ptr]
            tag_len = frame[ptr + 1]

            tag_start = ptr + 2
            tag_end = tag_start + tag_len
            if tag_end > len(frame):
                break # bounds check
            
            if tag_id == 0:
                if tag_len == 0:
                    return "<hidden>"

                if tag_len > 32:
                    return None # SSID max length is 32
                
                ssid_bytes = frame[tag_start : tag_end]
                if any(b < 0x20 and b not in (0x09, 0x0a, 0x0d) for b in ssid_bytes):
                    return None # Assume corruption
                
                try:
                    return ssid_bytes.decode('utf-8', errors='backslashreplace')
                except:
                    return None  # Assume corruption
                
            ptr = tag_end
        return None

    @staticmethod
    def is_frame_corrupt(frame, ptr):
        # SPEC: Tag 0 (SSID) MUST be first
        if frame[ptr] != 0: return True
        
        # Check only first 3 tags to avoid hitting the trailing padding
        count = 0
        while ptr + 2 <= len(frame) and count < 3:
            t_id, t_len = frame[ptr], frame[ptr+1]
            
            # 1. Bounds check
            if ptr + 2 + t_len > len(frame): return True
            
            # 2. Strict Ordering check (Spec 9.4.2.1)
            if count == 0 and t_id != 0: return True # SSID
            if count == 1 and t_id != 1: 
                # If Tag 0 is followed by something other than Tag 1, 
                # it's shifted/corrupt.
                return True 
                
            ptr += 2 + t_len
            count += 1
            
        return False # First few tags are spec-compliant