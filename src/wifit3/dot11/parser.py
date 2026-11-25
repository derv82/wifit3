"""Pure-Python 802.11 frame parsing: ``WlanFrameParser`` builds the typed ``Packet`` hierarchy
(see ``dot11.packet``) from raw MPDU bytes.
"""
import struct
from typing import Optional, Dict, Any, Tuple

from wifit3.dot11.eap import (
    EAP_FAILURE,
    EAP_REQUEST,
    EAP_RESPONSE,
    EAP_SUCCESS,
    EAP_TYPE_EXPANDED,
)
from wifit3.dot11.eapol import EAPOL_TYPE_EAP, EAPOL_TYPE_KEY, LLC_SNAP_EAPOL
from wifit3.dot11.ie import iter_information_elements, parse_elements, rsn_akm_selection
from wifit3.dot11.mac import mac_to_str
from wifit3.dot11.wsc.messages import (
    WFA_VENDOR_ID,
    WFA_VENDOR_TYPE_SIMPLECONFIG,
)
from wifit3.dot11.packet import (
    Packet, BeaconPacket, EapolPacket, EapPacket, WepDataPacket, AssocRequestPacket,
    AuthPacket, AssocRespPacket, DeauthPacket, ProbeReqPacket,
)
from wifit3.dot11.rsn import (
    RSN_OUI,
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
    SUBTYPE_REASSOC_RESP = 0x03
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
                "ssid_bytes": tags.get("ssid_bytes"),
                "encryption": tags.get("encryption", "OPEN"),
                "wpa3": tags.get("wpa3", False),
                "transition_mode": tags.get("transition_mode", False),
                "pmf_capable": tags.get("pmf_capable", False),
                "pmf_required": tags.get("pmf_required", False),
                "beacon_protection": tags.get("beacon_protection", False),
                "pairwise_cipher": tags.get("pairwise_cipher"),
                "akms": tags.get("akms", []),
                "akm_suites": tags.get("akm_suites", []),
                "rsn_valid": tags.get("rsn_valid", False),
                "vendor_akm_offered": tags.get("vendor_akm_offered", False),
            }
            # Copy channel / rsn_ie_raw / WPS only when the walker found them, so a missing
            # value keeps the field default.
            for key in ("channel", "rsn_ie_raw", "wps", "wps_locked", "wps_version",
                        "wps_state", "wps_config_methods", "wps_device_password_id",
                        "wps_selected_registrar", "wsc_manufacturer", "wsc_model_name",
                        "wsc_model_number", "wsc_device_name", "wsc_device_type", "chipset"):
                if key in tags:
                    fields[key] = tags[key]
            return BeaconPacket(**base, **fields)

        if subtype == cls.SUBTYPE_PROBE_REQ:
            tags = cls._parse_tags(frame, subtype)
            if tags is None:
                return None
            return ProbeReqPacket(**base, type="probe_req", ssid=tags.get("ssid"),
                                  ssid_bytes=tags.get("ssid_bytes"))

        if subtype == cls.SUBTYPE_ASSOC_REQ:
            tags = cls._parse_tags(frame, subtype)
            if tags is None:
                return None
            # Client's selected AKM from the RSN IE (24 hdr + cap + listen = 28).
            return AssocRequestPacket(
                **base, type="assoc_req", ssid=tags.get("ssid"), ssid_bytes=tags.get("ssid_bytes"),
                assoc_akm=rsn_akm_selection(frame, 28)[0])

        if subtype == cls.SUBTYPE_REASSOC_REQ:
            tags = cls._parse_tags(frame, subtype)
            if tags is None:
                return None
            # 34 = assoc's 28 + 6-byte Current AP Address.
            return AssocRequestPacket(
                **base, type="reassoc_req", ssid=tags.get("ssid"), ssid_bytes=tags.get("ssid_bytes"),
                assoc_akm=rsn_akm_selection(frame, 34)[0])

        if subtype in (cls.SUBTYPE_ASSOC_RESP, cls.SUBTYPE_REASSOC_RESP):
            # Body: Capability(2) + Status(2) + AID(2); status at offset 26.
            status = struct.unpack("<H", frame[26:28])[0] if len(frame) >= 28 else None
            label = "assoc_resp" if subtype == cls.SUBTYPE_ASSOC_RESP else "reassoc_resp"
            return AssocRespPacket(**base, type=label, status=status)
        if subtype == cls.SUBTYPE_AUTH:
            # Body: Algorithm(2) + Seq(2) + Status(2); status at offset 28.
            status = struct.unpack("<H", frame[28:30])[0] if len(frame) >= 30 else None
            return AuthPacket(**base, type=f"mgmt_{subtype}", status=status)
        if subtype in (cls.SUBTYPE_DEAUTH, cls.SUBTYPE_DISASSOC):
            # Reason code is the 2-byte body right after the 24-byte header.
            reason = struct.unpack("<H", frame[24:26])[0] if len(frame) >= 26 else None
            label = "deauth" if subtype == cls.SUBTYPE_DEAUTH else "disassoc"
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
                    # First 16 ciphertext bytes: the PTW keystream (XOR the known ARP plaintext).
                    cipher=bytes(frame[cipher_start : cipher_start + 16]))
            return Packet(**base, type="data")

        eapol = cls._parse_eapol(frame, header_len, base)
        return eapol if eapol is not None else Packet(**base, type="data")

    @classmethod
    def _parse_eapol(cls, frame: bytes, header_len: int,
                     base: Dict[str, Any]) -> Optional["Packet"]:
        """Build the Packet for an 802.1X/EAPOL data frame, or None without EAPOL LLC/SNAP:
        EAPOL-Key or EAP-Expanded (WSC) -> EapolPacket; a genuine EAP method -> EapPacket."""
        if len(frame) < header_len + 8:
            return None
        # DMA pads the 802.11 header for 4-byte alignment, so slide a window to find the
        # LLC/SNAP + EAPOL ethertype signature regardless of padding.
        sig_idx = frame[header_len : header_len + 16].find(LLC_SNAP_EAPOL)
        if sig_idx == -1:
            return None

        eapol_start = header_len + sig_idx + 8
        if len(frame) < eapol_start + 2:
            return EapolPacket(**base, type="eapol")
        # Split EAP (802.1X Type 0, at offset +1) from EAPOL-Key. WSC keeps the legacy
        # EapolPacket path; malformed EAP is discarded as protocol data.
        if frame[eapol_start + 1] == EAPOL_TYPE_EAP:
            if len(frame) < eapol_start + 4:
                return None
            body_len = int.from_bytes(frame[eapol_start + 2:eapol_start + 4], "big")
            body_start = eapol_start + 4
            body_end = body_start + body_len
            if body_end > len(frame):
                return None
            return cls._parse_eap(frame[body_start:body_end], base)

        # Key-descriptor offsets from eapol_start: 1=802.1X type (3=EAPOL-Key), 5=Key Info
        # (2B BE), 9=Replay Counter (8B), 17=Key Nonce (32B), 81=Key MIC (16B), 97=Key Data
        # Length (2B BE), 99=Key Data.
        fields: Dict[str, Any] = {}
        if len(frame) >= eapol_start + 99 and frame[eapol_start + 1] == EAPOL_TYPE_KEY:  # EAPOL-Key
            body_len = int.from_bytes(frame[eapol_start + 2: eapol_start + 4], "big")
            pdu_end = eapol_start + 4 + body_len
            key_data_len = struct.unpack(">H", frame[eapol_start + 97: eapol_start + 99])[0]
            structured_pdu_end = eapol_start + 99 + key_data_len
            if pdu_end >= eapol_start + 99 and structured_pdu_end <= pdu_end:
                readable_end = min(pdu_end, len(frame))
                key_info = struct.unpack(">H", frame[eapol_start + 5: eapol_start + 7])[0]
                msg_num = cls._classify_eapol_msg(key_info, key_data_len)
                fields.update(
                    key_info=key_info,
                    replay_counter=frame[eapol_start + 9: eapol_start + 17],
                    nonce=frame[eapol_start + 17: eapol_start + 49],
                    mic=frame[eapol_start + 81: eapol_start + 97],
                    msg_num=msg_num,
                )
                if pdu_end == structured_pdu_end and len(frame) >= structured_pdu_end:
                    fields["payload"] = bytes(frame[eapol_start: structured_pdu_end])
                # PMKID KDE (M1) and the client's negotiated AKM (M2's cleartext RSN IE) both sit
                # at the START of Key Data, so a truncated tail still yields them (seen on
                # mt76x0u / rt5572).
                if key_data_len > 0:
                    key_data = frame[eapol_start + 99: min(structured_pdu_end, readable_end)]
                    if key_data:
                        pmkid = cls._extract_pmkid_kde(key_data)
                        if pmkid is not None:
                            fields["pmkid"] = pmkid
                        if msg_num == 2:
                            akm, rejected = rsn_akm_selection(key_data)
                            fields.update(akm=akm, akm_selection_rejected=rejected)
                            mdid, r0khid, r1khid = cls._ft_identifiers(key_data)
                            fields.update(mdid=mdid, r0khid=r0khid, r1khid=r1khid)
        return EapolPacket(**base, type="eapol", **fields)

    @classmethod
    def _parse_eap(cls, eapol_body: bytes,
                   base: Dict[str, Any]) -> Optional["Packet"]:
        """Classify a length-bounded EAP body without retaining identity or credential data.
        WSC returns its legacy EapolPacket; malformed EAP returns None."""
        if len(eapol_body) < 4:
            return None
        code = eapol_body[0]
        eap_len = int.from_bytes(eapol_body[2:4], "big")
        if code not in (EAP_REQUEST, EAP_RESPONSE, EAP_SUCCESS, EAP_FAILURE):
            return None
        if eap_len < 4 or eap_len > len(eapol_body):
            return None

        # RFC 3748: octets beyond the inner EAP Length are link-layer padding.
        eap = eapol_body[:eap_len]
        if code in (EAP_SUCCESS, EAP_FAILURE):
            if eap_len != 4:
                return None
            return EapPacket(**base, type="eap")
        if eap_len < 5:
            return None

        eap_type = eap[4]
        if eap_type == EAP_TYPE_EXPANDED:
            if eap_len < 12:
                return None
            if eap[5:8] == WFA_VENDOR_ID and eap[8:12] == WFA_VENDOR_TYPE_SIMPLECONFIG:
                # WSC adds an opcode and flags after the generic Expanded header.
                if eap_len < 14:
                    return None
                return EapolPacket(**base, type="eapol")
        return EapPacket(**base, type="eap", eap_type=eap_type)

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
        for kde_type, val, _raw in iter_information_elements(key_data):
            if kde_type == 0xDD and len(val) >= 20:
                if (
                    val[:3] == RSN_OUI
                    and val[3] == 0x04  # PMKID KDE data type
                ):
                    pmkid = bytes(val[4:20])
                    # Some APs include a PMKID KDE with all-zero bytes as a
                    # placeholder. Treat as "no PMKID", uncrackable anyway.
                    if pmkid != b"\x00" * 16:
                        return pmkid
        return None

    @staticmethod
    def _ft_identifiers(key_data: bytes) -> Tuple[bytes, bytes, bytes]:
        """(MDID, R0KH-ID, R1KH-ID) from the Mobility Domain element (54) and the FT element
        (55: 82-byte fixed part, then subelements 1 = R1KH-ID, 3 = R0KH-ID); b"" when absent."""
        mdid = r0khid = r1khid = b""
        for tag_id, value, _raw in iter_information_elements(key_data):
            if tag_id == 54 and len(value) == 3:
                mdid = bytes(value[:2])
            elif tag_id == 55 and len(value) >= 82:
                offset = 82
                while offset + 2 <= len(value):
                    subelement_id = value[offset]
                    end = offset + 2 + value[offset + 1]
                    if end > len(value):
                        break
                    if subelement_id == 1:
                        r1khid = bytes(value[offset + 2:end])
                    elif subelement_id == 3:
                        r0khid = bytes(value[offset + 2:end])
                    offset = end
        return mdid, r0khid, r1khid

    @classmethod
    def _ie_offset(cls, subtype: int) -> Optional[int]:
        """Byte offset where a mgmt frame's Information Elements begin (past the header +
        fixed params), or None for subtypes that carry no IE region."""
        if subtype in (cls.SUBTYPE_BEACON, cls.SUBTYPE_PROBE_RESP):
            return 36  # 24-byte HDR + 12-byte Fixed Params
        if subtype == cls.SUBTYPE_PROBE_REQ:
            return 24  # 24-byte HDR + 0-byte Fixed Params
        if subtype == cls.SUBTYPE_ASSOC_REQ:
            return 28  # 24-byte HDR + Capability(2) + Listen Interval(2)
        if subtype == cls.SUBTYPE_REASSOC_REQ:
            return 34  # assoc offset + Current AP Address(6)
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
            if subtype == cls.SUBTYPE_DEAUTH:
                return len(frame) >= 26
            # (Re)Assoc Req carry IEs but are not offset-gated here; only
            # Beacon / Probe Resp / Probe Req get the SSID-first check.
            if subtype in (cls.SUBTYPE_ASSOC_REQ, cls.SUBTYPE_REASSOC_REQ):
                return True
            ptr = cls._ie_offset(subtype)
            if ptr is None:
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

    @classmethod
    def _parse_tags(cls, frame: bytes, subtype: int) -> Optional[Dict[str, Any]]:
        """Parse a management frame's Information Elements into a dict (ssid, channel,
        encryption, …), or None if the frame is corrupt.
        """
        ptr = cls._ie_offset(subtype)
        if ptr is None:
            return {}
        if len(frame) < ptr + 2:
            return None
        # Strict validation: The first tag MUST be Tag 0 (SSID)
        if frame[ptr] != 0:
            return None
        privacy = len(frame) >= 36 and bool(int.from_bytes(frame[34:36], "little") & 0x0010)
        return parse_elements(frame[ptr:], privacy)
