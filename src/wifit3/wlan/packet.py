import struct
import logging
from typing import Optional, List, Tuple

logger = logging.getLogger(__name__)

class WlanFrameParser:
    """
    Lightweight, native 802.11 parser for AR9271.
    Handles WMI_RX_STATUS metadata and basic management frame extraction.
    Avoids heavy dependencies like Scapy.
    """

    # --- WMI RX Status (v1.4) ---
    # According to Lead: 30 bytes fixed length
    # RSSI is at index 14, Rate at index 17
    WMI_RX_STATUS_LEN = 30
    
    # --- 802.11 Constants ---
    TYPE_MGMT = 0x00
    SUBTYPE_BEACON = 0x08
    SUBTYPE_PROBE_RESP = 0x05

    @staticmethod
    def parse_wmi_rx(payload: bytes) -> Tuple[Optional[str], Optional[int], Optional[bytes]]:
        """
        Parses a WMI_RX_EVENTID payload.
        Offsets: [0-29] WMI_RX_STATUS, [30+] 802.11 Frame
        Returns: (ssid, rssi, raw_frame)
        """
        if len(payload) < WlanFrameParser.WMI_RX_STATUS_LEN + 24:
            return None, None, None

        # 1. Extract Metadata
        # rssi = payload[14], rate = payload[17]
        # Atheros RSSI is usually in dBm relative to noise floor
        rssi = payload[14]
        if rssi > 128: rssi -= 256 # Handle signed byte

        # 2. Re-align to 802.11 Frame
        # Lead says frame starts at offset 30 (WMI_RX_STATUS ends there)
        # Note: 42 was the offset from the START of the URB (including 12-byte HIF/HTC)
        frame = payload[WlanFrameParser.WMI_RX_STATUS_LEN:]
        
        if len(frame) < 24:
            return None, rssi, None

        # 3. Basic 802.11 Header Check
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
        Surgically extracts SSID from 802.11 management frames.
        Skip: Header(24) + Fixed Params(12) = 36 bytes
        Then parse Information Elements (IEs).
        """
        if len(frame) < 38:
            return None

        # Offset to first IE
        ptr = 36
        
        # IE Loop: [ID(1)] [LEN(1)] [DATA(N)]
        while ptr + 2 <= len(frame):
            ie_id = frame[ptr]
            ie_len = frame[ptr + 1]
            
            if ptr + 2 + ie_len > len(frame):
                break
                
            if ie_id == 0: # SSID Tag
                try:
                    return frame[ptr + 2 : ptr + 2 + ie_len].decode('utf-8', errors='ignore')
                except Exception:
                    return "<hex:" + frame[ptr + 2 : ptr + 2 + ie_len].hex() + ">"
            
            ptr += 2 + ie_len
            
        return None
