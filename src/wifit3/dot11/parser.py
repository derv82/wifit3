"""Pure-Python 802.11 frame parsing: ``WlanFrameParser`` builds the typed ``Packet`` hierarchy
(see ``dot11.packet``) from raw MPDU bytes.
"""
import struct
from typing import Optional, List, Dict, Any

from wifit3.dot11.mac import mac_to_str
from wifit3.dot11.packet import (
    Packet, BeaconPacket, EapolPacket, WepDataPacket, AssocRequestPacket,
    AuthPacket, AssocRespPacket, DeauthPacket, ProbeReqPacket,
)


class WlanFrameParser:
    """Native 802.11 frame parser."""

    # --- 802.11 Constants ---
    TYPE_MGMT = 0x00
    TYPE_CTRL = 0x01
    TYPE_DATA = 0x02

    SUBTYPE_ASSOC_REQ = 0x00
    SUBTYPE_ASSOC_RESP = 0x01
    SUBTYPE_REASSOC_REQ = 0x02
    SUBTYPE_PROBE_REQ = 0x04
    SUBTYPE_PROBE_RESP = 0x05
    SUBTYPE_BEACON = 0x08
    SUBTYPE_DISASSOC = 0x0a
    SUBTYPE_AUTH = 0x0b
    SUBTYPE_DEAUTH = 0x0c

    @classmethod
    def parse_80211_frame(cls, frame: bytes, rssi: int) -> Optional["Packet"]:
        """Generic 802.11 frame parser: a raw MPDU + RSSI -> the matching typed
        ``Packet`` subclass, or ``None`` if the frame is noise / unparseable / an
        unsupported type (WDS, control frames).
        """
        if not cls._is_valid_frame(frame):
            return None

        fc0 = frame[0]
        fc1 = frame[1]
        ftype = (fc0 & 0x0C) >> 2
        subtype = (fc0 & 0xF0) >> 4

        to_ds = (fc1 & 0x01) != 0
        from_ds = (fc1 & 0x02) != 0

        addr1 = mac_to_str(frame[4:10])
        addr2 = mac_to_str(frame[10:16])
        addr3 = mac_to_str(frame[16:22])

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
        else: # WDS (4-address), not parsed
            return None

        base: Dict[str, Any] = {
            "type_id": ftype, "subtype_id": subtype, "bssid": bssid,
            "source": source, "dest": dest, "to_ds": to_ds, "from_ds": from_ds,
            "rssi": rssi, "raw": frame,
        }
        if ftype == cls.TYPE_MGMT:
            return cls._parse_mgmt(frame, subtype, base)
        if ftype == cls.TYPE_DATA:
            return cls._parse_data(frame, fc1, subtype, base)
        return None  # ctrl / reserved: _is_valid_frame already rejects these

    @classmethod
    def _parse_mgmt(cls, frame: bytes, subtype: int, base: Dict[str, Any]) -> Optional["Packet"]:
        """Build the Packet for a management frame.

        Beacon / Probe Response carry the AP's IEs (-> BeaconPacket); (Re)Assoc Request
        carries the client's chosen AKM (-> AssocRequestPacket); every other subtype is a
        bare Packet with a type label.
        """
        if subtype in (cls.SUBTYPE_BEACON, cls.SUBTYPE_PROBE_RESP):
            tags = cls._parse_tags(frame, subtype)
            if tags is None:
                return None
            type_str = "beacon" if subtype == cls.SUBTYPE_BEACON else "probe_resp"
            fields: Dict[str, Any] = {
                "type": type_str,
                "ssid": tags.get("ssid"),
                "encryption": tags.get("encryption", "OPEN"),
                "wpa3": tags.get("wpa3", False),
                "transition_mode": tags.get("transition_mode", False),
                "pmf_capable": tags.get("pmf_capable", False),
                "pmf_required": tags.get("pmf_required", False),
                "beacon_protection": tags.get("beacon_protection", False),
                "pairwise_cipher": tags.get("pairwise_cipher"),
                "akms": tags.get("akms", []),
                "akm_suites": tags.get("akm_suites", []),
            }
            # Copy channel / rsn_ie_raw / WPS only when the walker found them, so a missing
            # value keeps the field default.
            for key in ("channel", "rsn_ie_raw", "wps", "wps_locked", "wps_version",
                        "wps_state", "wps_config_methods", "wps_device_password_id",
                        "wps_selected_registrar"):
                if key in tags:
                    fields[key] = tags[key]
            return BeaconPacket(**base, **fields)

        if subtype == cls.SUBTYPE_PROBE_REQ:
            tags = cls._parse_tags(frame, subtype)
            if tags is None:
                return None
            return ProbeReqPacket(**base, type="probe_req", ssid=tags.get("ssid"))

        if subtype == cls.SUBTYPE_ASSOC_REQ:
            tags = cls._parse_tags(frame, subtype)
            if tags is None:
                return None
            # Client's selected AKM from the RSN IE (24 hdr + cap + listen = 28).
            return AssocRequestPacket(
                **base, type="assoc_req", ssid=tags.get("ssid"),
                assoc_akm=cls._first_rsn_akm(frame, 28))

        if subtype == cls.SUBTYPE_REASSOC_REQ:
            tags = cls._parse_tags(frame, subtype)
            if tags is None:
                return None
            # 34 = assoc's 28 + 6-byte Current AP Address.
            return AssocRequestPacket(
                **base, type="reassoc_req", ssid=tags.get("ssid"),
                assoc_akm=cls._first_rsn_akm(frame, 34))

        if subtype == cls.SUBTYPE_ASSOC_RESP:
            # Body: Capability(2) + Status(2) + AID(2); status at offset 26.
            status = struct.unpack("<H", frame[26:28])[0] if len(frame) >= 28 else None
            return AssocRespPacket(**base, type="assoc_resp", status=status)
        if subtype == cls.SUBTYPE_AUTH:
            # Body: Algorithm(2) + Seq(2) + Status(2); status at offset 28.
            status = struct.unpack("<H", frame[28:30])[0] if len(frame) >= 30 else None
            return AuthPacket(**base, type=f"mgmt_{subtype}", status=status)
        if subtype in (cls.SUBTYPE_DEAUTH, cls.SUBTYPE_DISASSOC):
            # Reason code is the 2-byte body right after the 24-byte header.
            reason = struct.unpack("<H", frame[24:26])[0] if len(frame) >= 26 else None
            label = "deauth" if subtype == cls.SUBTYPE_DEAUTH else f"mgmt_{subtype}"
            return DeauthPacket(**base, type=label, reason=reason)
        return Packet(**base, type=f"mgmt_{subtype}")

    @classmethod
    def _parse_data(cls, frame: bytes, fc1: int, subtype: int, base: Dict[str, Any]) -> "Packet":
        """Build the Packet for a data frame: WepDataPacket if WEP-protected, EapolPacket
        if it carries an EAPOL-Key handshake, else a bare 'data' Packet.
        """
        header_len = 24
        if subtype & 0x08:            # QoS Control field present (+2)
            header_len += 2
        if fc1 & 0x80:                # HT Control field present, Order bit (+4)
            header_len += 4

        # Protected frame: the Key ID byte's ExtIV bit (0x20) tells WEP (clear → 4-byte IV)
        # from TKIP/CCMP (set → 8-byte ext IV). Body is ciphertext either way, no LLC/SNAP.
        if (fc1 & 0x40) and len(frame) >= header_len + 4:
            keyid_byte = frame[header_len + 3]
            if not (keyid_byte & 0x20):   # ExtIV clear → WEP
                cipher_start = header_len + 4
                return WepDataPacket(
                    **base, type="wep_data",
                    iv=bytes(frame[header_len : header_len + 3]),
                    keyid=(keyid_byte >> 6) & 0x03,
                    # First 16 ciphertext bytes: the PTW keystream (XOR the known ARP plaintext).
                    cipher=bytes(frame[cipher_start : cipher_start + 16]))
            return Packet(**base, type="data")

        eapol = cls._parse_eapol(frame, header_len, base)
        return eapol if eapol is not None else Packet(**base, type="data")

    @classmethod
    def _parse_eapol(cls, frame: bytes, header_len: int,
                     base: Dict[str, Any]) -> Optional["EapolPacket"]:
        """Find an EAPOL-Key payload after the MAC header and build an EapolPacket, or None
        if the frame has no EAPOL LLC/SNAP. A frame carrying the EAPOL ethertype but too
        short to fully decode still returns an EapolPacket ('eapol') with whatever fields it
        reached. The interface guards on replay_counter before storing.
        """
        if len(frame) < header_len + 8:
            return None
        # DMA pads the 802.11 header for 4-byte alignment, so slide a window to find the
        # LLC/SNAP + EAPOL ethertype signature regardless of padding.
        llc_snap_sig = b'\xaa\xaa\x03\x00\x00\x00\x88\x8e'
        sig_idx = frame[header_len : header_len + 16].find(llc_snap_sig)
        if sig_idx == -1:
            return None

        # Key-descriptor offsets from eapol_start: 1=802.1X type (3=EAPOL-Key), 5=Key Info
        # (2B BE), 9=Replay Counter (8B), 17=Key Nonce (32B), 81=Key MIC (16B), 97=Key Data
        # Length (2B BE), 99=Key Data.
        fields: Dict[str, Any] = {}
        eapol_start = header_len + sig_idx + 8
        if len(frame) >= eapol_start + 99 and frame[eapol_start + 1] == 3:  # EAPOL-Key
            key_info = struct.unpack(">H", frame[eapol_start + 5: eapol_start + 7])[0]
            key_data_len = struct.unpack(">H", frame[eapol_start + 97: eapol_start + 99])[0]
            msg_num = cls._classify_eapol_msg(key_info, key_data_len)
            fields.update(
                key_info=key_info,
                replay_counter=frame[eapol_start + 9: eapol_start + 17],
                nonce=frame[eapol_start + 17: eapol_start + 49],
                mic=frame[eapol_start + 81: eapol_start + 97],
                key_data_len=key_data_len,
                msg_num=msg_num,
            )
            # The 802.1X slice (header + key descriptor + key data) is exactly what hashcat
            # -m 22000 embeds; store it now to avoid re-finding LLC/SNAP at save time.
            total_eapol_len = 99 + key_data_len
            if len(frame) >= eapol_start + total_eapol_len:
                fields["payload"] = bytes(frame[eapol_start: eapol_start + total_eapol_len])
            # PMKID KDE (M1) and the client's negotiated AKM (M2's cleartext RSN IE) both sit
            # at the START of Key Data, so a truncated tail still yields them (seen on
            # mt76x0u / rt2800usb RT5572).
            if key_data_len > 0:
                key_data = frame[eapol_start + 99: eapol_start + 99 + key_data_len]
                if key_data:
                    pmkid = cls._extract_pmkid_kde(key_data)
                    if pmkid is not None:
                        fields["pmkid"] = pmkid
                    if msg_num == 2:
                        akm = cls._first_rsn_akm(key_data)
                        if akm is not None:
                            fields["akm"] = akm
        return EapolPacket(**base, type="eapol", **fields)

    @staticmethod
    def _classify_eapol_msg(key_info: int, key_data_len: int) -> int:
        """Classify an EAPOL-Key frame as M1/M2/M3/M4 of the 4-way handshake.

        Returns 1-4, or 0 if the frame doesn't fit any of the four canonical
        roles (e.g. group rekey, malformed flags).

        M1: ACK=1, MIC=0, INSTALL=0
        M2: ACK=0, MIC=1, INSTALL=0, key data present (RSN IE)
        M3: ACK=1, MIC=1, INSTALL=1
        M4: ACK=0, MIC=1, INSTALL=0, key data empty
        """
        # 802.11i Key Info (16-bit BE): bit 6 = INSTALL, bit 7 = ACK, bit 8 = MIC.
        install = bool(key_info & 0x0040)
        ack = bool(key_info & 0x0080)
        mic = bool(key_info & 0x0100)

        if ack and not mic and not install:
            return 1
        if ack and mic and install:
            return 3
        if not ack and mic and not install:
            # M2 vs M4 disambiguated by Key Data presence: M2 carries the
            # supplicant's RSN IE, M4 carries nothing.
            return 2 if key_data_len > 0 else 4
        return 0

    @staticmethod
    def _extract_pmkid_kde(key_data: bytes) -> Optional[bytes]:
        """Walk the EAPOL Key Data for a PMKID KDE; return the 16-byte
        PMKID if present, else None.

        Key Data is a stream of KDEs (vendor-specific IE format):
            Type (1B) | Length (1B) | Value (Length B)
        For a PMKID KDE: Type=0xDD, Length>=20, Value=OUI(3)+DataType(1)+PMKID(16).
        """
        i = 0
        n = len(key_data)
        while i + 2 <= n:
            kde_type = key_data[i]
            kde_len = key_data[i + 1]
            value_start = i + 2
            value_end = value_start + kde_len
            if value_end > n:
                return None
            if kde_type == 0xDD and kde_len >= 4 + 16:
                if (
                    key_data[value_start : value_start + 3] == b"\x00\x0f\xac"  # IEEE 802.11 OUI
                    and key_data[value_start + 3] == 0x04  # PMKID KDE data type
                ):
                    pmkid = bytes(key_data[value_start + 4 : value_start + 4 + 16])
                    # Some APs include a PMKID KDE with all-zero bytes as a
                    # placeholder. Treat as "no PMKID", uncrackable anyway.
                    if pmkid != b"\x00" * 16:
                        return pmkid
            i = value_end
        return None

    @classmethod
    def _first_rsn_akm(cls, data: bytes, start: int = 0) -> Optional[int]:
        """First AKM suite (00-0F-AC:N) in the RSN IE (tag 48) within the element/KDE
        list, walked from ``start``, or None (the single suite the supplicant selected).
        """
        i = start
        n = len(data)
        while i + 2 <= n:
            tag = data[i]
            length = data[i + 1]
            value_start = i + 2
            value_end = value_start + length
            if value_end > n:
                return None
            if tag == 48:  # RSN IE (element id 48)
                rsn = cls._parse_rsn_ie(data[value_start:value_end])
                if rsn and rsn["akm_suites"]:
                    return rsn["akm_suites"][0]
                return None
            i = value_end
        return None

    @classmethod
    def _is_valid_frame(cls, frame: bytes) -> bool:
        """Cheap structural gate before parsing: length, protocol version, mgmt IE
        ordering (tag 0 SSID first, tag 1 rates), and a data-frame address noise filter.
        Only MGMT and DATA can pass; CTRL / reserved are rejected here.
        """
        if len(frame) < 24:
            return False
        fc0 = frame[0]

        # Protocol version must be 0
        if (fc0 & 0x03) != 0:
            return False

        ftype = (fc0 & 0x0C) >> 2
        subtype = (fc0 & 0xF0) >> 4

        if ftype == cls.TYPE_MGMT:
            # Enforce Strict Tag Ordering for Mgmt Frames
            if subtype in (cls.SUBTYPE_BEACON, cls.SUBTYPE_PROBE_RESP):
                ptr = 36
            elif subtype == cls.SUBTYPE_PROBE_REQ:
                ptr = 24
            elif subtype == cls.SUBTYPE_DEAUTH:
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

        if ftype == cls.TYPE_DATA:
            # Sanity-check addresses to filter random USB noise from real frames.
            addr2 = frame[10:16]
            addr3 = frame[16:22]

            # addr2 is always the transmitter (SA), never legitimately
            # broadcast or zero, so it's a good noise filter.
            if addr2 == b'\x00\x00\x00\x00\x00\x00' or addr2 == b'\xff\xff\xff\xff\xff\xff':
                return False
            # addr3 is the DA on a ToDS frame, and a BROADCAST DA is exactly what a (WEP) ARP
            # request carries, so reject only all-zeros here, NOT broadcast.
            if addr3 == b'\x00\x00\x00\x00\x00\x00':
                return False

            return True

        return False

    @staticmethod
    def _wps_version2(vext: bytes) -> Optional[int]:
        """Pull the WPS 2.0 Version2 value from a WPS Vendor Extension
        attribute. The value is the WFA vendor id (00:37:2A) followed by
        1-byte-id / 1-byte-len subelements; Version2 is subelement 0x00.
        """
        if len(vext) < 3 or vext[:3] != b"\x00\x37\x2a":
            return None
        j = 3
        while j + 2 <= len(vext):
            sub_id = vext[j]
            sub_len = vext[j + 1]
            j += 2
            if j + sub_len > len(vext):
                break
            if sub_id == 0x00 and sub_len >= 1:   # Version2
                return vext[j]
            j += sub_len
        return None

    @classmethod
    def _parse_wps_ie(cls, data: bytes) -> Dict[str, Any]:
        """Walk the WPS IE's nested big-endian TLVs (``data`` = bytes after
        the OUI + OUI-type) and surface the attacker-relevant subset.

        Each TLV is a 2-byte attribute id, 2-byte length, then value.
        Missing attributes leave their fields at the model defaults.
        """
        # WPS attribute IDs (big-endian), WSC spec §12.
        ATTR_AP_SETUP_LOCKED, ATTR_STATE, ATTR_CONFIG_METHODS = 0x1057, 0x1044, 0x1008
        ATTR_DEVICE_PASSWORD_ID, ATTR_SELECTED_REGISTRAR = 0x1012, 0x1041
        ATTR_VERSION, ATTR_VENDOR_EXTENSION = 0x104A, 0x1049
        out: Dict[str, Any] = {"wps": True}
        version1 = False
        version2 = 0
        i, n = 0, len(data)
        while i + 4 <= n:
            attr = (data[i] << 8) | data[i + 1]
            ln = (data[i + 2] << 8) | data[i + 3]
            i += 4
            if i + ln > n:
                break
            val = data[i:i + ln]
            i += ln
            if attr == ATTR_AP_SETUP_LOCKED and ln >= 1:
                out["wps_locked"] = val[0] == 0x01
            elif attr == ATTR_STATE and ln >= 1:
                out["wps_state"] = val[0]          # 1=unconfigured, 2=configured
            elif attr == ATTR_CONFIG_METHODS and ln >= 2:
                out["wps_config_methods"] = (val[0] << 8) | val[1]
            elif attr == ATTR_DEVICE_PASSWORD_ID and ln >= 2:
                out["wps_device_password_id"] = (val[0] << 8) | val[1]
            elif attr == ATTR_SELECTED_REGISTRAR and ln >= 1:
                out["wps_selected_registrar"] = val[0] == 0x01
            elif attr == ATTR_VERSION and ln >= 1:
                version1 = True
            elif attr == ATTR_VENDOR_EXTENSION:
                v2 = cls._wps_version2(val)
                if v2 is not None:
                    version2 = v2
        if version2 >= 0x20:
            out["wps_version"] = "2.0"
        elif version1:
            out["wps_version"] = "1.0"
        return out

    @classmethod
    def _parse_tags(cls, frame: bytes, subtype: int) -> Optional[Dict[str, Any]]:
        """Parse a management frame's Information Elements into a dict (ssid, channel,
        encryption, …), or None if the frame is corrupt.
        """
        parsed = {}
        if subtype in (cls.SUBTYPE_BEACON, cls.SUBTYPE_PROBE_RESP):
            ptr = 36 # Skip 24-byte HDR + 12-byte Fixed Params
        elif subtype == cls.SUBTYPE_PROBE_REQ:
            ptr = 24 # 24-byte HDR + 0-byte Fixed Params
        elif subtype == cls.SUBTYPE_ASSOC_REQ:
            ptr = 28 # 24-byte HDR + Capability(2) + Listen Interval(2)
        elif subtype == cls.SUBTYPE_REASSOC_REQ:
            ptr = 34 # assoc offset + Current AP Address(6)
        else:
            return parsed

        if len(frame) < ptr + 2:
            return None
        
        # Strict validation: The first tag MUST be Tag 0 (SSID)
        if frame[ptr] != 0:
            return None
        
        has_wpa = False
        has_rsn = False
        has_wpa3 = False
        transition_mode = False
        pmf_capable = False
        pmf_required = False
        beacon_protection = False
        pairwise_cipher: Optional[str] = None
        akms: List[str] = []
        akm_suites: List[int] = []
        channel_ds: Optional[int] = None
        channel_ht: Optional[int] = None
        channel_vht: Optional[int] = None

        # Per 802.11 the SSID IE is mandatory and FIRST. A later tag_id=0 is a malformed
        # frame or the walker straying into trailing bytes (unstripped metadata, padding),
        # so honor only the first occurrence.
        seen_ssid = False

        while ptr + 2 <= len(frame):
            tag_id = frame[ptr]
            tag_len = frame[ptr + 1]

            tag_start = ptr + 2
            tag_end = tag_start + tag_len
            if tag_end > len(frame):
                break

            tag_data = frame[tag_start : tag_end]

            if tag_id == 0 and not seen_ssid: # SSID (only the first)
                seen_ssid = True
                if tag_len == 0:
                    parsed["ssid"] = "<hidden>"
                elif tag_len <= 32:
                    # Validate against completely corrupted text
                    if any(b < 0x20 and b not in (0x09, 0x0a, 0x0d) for b in tag_data):
                        return None # Corrupt frame masquerading as valid
                    parsed["ssid"] = tag_data.decode('utf-8', errors='ignore')
            elif tag_id == 3: # DS Parameter Set (Channel)
                if tag_len == 1:
                    channel_ds = tag_data[0]
            elif tag_id == 61: # HT Operation: primary channel = first byte
                if tag_len >= 1:
                    channel_ht = tag_data[0]
            elif tag_id == 192: # VHT Operation: center freq seg 0 at byte 1
                if tag_len >= 2:
                    channel_vht = tag_data[1]
            elif tag_id == 127: # Extended Capabilities: bit 84 = Beacon Protection Enabled
                if tag_len >= 11:
                    beacon_protection = bool(tag_data[10] & 0x10)   # bit 84 = octet 10, bit 4
            elif tag_id == 48: # RSN (WPA2/WPA3)
                has_rsn = True
                # Preserve the raw IE bytes (with tag header) so the PMKID
                # harvester can echo the AP's exact RSN config in Assoc Req.
                parsed["rsn_ie_raw"] = bytes(frame[ptr : tag_end])
                rsn = cls._parse_rsn_ie(tag_data)
                if rsn is not None:
                    pairwise_cipher = rsn["pairwise"]
                    akms = rsn["akms"]
                    akm_suites = rsn["akm_suites"]
                    pmf_capable = rsn["pmf_capable"]
                    pmf_required = rsn["pmf_required"]
                    # SAE-family => WPA3; SAE + a PSK-family suite => transition.
                    # Suite-number based so WPA3-H2E (SAE-EXT-KEY, 24) is caught.
                    has_wpa3 = bool(cls._SAE_SUITES.intersection(akm_suites))
                    transition_mode = has_wpa3 and bool(
                        cls._PSK_SUITES.intersection(akm_suites)
                    )
            elif tag_id == 221: # Vendor Specific
                if tag_len >= 4:
                    oui = tag_data[:3]
                    oui_type = tag_data[3]
                    if oui == b'\x00\x50\xf2':
                        if oui_type == 1: # WPA
                            has_wpa = True
                        elif oui_type == 4: # WPS
                            # tag_data = OUI(3) + type(1) + WPS TLVs.
                            parsed.update(
                                cls._parse_wps_ie(tag_data[4:])
                            )

            ptr = tag_end

        # Channel preference: DS Param (tag 3, 2.4 GHz authoritative) → HT Op (tag 61, the
        # only cross-band source; 5 GHz often omits DS per 802.11-2020 9.4.2.3) → VHT Op
        # (tag 192, last resort). Caller falls back to its tuned channel if none present.
        if channel_ds is not None:
            parsed["channel"] = channel_ds
        elif channel_ht is not None:
            parsed["channel"] = channel_ht
        elif channel_vht is not None:
            parsed["channel"] = channel_vht

        parsed["wpa3"] = has_wpa3
        parsed["transition_mode"] = transition_mode
        parsed["pmf_capable"] = pmf_capable
        parsed["pmf_required"] = pmf_required
        parsed["beacon_protection"] = beacon_protection
        parsed["pairwise_cipher"] = pairwise_cipher
        parsed["akms"] = akms
        parsed["akm_suites"] = akm_suites
        parsed["encryption"] = cls._format_encryption_label(
            frame=frame,
            has_rsn=has_rsn,
            has_wpa=has_wpa,
            akms=akms,
            pairwise_cipher=pairwise_cipher,
        )
        return parsed

    # ---- RSN IE helpers -----------------------------------------------------

    # Suite-OUI prefix shared by every IEEE-standard cipher + AKM suite.
    _SUITE_OUI = b"\x00\x0f\xac"

    _CIPHER_NAMES = {
        0x01: "WEP-40",
        0x02: "TKIP",
        0x04: "CCMP",
        0x05: "WEP-104",
        0x06: "BIP-CMAC-128",
        0x08: "GCMP-128",
        0x09: "GCMP-256",
        0x0A: "CCMP-256",
    }
    _AKM_NAMES = {
        0x01: "EAP",      # 802.1X (Enterprise)
        0x02: "PSK",
        0x03: "FT-EAP",
        0x04: "FT-PSK",
        0x05: "EAP-SHA256",
        0x06: "PSK-SHA256",
        0x08: "SAE",      # WPA3
        0x09: "FT-SAE",
        0x0B: "EAP-SUITE-B",
        0x0C: "EAP-SUITE-B-192",
        0x0D: "FT-EAP-SHA384",
        0x12: "OWE",      # Enhanced Open
        0x13: "FT-PSK-SHA384",
        0x14: "PSK-SHA384",
        0x18: "SAE-EXT-KEY",      # WPA3 H2E (group-dependent hash)
        0x19: "FT-SAE-EXT-KEY",
    }

    # AKM suite numbers (00-0F-AC:N) grouped for WPA3 detection: any SAE-family
    # suite => WPA3; SAE alongside a PSK-family suite => WPA2/WPA3 transition.
    # Mirrors the crackability split in crack.handshake (duplicated here to
    # avoid a wlan->engine import), keep the two in sync.
    _SAE_SUITES = frozenset({0x08, 0x09, 0x18, 0x19})
    _PSK_SUITES = frozenset({0x02, 0x04, 0x06, 0x13, 0x14})
    # TODO: FT-PSK family (suites 4 & 19) is "crackable" but the FT key hierarchy
    #       (PMK-R0 → PMK-R1 → PTK) is more involved than plain PSK.

    @classmethod
    def _suite_name(cls, suite: bytes, table: Dict[int, str]) -> Optional[str]:
        if len(suite) != 4 or suite[:3] != cls._SUITE_OUI:
            return None
        return table.get(suite[3])

    @classmethod
    def _parse_rsn_ie(cls, tag_data: bytes) -> Optional[Dict[str, Any]]:
        """Parse the RSN IE body (tag 48 contents, sans the 2-byte header).

        Returns dict with pairwise (str|None), akms (list[str]), pmf_capable,
        pmf_required, or None if the IE is malformed.

        Field layout (per IEEE 802.11-2020 § 9.4.2.24):
            Version (2 B LE) | Group Cipher Suite (4 B) |
            Pairwise Suite Count (2 B LE) | Pairwise Suite List (4 B × N) |
            AKM Suite Count    (2 B LE) | AKM Suite List    (4 B × N) |
            RSN Capabilities   (2 B LE) | [optional PMKID list, GMCS, ...]
        """
        try:
            n = len(tag_data)
            # Need at least version (2) + group cipher (4) + 2 size fields (2+2) = 10.
            if n < 10:
                return None
            p = 6  # skip version + group cipher
            pairwise_count = int.from_bytes(tag_data[p:p+2], "little")
            p += 2
            if p + 4 * pairwise_count > n:
                return None
            pairwise: Optional[str] = None
            for i in range(pairwise_count):
                name = cls._suite_name(tag_data[p:p+4], cls._CIPHER_NAMES)
                # Stick with the first listed pairwise cipher (the AP's
                # preferred one). Some APs list TKIP+CCMP for compatibility;
                # CCMP is conventionally listed first.
                if pairwise is None and name is not None:
                    pairwise = name
                p += 4
            if p + 2 > n:
                return None
            akm_count = int.from_bytes(tag_data[p:p+2], "little")
            p += 2
            if p + 4 * akm_count > n:
                return None
            akms: List[str] = []
            akm_suites: List[int] = []
            for _ in range(akm_count):
                suite = tag_data[p:p + 4]
                p += 4
                if len(suite) != 4 or suite[:3] != cls._SUITE_OUI:
                    continue
                sid = suite[3]
                if sid not in akm_suites:
                    akm_suites.append(sid)   # raw 00-0F-AC:N, drives crackability
                name = cls._AKM_NAMES.get(sid)
                if name is not None and name not in akms:
                    akms.append(name)
            pmf_capable = False
            pmf_required = False
            if p + 2 <= n:
                rsn_caps = int.from_bytes(tag_data[p:p+2], "little")
                pmf_capable = bool(rsn_caps & 0x0080)  # Bit 7 (MFPC)
                pmf_required = bool(rsn_caps & 0x0040)  # Bit 6 (MFPR)
            return {
                "pairwise": pairwise,
                "akms": akms,
                "akm_suites": akm_suites,
                "pmf_capable": pmf_capable,
                "pmf_required": pmf_required,
            }
        except Exception:
            return None

    @staticmethod
    def _format_encryption_label(
        *,
        frame: bytes,
        has_rsn: bool,
        has_wpa: bool,
        akms: List[str],
        pairwise_cipher: Optional[str],
    ) -> str:
        """Build an airodump-style encryption label.

        Examples:
            "WPA2-PSK-CCMP"
            "WPA3-SAE-CCMP"
            "WPA2/WPA3-PSK+SAE-CCMP"   (transition mode)
            "WPA2-EAP-CCMP"
            "WPA-PSK"                  (legacy WPA1 vendor IE only)
            "OPEN" / "WEP"
        """
        if has_rsn:
            # Every SAE-family name (SAE, FT-SAE, SAE-EXT-KEY, FT-SAE-EXT-KEY)
            # contains "SAE", so this also catches WPA3-H2E, matching the
            # suite-number `wpa3` flag so label and flag never disagree.
            has_sae = any("SAE" in a for a in akms)
            has_psk = "PSK" in akms or "PSK-SHA256" in akms
            has_eap = any(a.startswith("EAP") or a == "FT-EAP" for a in akms)
            has_owe = "OWE" in akms

            if has_sae and has_psk:
                wpa_tag = "WPA2/WPA3"
                akm_tag = "PSK+SAE"
            elif has_sae:
                wpa_tag = "WPA3"
                akm_tag = "SAE"
            elif has_owe:
                wpa_tag = "OWE"
                akm_tag = None
            else:
                wpa_tag = "WPA2"
                if has_eap and has_psk:
                    akm_tag = "PSK+EAP"
                elif has_eap:
                    akm_tag = "EAP"
                elif has_psk:
                    akm_tag = "PSK"
                else:
                    # Unknown AKM(s): fall back to listing them.
                    akm_tag = "+".join(akms) if akms else None

            parts = [wpa_tag]
            if akm_tag:
                parts.append(akm_tag)
            if pairwise_cipher:
                parts.append(pairwise_cipher)
            return "-".join(parts)

        if has_wpa:
            # Legacy WPA1 vendor IE: TKIP is the universal assumption.
            return "WPA-PSK-TKIP"

        if len(frame) >= 36:
            cap_info = int.from_bytes(frame[34:36], byteorder='little')
            if cap_info & 0x0010:  # Privacy bit
                return "WEP"
        return "OPEN"