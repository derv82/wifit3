"""Pure-Python 802.11 frame parsing: ``WlanFrameParser`` builds the typed ``Packet`` hierarchy
(see ``dot11.packet``) from raw MPDU bytes.
"""
import struct
from typing import Optional, List, Dict, Any, Tuple

from wifit3.dot11.eap import (
    EAP_FAILURE,
    EAP_REQUEST,
    EAP_RESPONSE,
    EAP_SUCCESS,
    EAP_TYPE_EXPANDED,
)
from wifit3.dot11.eapol import EAPOL_TYPE_EAP, EAPOL_TYPE_KEY, LLC_SNAP_EAPOL
from wifit3.dot11.ie import iter_information_elements
from wifit3.dot11.mac import mac_to_str
from wifit3.dot11.wsc.identity import device_type_label, wps_text
from wifit3.dot11.wsc.messages import (
    iter_wsc_tlvs,
    ATTR_AP_SETUP_LOCKED,
    ATTR_CONFIG_METHODS,
    ATTR_MANUFACTURER,
    ATTR_MODEL_NAME,
    ATTR_MODEL_NUMBER,
    ATTR_PRIMARY_DEV_TYPE,
    ATTR_SELECTED_REGISTRAR,
    ATTR_VENDOR_EXTENSION,
    ATTR_VERSION,
    ATTR_WPS_STATE as ATTR_STATE,
    ATTR_DEV_PASSWORD_ID as ATTR_DEVICE_PASSWORD_ID,
    ATTR_DEV_NAME as ATTR_DEVICE_NAME,
    WFA_VENDOR_ID,
    WFA_VENDOR_TYPE_SIMPLECONFIG,
)
from wifit3.dot11.packet import (
    Packet, BeaconPacket, EapolPacket, EapPacket, WepDataPacket, AssocRequestPacket,
    AuthPacket, AssocRespPacket, DeauthPacket, ProbeReqPacket,
)
from wifit3.dot11.rsn import (
    AKM_802_1X,
    AKM_802_1X_SHA256,
    AKM_802_1X_SHA384,
    AKM_802_1X_SUITE_B,
    AKM_802_1X_SUITE_B_192,
    AKM_FILS_SHA256,
    AKM_FILS_SHA384,
    AKM_FT_802_1X,
    AKM_FT_802_1X_SHA384,
    AKM_FT_802_1X_SUITE_B_192,
    AKM_FT_FILS_SHA256,
    AKM_FT_FILS_SHA384,
    AKM_FT_PSK,
    AKM_FT_PSK_SHA384,
    AKM_FT_SAE,
    AKM_FT_SAE_EXT_KEY,
    AKM_OWE,
    AKM_PSK,
    AKM_PSK_SHA256,
    AKM_PSK_SHA384,
    AKM_SAE,
    AKM_SAE_EXT_KEY,
    AKM_SELECTOR_DPP,
    AKM_SELECTOR_OSEN,
    CIPHER_AES_128_CMAC,
    CIPHER_BIP_GMAC_256,
    CIPHER_CCMP,
    CIPHER_CCMP_256,
    CIPHER_GCMP,
    CIPHER_GCMP_256,
    CIPHER_TKIP,
    CIPHER_WEP40,
    CIPHER_WEP104,
    ENTERPRISE_AKM_SUITES,
    PSK_AKM_SUITES,
    RSN_OUI,
    SAE_AKM_SUITES,
    WPA_OUI,
    RsnElement,
    WpaElement,
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
                        "wsc_model_number", "wsc_device_name", "wsc_device_type"):
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
                assoc_akm=cls._rsn_akm_selection(frame, 28)[0])

        if subtype == cls.SUBTYPE_REASSOC_REQ:
            tags = cls._parse_tags(frame, subtype)
            if tags is None:
                return None
            # 34 = assoc's 28 + 6-byte Current AP Address.
            return AssocRequestPacket(
                **base, type="reassoc_req", ssid=tags.get("ssid"),
                assoc_akm=cls._rsn_akm_selection(frame, 34)[0])

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
                    keyid=(keyid_byte >> 6) & 0x03,
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
                    key_data_len=key_data_len,
                    msg_num=msg_num,
                )
                if pdu_end == structured_pdu_end and len(frame) >= structured_pdu_end:
                    fields["payload"] = bytes(frame[eapol_start: structured_pdu_end])
                # PMKID KDE (M1) and the client's negotiated AKM (M2's cleartext RSN IE) both sit
                # at the START of Key Data, so a truncated tail still yields them (seen on
                # mt76x0u / rt2800usb RT5572).
                if key_data_len > 0:
                    key_data = frame[eapol_start + 99: min(structured_pdu_end, readable_end)]
                    if key_data:
                        pmkid = cls._extract_pmkid_kde(key_data)
                        if pmkid is not None:
                            fields["pmkid"] = pmkid
                        if msg_num == 2:
                            akm, rejected = cls._rsn_akm_selection(key_data)
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
    def _rsn_akm_selection(cls, data: bytes, start: int = 0) -> tuple[Optional[int], bool]:
        """The first RSN IE's AKM when it lists exactly one 00-0F-AC AKM, and whether an RSN IE
        (complete, truncated or malformed) is present without one."""
        end = start
        for tag_id, val, raw in iter_information_elements(data, start=start):
            if tag_id == 48:  # RSN IE (element id 48)
                rsn = cls._parse_rsn_ie(val)
                if rsn is not None and rsn.akm_suite_count == 1 and len(rsn.akm_suites) == 1:
                    return rsn.akm_suites[0], False
                return None, True
            end += len(raw)
        return None, end < len(data) and data[end] == 48

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
        out: Dict[str, Any] = {"wps": True}
        version1 = False
        version2 = 0
        for attr, val in iter_wsc_tlvs(data):
            ln = len(val)
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
            elif attr == ATTR_MANUFACTURER:
                text = cls._wps_text(val)
                if text:
                    out["wsc_manufacturer"] = text
            elif attr == ATTR_MODEL_NAME:
                text = cls._wps_text(val)
                if text:
                    out["wsc_model_name"] = text
            elif attr == ATTR_MODEL_NUMBER:
                text = cls._wps_text(val)
                if text:
                    out["wsc_model_number"] = text
            elif attr == ATTR_DEVICE_NAME:
                text = cls._wps_text(val)
                if text:
                    out["wsc_device_name"] = text
            elif attr == ATTR_PRIMARY_DEV_TYPE:
                label = device_type_label(val)
                if label:
                    out["wsc_device_type"] = label
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

    @staticmethod
    def _wps_text(value: bytes) -> str:
        return wps_text(value)

    @classmethod
    def _parse_tags(cls, frame: bytes, subtype: int) -> Optional[Dict[str, Any]]:
        """Parse a management frame's Information Elements into a dict (ssid, channel,
        encryption, …), or None if the frame is corrupt.
        """
        parsed = {}
        ptr = cls._ie_offset(subtype)
        if ptr is None:
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
        vendor_akm_offered = False
        rsn_element: Optional[RsnElement] = None
        wpa_element: Optional[WpaElement] = None
        channel_ds: Optional[int] = None
        channel_ht: Optional[int] = None
        channel_vht: Optional[int] = None

        # Per 802.11 the SSID IE is mandatory and FIRST. A later tag_id=0 is a malformed
        # frame or the walker straying into trailing bytes (unstripped metadata, padding),
        # so honor only the first occurrence.
        seen_ssid = False
        wps_payloads: List[bytes] = []

        for tag_id, tag_data, raw_elem in iter_information_elements(frame, start=ptr):
            if tag_id == 0 and not seen_ssid: # SSID (only the first)
                seen_ssid = True
                if len(tag_data) == 0:
                    parsed["ssid"] = "<hidden>"
                elif len(tag_data) <= 32:
                    # Validate the DECODED codepoints, not the raw bytes. A byte test cannot
                    # see anything whose UTF-8 encoding is >= 0x20 per byte: C1 controls
                    # (U+0085, U+009B), DEL (U+007F) and bidi overrides (U+202E, which reverse
                    # the rest of the row on screen) all sail through, as do TAB/LF/CR, which
                    # this guard used to whitelist and which shift the table row. isprintable()
                    # rejects all of them while keeping non-ASCII SSIDs intact.
                    ssid_text = tag_data.decode('utf-8', errors='ignore')
                    if not all(ch.isprintable() for ch in ssid_text):
                        return None  # Corrupt frame masquerading as valid
                    parsed["ssid"] = ssid_text
            elif tag_id == 3: # DS Parameter Set (Channel)
                if len(tag_data) == 1:
                    channel_ds = tag_data[0]
            elif tag_id == 61: # HT Operation: primary channel = first byte
                if len(tag_data) >= 1:
                    channel_ht = tag_data[0]
            elif tag_id == 192: # VHT Operation: center freq seg 0 at byte 1
                if len(tag_data) >= 2:
                    channel_vht = tag_data[1]
            elif tag_id == 127: # Extended Capabilities: bit 84 = Beacon Protection Enabled
                if len(tag_data) >= 11:
                    beacon_protection = bool(tag_data[10] & 0x10)   # bit 84 = octet 10, bit 4
            elif tag_id == 48: # RSN (WPA2/WPA3)
                has_rsn = True
                rsn = cls._parse_rsn_ie(tag_data)
                if rsn is not None:
                    parsed["rsn_ie_raw"] = bytes(raw_elem)
                    rsn_element = rsn
                    pairwise_cipher = rsn.pairwise_cipher
                    akms = list(rsn.akms)
                    akm_suites = list(rsn.akm_suites)
                    vendor_akm_offered = rsn.vendor_akm_offered
                    pmf_capable = rsn.pmf_capable
                    pmf_required = rsn.pmf_required
                    # SAE-family => WPA3; SAE + a PSK-family suite => transition.
                    # Suite-number based so WPA3-H2E (SAE-EXT-KEY, 24) is caught.
                    has_wpa3 = bool(SAE_AKM_SUITES.intersection(akm_suites))
                    transition_mode = has_wpa3 and bool(
                        PSK_AKM_SUITES.intersection(akm_suites)
                    )
                elif rsn_element is None and "rsn_ie_raw" not in parsed:
                    parsed["rsn_ie_raw"] = bytes(raw_elem)
            elif tag_id == 221: # Vendor Specific
                if len(tag_data) >= 4:
                    oui = tag_data[:3]
                    oui_type = tag_data[3]
                    if oui == WPA_OUI:
                        if oui_type == 1: # WPA
                            has_wpa = True
                            if wpa_element is None:
                                wpa_element = cls._parse_wpa_ie(tag_data)
                        elif oui_type == 4: # WPS
                            # tag_data = OUI(3) + type(1) + WPS TLVs.
                            wps_payloads.append(tag_data[4:])

        if wps_payloads:
            parsed.update(cls._parse_wps_ie(b"".join(wps_payloads)))

        if rsn_element is None and wpa_element is not None:
            has_rsn = False
            pairwise_cipher = wpa_element.pairwise_cipher
            akms = list(wpa_element.akms)
            akm_suites = list(wpa_element.akm_suites)
            vendor_akm_offered = wpa_element.vendor_akm_offered

        if rsn_element is not None:
            wpa3_enterprise, enterprise_transition = cls._classify_wpa3_enterprise(
                rsn_element, has_wpa
            )
            has_wpa3 = has_wpa3 or wpa3_enterprise
            transition_mode = transition_mode or enterprise_transition

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
        parsed["rsn_valid"] = rsn_element is not None
        parsed["vendor_akm_offered"] = vendor_akm_offered
        parsed["encryption"] = cls._format_encryption_label(
            frame=frame,
            has_rsn=has_rsn,
            has_wpa=has_wpa,
            has_wpa3=has_wpa3,
            transition_mode=transition_mode,
            akms=akms,
            pairwise_cipher=pairwise_cipher,
        )
        return parsed

    # ---- RSN IE helpers -----------------------------------------------------

    _CIPHER_NAMES = {
        CIPHER_WEP40: "WEP-40",
        CIPHER_TKIP: "TKIP",
        CIPHER_CCMP: "CCMP",
        CIPHER_WEP104: "WEP-104",
        CIPHER_AES_128_CMAC: "BIP-CMAC-128",
        CIPHER_GCMP: "GCMP-128",
        CIPHER_GCMP_256: "GCMP-256",
        CIPHER_CCMP_256: "CCMP-256",
    }
    _AKM_NAMES = {
        AKM_802_1X: "EAP",      # 802.1X (Enterprise)
        AKM_PSK: "PSK",
        AKM_FT_802_1X: "FT-EAP",
        AKM_FT_PSK: "FT-PSK",
        AKM_802_1X_SHA256: "EAP-SHA256",
        AKM_PSK_SHA256: "PSK-SHA256",
        AKM_SAE: "SAE",      # WPA3
        AKM_FT_SAE: "FT-SAE",
        AKM_802_1X_SUITE_B: "EAP-SUITE-B",
        AKM_802_1X_SUITE_B_192: "EAP-SUITE-B-192",
        AKM_FT_802_1X_SUITE_B_192: "FT-EAP-SUITE-B-192",
        AKM_FILS_SHA256: "FILS-SHA256",
        AKM_FILS_SHA384: "FILS-SHA384",
        AKM_FT_FILS_SHA256: "FT-FILS-SHA256",
        AKM_FT_FILS_SHA384: "FT-FILS-SHA384",
        AKM_OWE: "OWE",      # Enhanced Open
        AKM_FT_PSK_SHA384: "FT-PSK-SHA384",
        AKM_PSK_SHA384: "PSK-SHA384",
        AKM_FT_802_1X_SHA384: "FT-EAP-SHA384",
        AKM_802_1X_SHA384: "EAP-SHA384",
        AKM_SAE_EXT_KEY: "SAE-EXT-KEY",      # WPA3 H2E (group-dependent hash)
        AKM_FT_SAE_EXT_KEY: "FT-SAE-EXT-KEY",
    }
    _VENDOR_AKM_NAMES = {
        AKM_SELECTOR_OSEN: "OSEN",
        AKM_SELECTOR_DPP: "DPP",
    }

    _WPA_AKM_SUITES = {1: AKM_802_1X, 2: AKM_PSK}   # 00-50-F2:N -> 00-0F-AC:N

    _WPA3_DATA_CIPHERS = frozenset({CIPHER_CCMP, CIPHER_GCMP, CIPHER_GCMP_256, CIPHER_CCMP_256})
    # TODO: FT-PSK family (suites 4 & 19) is "crackable" but the FT key hierarchy
    #       (PMK-R0 → PMK-R1 → PTK) is more involved than plain PSK.

    @classmethod
    def _suite_name(cls, suite: bytes, table: Dict[int, str]) -> Optional[str]:
        if len(suite) != 4 or suite[:3] != RSN_OUI:
            return None
        return table.get(suite[3])

    @classmethod
    def _parse_rsn_ie(cls, tag_data: bytes) -> Optional[RsnElement]:
        """Parse the RSN IE body (tag 48 contents, sans the 2-byte header), or None if
        the IE is malformed.

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
            if int.from_bytes(tag_data[:2], "little") != 1:
                return None
            group_cipher_suite = tag_data[5] if tag_data[2:5] == RSN_OUI else None
            p = 6  # skip version + group cipher
            pairwise_count = int.from_bytes(tag_data[p:p+2], "little")
            p += 2
            if p + 4 * pairwise_count > n:
                return None
            pairwise: Optional[str] = None
            pairwise_suites: List[int] = []
            for i in range(pairwise_count):
                suite = tag_data[p:p + 4]
                name = cls._suite_name(suite, cls._CIPHER_NAMES)
                if suite[:3] == RSN_OUI:
                    pairwise_suites.append(suite[3])
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
            vendor_akm_offered = False
            for _ in range(akm_count):
                suite = tag_data[p:p + 4]
                p += 4
                if len(suite) != 4:
                    continue
                if suite[:3] == RSN_OUI:
                    sid = suite[3]
                    if sid not in akm_suites:
                        akm_suites.append(sid)   # raw 00-0F-AC:N, drives crackability
                    name = cls._AKM_NAMES.get(sid, f"AKM-{suite.hex()}")
                else:
                    vendor_akm_offered = True
                    name = cls._VENDOR_AKM_NAMES.get(bytes(suite), "Vendor")
                if name not in akms:
                    akms.append(name)
            pmf_capable = False
            pmf_required = False
            group_management_cipher_suite: Optional[int] = None
            if p < n:
                if p + 2 > n:
                    return None
                rsn_caps = int.from_bytes(tag_data[p:p+2], "little")
                pmf_capable = bool(rsn_caps & 0x0080)  # Bit 7 (MFPC)
                pmf_required = bool(rsn_caps & 0x0040)  # Bit 6 (MFPR)
                p += 2
                # Optional tail: PMKID Count (2 B) + PMKID List (16 B each) +
                # Group Management Cipher Suite. GMCS is mandatory for WPA3-Ent-192.
                if p < n:
                    if p + 2 > n:
                        return None
                    pmkid_count = int.from_bytes(tag_data[p:p+2], "little")
                    p += 2
                    pmkid_bytes = 16 * pmkid_count
                    if p + pmkid_bytes > n:
                        return None
                    p += pmkid_bytes
                    if p < n and p + 4 > n:
                        return None
                    if p + 4 <= n and tag_data[p:p+3] == RSN_OUI:
                        group_management_cipher_suite = tag_data[p+3]
            return RsnElement(
                group_cipher_suite=group_cipher_suite,
                pairwise_cipher=pairwise,
                pairwise_cipher_suites=tuple(pairwise_suites),
                pairwise_cipher_count=pairwise_count,
                akms=tuple(akms),
                akm_suites=tuple(akm_suites),
                akm_suite_count=akm_count,
                vendor_akm_offered=vendor_akm_offered,
                pmf_capable=pmf_capable,
                pmf_required=pmf_required,
                group_management_cipher_suite=group_management_cipher_suite,
            )
        except Exception:
            return None

    @classmethod
    def _parse_wpa_ie(cls, tag_data: bytes) -> Optional[WpaElement]:
        """Parse a WPA vendor IE body (from its 00-50-F2 OUI and type 1 through the AKM list),
        or None if malformed or truncated."""
        n = len(tag_data)
        # OUI + type (4) + version (2) + group cipher (4) + pairwise count (2) = 12.
        if n < 12 or int.from_bytes(tag_data[4:6], "little") != 1:
            return None
        pairwise_count = int.from_bytes(tag_data[10:12], "little")
        p = 12
        if p + 4 * pairwise_count + 2 > n:
            return None
        pairwise: Optional[str] = None
        for _ in range(pairwise_count):
            suite = tag_data[p:p + 4]
            if pairwise is None and suite[:3] == WPA_OUI:
                pairwise = cls._CIPHER_NAMES.get(suite[3])
            p += 4
        akm_count = int.from_bytes(tag_data[p:p + 2], "little")
        p += 2
        if p + 4 * akm_count > n:
            return None
        akms: List[str] = []
        akm_suites: List[int] = []
        vendor_akm_offered = False
        for _ in range(akm_count):
            suite = tag_data[p:p + 4]
            p += 4
            akm = cls._WPA_AKM_SUITES.get(suite[3]) if suite[:3] == WPA_OUI else None
            if akm is None:
                vendor_akm_offered = True
                name = "Vendor"
            else:
                name = cls._AKM_NAMES[akm]
                if akm not in akm_suites:
                    akm_suites.append(akm)
            if name not in akms:
                akms.append(name)
        return WpaElement(
            pairwise_cipher=pairwise,
            akms=tuple(akms),
            akm_suites=tuple(akm_suites),
            vendor_akm_offered=vendor_akm_offered,
        )

    @classmethod
    def _classify_wpa3_enterprise(cls, rsn: RsnElement, has_wpa: bool) -> tuple[bool, bool]:
        if has_wpa or not rsn.pmf_capable or not cls._enterprise_akms_only(rsn):
            return False, False

        akm_suites = set(rsn.akm_suites)
        if cls._wpa3_enterprise_192_security(rsn):
            if akm_suites == {AKM_802_1X_SUITE_B_192}:
                return True, False
            ft_192_akms = {AKM_802_1X_SUITE_B_192, AKM_FT_802_1X_SUITE_B_192}
            if AKM_FT_802_1X_SUITE_B_192 in akm_suites and akm_suites.issubset(ft_192_akms):
                return True, False

        if not cls._modern_rsn_ciphers(rsn) or AKM_802_1X_SHA256 not in akm_suites:
            return False, False
        if AKM_802_1X in akm_suites:
            return not rsn.pmf_required, not rsn.pmf_required
        return rsn.pmf_required, False

    @classmethod
    def _modern_rsn_ciphers(cls, rsn: RsnElement) -> bool:
        return (
            rsn.group_cipher_suite in cls._WPA3_DATA_CIPHERS
            and rsn.pairwise_cipher_count == len(rsn.pairwise_cipher_suites)
            and bool(rsn.pairwise_cipher_suites)
            and set(rsn.pairwise_cipher_suites).issubset(cls._WPA3_DATA_CIPHERS)
        )

    @staticmethod
    def _wpa3_enterprise_192_security(rsn: RsnElement) -> bool:
        return (
            rsn.pmf_required
            and rsn.pairwise_cipher_count == 1
            and rsn.pairwise_cipher_suites == (CIPHER_GCMP_256,)
            and rsn.group_cipher_suite == CIPHER_GCMP_256
            and rsn.group_management_cipher_suite == CIPHER_BIP_GMAC_256
        )

    @staticmethod
    def _enterprise_akms_only(rsn: RsnElement) -> bool:
        return (
            rsn.akm_suite_count == len(rsn.akm_suites)
            and bool(rsn.akm_suites)
            and set(rsn.akm_suites).issubset(ENTERPRISE_AKM_SUITES)
        )

    @staticmethod
    def _format_encryption_label(
        *,
        frame: bytes,
        has_rsn: bool,
        has_wpa: bool,
        has_wpa3: bool,
        transition_mode: bool,
        akms: List[str],
        pairwise_cipher: Optional[str],
    ) -> str:
        """Build an airodump-style encryption label.

        Examples:
            "WPA2-PSK-CCMP"
            "WPA3-SAE-CCMP"
            "WPA2/WPA3-PSK+SAE-CCMP"   (transition mode)
            "WPA2-EAP-CCMP"
            "WPA-EAP-TKIP"             (legacy WPA1 vendor IE only)
            "OPEN" / "WEP"
        """
        if has_rsn:
            # Every SAE-family name (SAE, FT-SAE, SAE-EXT-KEY, FT-SAE-EXT-KEY)
            # contains "SAE", so this also catches WPA3-H2E, matching the
            # suite-number `wpa3` flag so label and flag never disagree.
            has_sae = any("SAE" in a for a in akms)
            has_psk = any("PSK" in a for a in akms)
            enterprise_akms = [
                a for a in akms if a.startswith("EAP") or a.startswith("FT-EAP")
            ]
            has_owe = "OWE" in akms

            if transition_mode:
                wpa_tag = "WPA2/WPA3"
            elif has_wpa3:
                wpa_tag = "WPA3"
            elif has_owe:
                wpa_tag = "OWE"
            else:
                wpa_tag = "WPA2"

            if has_sae and has_psk:
                akm_tag = "PSK+SAE"
            elif has_sae:
                akm_tag = "SAE"
            elif has_owe:
                akm_tag = None
            elif enterprise_akms and has_psk:
                akm_tag = "PSK+" + "+".join(enterprise_akms)
            elif enterprise_akms:
                akm_tag = "+".join(enterprise_akms)
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
            if not akms:
                return "WPA-PSK-TKIP"   # malformed WPA1 vendor IE
            parts = ["WPA", "+".join(akms)]
            if pairwise_cipher:
                parts.append(pairwise_cipher)
            return "-".join(parts)

        if len(frame) >= 36:
            cap_info = int.from_bytes(frame[34:36], byteorder='little')
            if cap_info & 0x0010:  # Privacy bit
                return "WEP"
        return "OPEN"
