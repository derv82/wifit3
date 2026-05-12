import logging
from typing import Optional, List, Tuple, Dict, Any

logger = logging.getLogger(__name__)

class WlanFrameParser:
    """
    Lightweight, native 802.11 parser for AR9271.
    Handles WMI_RX_STATUS metadata and extracts a rich dictionary of attributes.
    """

    # --- 802.11 Constants ---
    TYPE_MGMT = 0x00
    TYPE_CTRL = 0x01
    TYPE_DATA = 0x02

    SUBTYPE_ASSOC_REQ = 0x00
    SUBTYPE_ASSOC_RESP = 0x01
    SUBTYPE_PROBE_REQ = 0x04
    SUBTYPE_PROBE_RESP = 0x05
    SUBTYPE_BEACON = 0x08
    SUBTYPE_DEAUTH = 0x0c
    
    SUBTYPE_DATA = 0x00
    SUBTYPE_QOS_DATA = 0x08

    @staticmethod
    def parse_80211_frame(frame: bytes, rssi: int) -> Optional[Dict[str, Any]]:
        """
        Generic 802.11 frame parser.
        Receives a raw 802.11 frame and an RSSI value.
        """
        if not WlanFrameParser._is_valid_frame(frame):
            return None

        fc0 = frame[0]
        fc1 = frame[1]
        ftype = (fc0 & 0x0C) >> 2
        subtype = (fc0 & 0xF0) >> 4
        
        to_ds = (fc1 & 0x01) != 0
        from_ds = (fc1 & 0x02) != 0

        # Parse MAC Addresses
        addr1 = WlanFrameParser._mac_to_str(frame[4:10])
        addr2 = WlanFrameParser._mac_to_str(frame[10:16])
        addr3 = WlanFrameParser._mac_to_str(frame[16:22])
        
        bssid = None
        source = None
        dest = None

        if not to_ds and not from_ds: # Ad-hoc, Mgmt, or Ctrl
            dest = addr1
            source = addr2
            bssid = addr3
        elif not to_ds and from_ds: # AP -> Client
            dest = addr1
            bssid = addr2
            source = addr3
        elif to_ds and not from_ds: # Client -> AP
            bssid = addr1
            source = addr2
            dest = addr3
        else: # WDS
            return None # Ignore WDS for now
            
        result = {
            "type_id": ftype,
            "subtype_id": subtype,
            "bssid": bssid,
            "source": source,
            "dest": dest,
            "rssi": rssi,
            "raw": frame
        }

        # Subtype Identification
        if ftype == WlanFrameParser.TYPE_MGMT:
            if subtype == WlanFrameParser.SUBTYPE_BEACON:
                result["type"] = "beacon"
            elif subtype == WlanFrameParser.SUBTYPE_PROBE_REQ:
                result["type"] = "probe_req"
            elif subtype == WlanFrameParser.SUBTYPE_PROBE_RESP:
                result["type"] = "probe_resp"
            elif subtype == WlanFrameParser.SUBTYPE_DEAUTH:
                result["type"] = "deauth"
            elif subtype == WlanFrameParser.SUBTYPE_ASSOC_REQ:
                result["type"] = "assoc_req"
            elif subtype == WlanFrameParser.SUBTYPE_ASSOC_RESP:
                result["type"] = "assoc_resp"
            else:
                result["type"] = f"mgmt_{subtype}"
                
            # Parse Tags for Beacons and Probe Responses
            if subtype in (WlanFrameParser.SUBTYPE_BEACON, WlanFrameParser.SUBTYPE_PROBE_RESP):
                tags = WlanFrameParser._parse_tags(frame, subtype)
                if tags is None: return None
                result["ssid"] = tags.get("ssid")
                result["channel"] = tags.get("channel", 1)
                result["encryption"] = tags.get("encryption", "OPEN")
            elif subtype in (WlanFrameParser.SUBTYPE_PROBE_REQ, WlanFrameParser.SUBTYPE_ASSOC_REQ):
                tags = WlanFrameParser._parse_tags(frame, subtype)
                if tags is None: return None
                result["ssid"] = tags.get("ssid")
                
        elif ftype == WlanFrameParser.TYPE_DATA:
            result["type"] = "data"
            # Check for EAPOL (Handshake)
            # MAC header length is dynamic based on flags
            header_len = 24
            
            # 1. Add 6 bytes if Address 4 is present (WDS)
            if to_ds and from_ds:
                header_len += 6
                
            # 2. Add 2 bytes if QoS Subtype
            if subtype & 0x08:
                header_len += 2
                
            # 3. Add 4 bytes if HT Control is present (Check Order bit in FC1)
            if fc1 & 0x80:
                header_len += 4

            if len(frame) >= header_len + 8:
                # The hardware DMA engine pads the 802.11 header so the payload is 4-byte aligned.
                # If the header is 26 bytes, it adds 2 bytes of padding (e.g. `00 00`), pushing LLC to offset 28.
                # We use a sliding window to gracefully find the LLC/SNAP signature regardless of padding.
                llc_snap_sig = b'\xaa\xaa\x03\x00\x00\x00\x88\x8e'
                search_window = frame[header_len : header_len + 16]
                sig_idx = search_window.find(llc_snap_sig)
                
                if sig_idx != -1:
                    result["type"] = "eapol"
                    # Extract EAPOL metadata
                    eapol_start = header_len + sig_idx + 8
                    if len(frame) >= eapol_start + 17: # Header(4) + DescType(1) + KeyInfo(2) + KeyLen(2) + Replay(8)
                        eapol_type = frame[eapol_start + 1]
                        if eapol_type == 3: # EAPOL-Key
                            import struct
                            key_info = struct.unpack(">H", frame[eapol_start+5 : eapol_start+7])[0]
                            replay_counter = frame[eapol_start+9 : eapol_start+17]
                            result["eapol_key_info"] = key_info
                            result["eapol_replay_counter"] = replay_counter
        else:
            result["type"] = f"ctrl_{subtype}"

        return result

    @staticmethod
    def _is_valid_frame(frame: bytes) -> bool:
        if len(frame) < 24: return False
        fc0, fc1 = frame[0], frame[1]
        
        # Protocol version must be 0
        if (fc0 & 0x03) != 0: return False 
        
        ftype = (fc0 & 0x0C) >> 2
        subtype = (fc0 & 0xF0) >> 4
        
        if ftype == WlanFrameParser.TYPE_MGMT:
            # Enforce Strict Tag Ordering for Mgmt Frames
            if subtype in (WlanFrameParser.SUBTYPE_BEACON, WlanFrameParser.SUBTYPE_PROBE_RESP):
                ptr = 36
            elif subtype == WlanFrameParser.SUBTYPE_PROBE_REQ:
                ptr = 24
            elif subtype == WlanFrameParser.SUBTYPE_DEAUTH:
                return len(frame) >= 26
            else:
                return True
                
            if len(frame) <= ptr + 2:
                return False
                
            # SPEC: Tag 0 (SSID) MUST be first
            if frame[ptr] != 0: 
                return False
                
            # Check Tag 1 (Supported Rates)
            t0_len = frame[ptr+1]
            ptr += 2 + t0_len
            
            if len(frame) > ptr + 2:
                # If Tag 0 is followed by something other than Tag 1, 
                # it's shifted/corrupt noise.
                if frame[ptr] != 1:
                    return False
            
            return True

        if ftype == WlanFrameParser.TYPE_DATA:
            # EAPOL frames are rarely very large, but must at least hold a header
            # Check if addresses look like valid MACs (not all zeros/broadcast usually for source)
            # This helps the 'Hunt' loop distinguish between random USB noise and a frame
            if len(frame) < 24:
                return False
                
            addr1 = frame[4:10]
            addr2 = frame[10:16]
            addr3 = frame[16:22]
            
            # Source MAC (usually addr2 or addr3) should rarely be broadcast or all zeros
            if addr2 == b'\x00\x00\x00\x00\x00\x00' or addr2 == b'\xff\xff\xff\xff\xff\xff':
                return False
            if addr3 == b'\x00\x00\x00\x00\x00\x00' or addr3 == b'\xff\xff\xff\xff\xff\xff':
                return False
                
            return True

        return False

    @staticmethod
    def _mac_to_str(mac_bytes: bytes) -> str:
        if len(mac_bytes) != 6: return "00:00:00:00:00:00"
        return ":".join(f"{b:02x}" for b in mac_bytes)

    @staticmethod
    def _parse_tags(frame: bytes, subtype: int) -> Optional[Dict[str, Any]]:
        """
        Parses 802.11 Information Elements (Tags) from management frames.
        Returns a dictionary of parsed info (ssid, channel, encryption) or None if corrupt.
        """
        parsed = {}
        if subtype in (WlanFrameParser.SUBTYPE_BEACON, WlanFrameParser.SUBTYPE_PROBE_RESP):
            ptr = 36 # Skip 24-byte HDR + 12-byte Fixed Params
        elif subtype == WlanFrameParser.SUBTYPE_PROBE_REQ:
            ptr = 24 # 24-byte HDR + 0-byte Fixed Params
        else:
            return parsed

        if len(frame) < ptr + 2: return None
        
        # Strict validation: The first tag MUST be Tag 0 (SSID)
        if frame[ptr] != 0:
            return None
        
        has_wpa = False
        has_rsn = False
        
        while ptr + 2 <= len(frame):
            tag_id = frame[ptr]
            tag_len = frame[ptr + 1]

            tag_start = ptr + 2
            tag_end = tag_start + tag_len
            if tag_end > len(frame):
                break # bounds check
                
            tag_data = frame[tag_start : tag_end]
            
            if tag_id == 0: # SSID
                if tag_len == 0:
                    parsed["ssid"] = "<hidden>"
                elif tag_len <= 32:
                    # Validate against completely corrupted text
                    if any(b < 0x20 and b not in (0x09, 0x0a, 0x0d) for b in tag_data):
                        return None # Corrupt frame masquerading as valid
                    try:
                        parsed["ssid"] = tag_data.decode('utf-8', errors='ignore')
                    except:
                        pass
            elif tag_id == 3: # DS Parameter Set (Channel)
                if tag_len == 1:
                    parsed["channel"] = tag_data[0]
            elif tag_id == 48: # RSN (WPA2/WPA3)
                has_rsn = True
            elif tag_id == 221: # Vendor Specific
                if tag_len >= 4:
                    oui = tag_data[:3]
                    oui_type = tag_data[3]
                    if oui == b'\x00\x50\xf2':
                        if oui_type == 1: # WPA
                            has_wpa = True
                        elif oui_type == 4: # WPS
                            parsed["wps"] = True
                            
            ptr = tag_end
            
        if has_rsn:
            parsed["encryption"] = "WPA2" # Or WPA3, could differentiate by AKM suites later
        elif has_wpa:
            parsed["encryption"] = "WPA"
        else:
            if len(frame) >= 36:
                # Need to check capability info in fixed params for WEP
                cap_info = int.from_bytes(frame[34:36], byteorder='little')
                if cap_info & 0x0010: # Privacy bit
                    parsed["encryption"] = "WEP"
                else:
                    parsed["encryption"] = "OPEN"
            else:
                parsed["encryption"] = "OPEN"
                
        return parsed