import logging
import struct
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


# Parsed 802.11 frame, as a typed hierarchy: the fields on ``Packet`` are on every frame;
# each subclass adds the fields that only exist for its frame type. Accessing (say) an EAPOL
# field on a BeaconPacket is a hard error (AttributeError / a red squiggle), not a silent None.
# kw_only so subclass fields need no ordering dance behind the base's ``ssid`` default; slots
# for a smaller, faster per-frame object than the dict it replaces.
@dataclass(slots=True, kw_only=True)
class Packet:
    type: str                 # airodump-style label: "beacon", "eapol", "wep_data", "mgmt_5", …
    type_id: int              # 802.11 frame type (0=mgmt, 1=ctrl, 2=data)
    subtype_id: int
    bssid: str
    source: str
    dest: str
    to_ds: bool
    from_ds: bool
    rssi: int
    raw: bytes
    ssid: Optional[str] = None   # on beacon/probe_resp/probe_req/assoc_req; None elsewhere


@dataclass(slots=True, kw_only=True)
class BeaconPacket(Packet):
    """A beacon or probe response — carries the AP's advertised capabilities (IEs)."""
    channel: Optional[int] = None
    encryption: str = "OPEN"
    akms: List[str] = field(default_factory=list)
    akm_suites: List[int] = field(default_factory=list)
    pairwise_cipher: Optional[str] = None
    wpa3: bool = False
    transition_mode: bool = False
    pmf_capable: bool = False
    pmf_required: bool = False
    wps: bool = False
    wps_locked: bool = False
    wps_state: Optional[int] = None
    wps_version: Optional[str] = None
    wps_config_methods: int = 0
    wps_device_password_id: Optional[int] = None
    wps_selected_registrar: bool = False
    rsn_ie_raw: Optional[bytes] = None


@dataclass(slots=True, kw_only=True)
class EapolPacket(Packet):
    """An EAPOL-Key frame of the 4-way handshake. Fields may be unset (None) on a frame too
    short to fully extract — the interface guards on ``replay_counter`` before storing."""
    msg_num: int = 0
    replay_counter: Optional[bytes] = None
    nonce: Optional[bytes] = None
    mic: Optional[bytes] = None
    key_data_len: int = 0
    payload: bytes = b""
    pmkid: Optional[bytes] = None
    akm: Optional[int] = None
    key_info: Optional[int] = None


@dataclass(slots=True, kw_only=True)
class WepDataPacket(Packet):
    """A WEP-encrypted Data frame — the IV + leading ciphertext the WEP suite feeds on."""
    iv: Optional[bytes] = None
    keyid: Optional[int] = None
    cipher: Optional[bytes] = None


@dataclass(slots=True, kw_only=True)
class AssocRequestPacket(Packet):
    """A (Re)Association Request — carries the client's selected AKM."""
    assoc_akm: Optional[int] = None


# Keys of the base ``Packet`` — used to split a raw parse dict into base vs. type-specific
# fields when the type-specific keys need renaming (eapol_/wep_ prefixes are dropped).
_BASE_FIELDS = (
    "type", "type_id", "subtype_id", "bssid", "source", "dest",
    "to_ds", "from_ds", "rssi", "raw", "ssid",
)


class WlanFrameParser:
    """Native, dependency-free 802.11 frame parser (no Scapy).

    Every driver feeds it a bare MPDU (its hardware RX descriptor already stripped)
    plus an RSSI; :meth:`parse_80211_frame` returns the typed :class:`Packet` subclass
    for the frame's type.
    """

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
    SUBTYPE_DEAUTH = 0x0c
    
    SUBTYPE_DATA = 0x00
    SUBTYPE_QOS_DATA = 0x08

    @staticmethod
    def parse_80211_frame(frame: bytes, rssi: int) -> Optional["Packet"]:
        """Generic 802.11 frame parser: a raw MPDU + RSSI -> the matching typed
        ``Packet`` subclass, or ``None`` if the frame is noise / unparseable / an
        unsupported type (WDS, most control frames).
        """
        if not WlanFrameParser._is_valid_frame(frame):
            return None

        fc0 = frame[0]
        fc1 = frame[1]
        ftype = (fc0 & 0x0C) >> 2
        subtype = (fc0 & 0xF0) >> 4
        
        to_ds = (fc1 & 0x01) != 0
        from_ds = (fc1 & 0x02) != 0

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
            "to_ds": to_ds,
            "from_ds": from_ds,
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
            elif subtype == WlanFrameParser.SUBTYPE_REASSOC_REQ:
                result["type"] = "reassoc_req"
            elif subtype == WlanFrameParser.SUBTYPE_ASSOC_RESP:
                result["type"] = "assoc_resp"
            else:
                result["type"] = f"mgmt_{subtype}"
                
            # Parse Tags for Beacons and Probe Responses
            if subtype in (WlanFrameParser.SUBTYPE_BEACON, WlanFrameParser.SUBTYPE_PROBE_RESP):
                tags = WlanFrameParser._parse_tags(frame, subtype)
                if tags is None:
                    return None
                result["ssid"] = tags.get("ssid")
                # Don't synthesise channel=1 when _parse_tags found nothing — the caller
                # falls back to the chip's current tuned channel (a default of 1 mis-tags
                # 5 GHz beacons whose vendor omits the DS Parameter Set IE).
                if "channel" in tags:
                    result["channel"] = tags["channel"]
                result["encryption"] = tags.get("encryption", "OPEN")
                # WPA3 / PMF / cipher details surfaced from the RSN IE walker.
                result["wpa3"] = tags.get("wpa3", False)
                result["transition_mode"] = tags.get("transition_mode", False)
                result["pmf_capable"] = tags.get("pmf_capable", False)
                result["pmf_required"] = tags.get("pmf_required", False)
                result["pairwise_cipher"] = tags.get("pairwise_cipher")
                result["akms"] = tags.get("akms", [])
                result["akm_suites"] = tags.get("akm_suites", [])
                if "rsn_ie_raw" in tags:
                    result["rsn_ie_raw"] = tags["rsn_ie_raw"]
                for wps_key in (
                    "wps", "wps_locked", "wps_version", "wps_state",
                    "wps_config_methods", "wps_device_password_id",
                    "wps_selected_registrar",
                ):
                    if wps_key in tags:
                        result[wps_key] = tags[wps_key]
            elif subtype in (WlanFrameParser.SUBTYPE_PROBE_REQ, WlanFrameParser.SUBTYPE_ASSOC_REQ):
                tags = WlanFrameParser._parse_tags(frame, subtype)
                if tags is None:
                    return None
                result["ssid"] = tags.get("ssid")

            # Client's selected AKM from the RSN IE in a (Re)Assoc Request. The IE list
            # starts past the fixed fields: 28 for Assoc (24 hdr + cap + listen),
            # 34 for Reassoc (+ 6-byte Current AP address).
            if subtype == WlanFrameParser.SUBTYPE_ASSOC_REQ:
                akm = WlanFrameParser._first_rsn_akm(frame, 28)
                if akm is not None:
                    result["assoc_akm"] = akm
            elif subtype == WlanFrameParser.SUBTYPE_REASSOC_REQ:
                akm = WlanFrameParser._first_rsn_akm(frame, 34)
                if akm is not None:
                    result["assoc_akm"] = akm

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

            # Protected (WEP/TKIP/CCMP) Data frame? The 4 bytes after the
            # MAC header are the IV header. The Key ID byte's ExtIV bit
            # (0x20) tells WEP (clear → 4-byte IV) from TKIP/CCMP (set →
            # 8-byte extended IV) without needing the AP's beacon. WEP gives
            # us a fresh 3-byte IV per frame — the raw material the IV
            # collector tallies and the cracker eventually consumes.
            if (fc1 & 0x40) and len(frame) >= header_len + 4:
                keyid_byte = frame[header_len + 3]
                if not (keyid_byte & 0x20):  # ExtIV clear → WEP
                    result["type"] = "wep_data"
                    result["wep_iv"] = bytes(frame[header_len : header_len + 3])
                    result["wep_keyid"] = (keyid_byte >> 6) & 0x03
                    # First 16 ciphertext bytes (after the 4-byte IV header).
                    # XORed with the known ARP plaintext these give the
                    # keystream the PTW cracker votes on.
                    cipher_start = header_len + 4
                    result["wep_cipher"] = bytes(
                        frame[cipher_start : cipher_start + 16]
                    )
                # Encrypted body — no plaintext LLC/SNAP to find below.
                return WlanFrameParser._to_packet(result)

            if len(frame) >= header_len + 8:
                # The hardware DMA engine pads the 802.11 header so the payload is 4-byte aligned.
                # If the header is 26 bytes, it adds 2 bytes of padding (e.g. `00 00`), pushing LLC to offset 28.
                # We use a sliding window to gracefully find the LLC/SNAP signature regardless of padding.
                llc_snap_sig = b'\xaa\xaa\x03\x00\x00\x00\x88\x8e'
                search_window = frame[header_len : header_len + 16]
                sig_idx = search_window.find(llc_snap_sig)
                
                if sig_idx != -1:
                    result["type"] = "eapol"
                    # Extract EAPOL metadata. Layout (offsets from eapol_start):
                    #   0   802.1X version (1B)
                    #   1   802.1X type    (1B) — must be 3 (EAPOL-Key)
                    #   2   802.1X length  (2B)
                    #   4   Key Desc Type  (1B) — 2=RSN, 254=WPA
                    #   5   Key Info       (2B, BE)
                    #   7   Key Length     (2B)
                    #   9   Replay Counter (8B)
                    #   17  Key Nonce      (32B)
                    #   49  Key IV         (16B)
                    #   65  Key RSC        (8B)
                    #   73  Reserved       (8B)
                    #   81  Key MIC        (16B)
                    #   97  Key Data Length (2B, BE)
                    #   99  Key Data       (variable)
                    eapol_start = header_len + sig_idx + 8
                    if len(frame) >= eapol_start + 99:
                        eapol_type = frame[eapol_start + 1]
                        if eapol_type == 3:  # EAPOL-Key
                            key_info = struct.unpack(">H", frame[eapol_start + 5: eapol_start + 7])[0]
                            replay_counter = frame[eapol_start + 9: eapol_start + 17]
                            nonce = frame[eapol_start + 17: eapol_start + 49]
                            mic = frame[eapol_start + 81: eapol_start + 97]
                            key_data_len = struct.unpack(">H", frame[eapol_start + 97: eapol_start + 99])[0]

                            result["eapol_key_info"] = key_info
                            result["eapol_replay_counter"] = replay_counter
                            result["eapol_nonce"] = nonce
                            result["eapol_mic"] = mic
                            result["eapol_key_data_len"] = key_data_len
                            msg_num = WlanFrameParser._classify_eapol_msg(key_info, key_data_len)
                            result["eapol_msg_num"] = msg_num
                            # Slice the 802.1X portion (header + full key
                            # descriptor + key data). This is what hashcat's
                            # mode 22000 hashline embeds; storing it now
                            # avoids re-finding LLC/SNAP at save time.
                            total_eapol_len = 99 + key_data_len
                            if len(frame) >= eapol_start + total_eapol_len:
                                result["eapol_payload"] = bytes(
                                    frame[eapol_start: eapol_start + total_eapol_len]
                                )
                            # PMKID extraction runs even when the frame is a few bytes short
                            # (seen on mt76x0u / rt2800usb RT5572): the PMKID KDE sits at the
                            # START of key_data, so a complete KDE survives a truncated tail.
                            if key_data_len > 0:
                                key_data = frame[
                                    eapol_start + 99 : eapol_start + 99 + key_data_len
                                ]
                                if key_data:
                                    pmkid = WlanFrameParser._extract_pmkid_kde(key_data)
                                    if pmkid is not None:
                                        result["eapol_pmkid"] = pmkid
                                    # M2's Key Data carries the supplicant's RSN IE in
                                    # the clear — M2 has no GTK, so (unlike M3) its Key
                                    # Data isn't KEK-encrypted. This is the one usable
                                    # source of the AKM the client actually negotiated.
                                    if msg_num == 2:
                                        akm = WlanFrameParser._first_rsn_akm(key_data)
                                        if akm is not None:
                                            result["eapol_akm"] = akm
        else:
            result["type"] = f"ctrl_{subtype}"

        return WlanFrameParser._to_packet(result)

    @staticmethod
    def _to_packet(r: Dict[str, Any]) -> "Packet":
        """Wrap a finished parse dict in its typed ``Packet`` subclass.

        Beacon/probe/assoc keys already match their dataclass fields, so they splat directly;
        the eapol_/wep_-prefixed keys are mapped to the (un-prefixed) subclass fields.
        """
        t = r["type"]
        if t in ("beacon", "probe_resp"):
            return BeaconPacket(**r)
        if t in ("assoc_req", "reassoc_req"):
            return AssocRequestPacket(**r)
        base = {k: r[k] for k in _BASE_FIELDS if k in r}
        if t == "eapol":
            return EapolPacket(
                **base,
                msg_num=r.get("eapol_msg_num", 0),
                replay_counter=r.get("eapol_replay_counter"),
                nonce=r.get("eapol_nonce"),
                mic=r.get("eapol_mic"),
                key_data_len=r.get("eapol_key_data_len", 0),
                payload=r.get("eapol_payload", b""),
                pmkid=r.get("eapol_pmkid"),
                akm=r.get("eapol_akm"),
                key_info=r.get("eapol_key_info"),
            )
        if t == "wep_data":
            return WepDataPacket(
                **base,
                iv=r.get("wep_iv"), keyid=r.get("wep_keyid"), cipher=r.get("wep_cipher"),
            )
        return Packet(**r)

    # Key Info bit masks (802.11i, 16-bit BE field):
    #   bit 6 = INSTALL, bit 7 = KEY_ACK, bit 8 = KEY_MIC
    _KI_INSTALL = 0x0040
    _KI_ACK = 0x0080
    _KI_MIC = 0x0100

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
        install = bool(key_info & WlanFrameParser._KI_INSTALL)
        ack = bool(key_info & WlanFrameParser._KI_ACK)
        mic = bool(key_info & WlanFrameParser._KI_MIC)

        if ack and not mic and not install:
            return 1
        if ack and mic and install:
            return 3
        if not ack and mic and not install:
            # M2 vs M4 disambiguated by Key Data presence: M2 carries the
            # supplicant's RSN IE, M4 carries nothing.
            return 2 if key_data_len > 0 else 4
        return 0

    # PMKID KDE: 0xDD <len=0x14> 00 0F AC 04 <16-byte PMKID>
    # Encapsulated in EAPOL-Key Key Data (sometimes within a "GTK/PMKID
    # KDE wrapper" alongside other KDEs). We walk the Key Data as a
    # sequence of (Type, Length, Value) records, where Type=0xDD denotes
    # a vendor-specific KDE and the OUI+DataType discriminates it.
    _PMKID_KDE_OUI = b"\x00\x0f\xac"
    _PMKID_KDE_DATA_TYPE = 0x04

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
                    key_data[value_start : value_start + 3] == WlanFrameParser._PMKID_KDE_OUI
                    and key_data[value_start + 3] == WlanFrameParser._PMKID_KDE_DATA_TYPE
                ):
                    pmkid = bytes(key_data[value_start + 4 : value_start + 4 + 16])
                    # Some APs include a PMKID KDE with all-zero bytes as a
                    # placeholder. Treat as "no PMKID" — uncrackable anyway.
                    if pmkid != b"\x00" * 16:
                        return pmkid
            i = value_end
        return None

    @staticmethod
    def _first_rsn_akm(data: bytes, start: int = 0) -> Optional[int]:
        """First AKM suite (00-0F-AC:N) in the RSN IE within an element/KDE list,
        or None. ``data`` is walked as (tag, len, value) from ``start``; the RSN
        IE is a plain element with tag 48 (0x30), and we return the single suite
        the supplicant selected for this association.

        Shared by the two cleartext sources of the client's chosen AKM: EAPOL M2's
        Key Data (``start=0``) and a (Re)Assoc Request's IE list (``start`` past
        the fixed fields).
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
                rsn = WlanFrameParser._parse_rsn_ie(data[value_start:value_end])
                if rsn and rsn["akm_suites"]:
                    return rsn["akm_suites"][0]
                return None
            i = value_end
        return None

    @staticmethod
    def _is_valid_frame(frame: bytes) -> bool:
        if len(frame) < 24:
            return False
        fc0 = frame[0]
        
        # Protocol version must be 0
        if (fc0 & 0x03) != 0:
            return False
        
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
            # Sanity-check addresses to filter random USB noise from real frames.
            if len(frame) < 24:
                return False
                
            addr2 = frame[10:16]
            addr3 = frame[16:22]
            
            # addr2 is always the transmitter (SA) — never legitimately
            # broadcast or zero, so it's a good noise filter.
            if addr2 == b'\x00\x00\x00\x00\x00\x00' or addr2 == b'\xff\xff\xff\xff\xff\xff':
                return False
            # addr3 is the DA on a ToDS frame, and a BROADCAST DA is exactly what a (WEP) ARP
            # request carries — so reject only all-zeros here, NOT broadcast.
            if addr3 == b'\x00\x00\x00\x00\x00\x00':
                return False

            return True

        return False

    @staticmethod
    def _mac_to_str(mac_bytes: bytes) -> str:
        if len(mac_bytes) != 6:
            return "00:00:00:00:00:00"
        return ":".join(f"{b:02x}" for b in mac_bytes)

    # WPS attribute IDs (big-endian) we care about. WSC spec §12.
    _WPS_ATTR_VERSION = 0x104A
    _WPS_ATTR_STATE = 0x1044
    _WPS_ATTR_AP_SETUP_LOCKED = 0x1057
    _WPS_ATTR_SELECTED_REGISTRAR = 0x1041
    _WPS_ATTR_DEVICE_PASSWORD_ID = 0x1012
    _WPS_ATTR_CONFIG_METHODS = 0x1008
    _WPS_ATTR_VENDOR_EXTENSION = 0x1049

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

    @staticmethod
    def _parse_wps_ie(data: bytes) -> Dict[str, Any]:
        """Walk the WPS IE's nested big-endian TLVs (``data`` = bytes after
        the OUI + OUI-type) and surface the attacker-relevant subset.

        Each TLV is a 2-byte attribute id, 2-byte length, then value.
        Missing attributes leave their fields at the model defaults.
        """
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
            if attr == WlanFrameParser._WPS_ATTR_AP_SETUP_LOCKED and ln >= 1:
                out["wps_locked"] = val[0] == 0x01
            elif attr == WlanFrameParser._WPS_ATTR_STATE and ln >= 1:
                out["wps_state"] = val[0]          # 1=unconfigured, 2=configured
            elif attr == WlanFrameParser._WPS_ATTR_CONFIG_METHODS and ln >= 2:
                out["wps_config_methods"] = (val[0] << 8) | val[1]
            elif attr == WlanFrameParser._WPS_ATTR_DEVICE_PASSWORD_ID and ln >= 2:
                out["wps_device_password_id"] = (val[0] << 8) | val[1]
            elif attr == WlanFrameParser._WPS_ATTR_SELECTED_REGISTRAR and ln >= 1:
                out["wps_selected_registrar"] = val[0] == 0x01
            elif attr == WlanFrameParser._WPS_ATTR_VERSION and ln >= 1:
                version1 = True
            elif attr == WlanFrameParser._WPS_ATTR_VENDOR_EXTENSION:
                v2 = WlanFrameParser._wps_version2(val)
                if v2 is not None:
                    version2 = v2
        if version2 >= 0x20:
            out["wps_version"] = "2.0"
        elif version1:
            out["wps_version"] = "1.0"
        return out

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
        pairwise_cipher: Optional[str] = None
        akms: List[str] = []
        akm_suites: List[int] = []
        # Channel sources, in preference order:
        #   1. DS Parameter Set (tag 3)  — present on every 2.4 GHz beacon,
        #      OPTIONAL on 5 GHz per 802.11-2020 9.4.2.3 (most APs omit it).
        #   2. HT Operation (tag 61)     — primary channel byte; present on
        #      every 802.11n/ac AP regardless of band.
        #   3. VHT Operation (tag 192)   — channel center freq seg 0;
        #      tertiary fallback for VHT-only oddities.
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
                break # bounds check

            tag_data = frame[tag_start : tag_end]

            if tag_id == 0 and not seen_ssid: # SSID (only the first)
                seen_ssid = True
                if tag_len == 0:
                    parsed["ssid"] = "<hidden>"
                elif tag_len <= 32:
                    # Validate against completely corrupted text
                    if any(b < 0x20 and b not in (0x09, 0x0a, 0x0d) for b in tag_data):
                        return None # Corrupt frame masquerading as valid
                    try:
                        parsed["ssid"] = tag_data.decode('utf-8', errors='ignore')
                    except Exception:
                        pass
            elif tag_id == 3: # DS Parameter Set (Channel)
                if tag_len == 1:
                    channel_ds = tag_data[0]
            elif tag_id == 61: # HT Operation — primary channel = first byte
                if tag_len >= 1:
                    channel_ht = tag_data[0]
            elif tag_id == 192: # VHT Operation — center freq seg 0 at byte 1
                # IE layout (802.11ac-2013 8.4.2.157):
                #   byte 0 = Channel Width
                #   byte 1 = Channel Center Freq Segment 0  ← primary on 20 MHz
                #   byte 2 = Channel Center Freq Segment 1
                #   bytes 3-4 = Basic VHT-MCS Set
                if tag_len >= 2:
                    channel_vht = tag_data[1]
            elif tag_id == 48: # RSN (WPA2/WPA3)
                has_rsn = True
                # Preserve the raw IE bytes (with tag header) so the PMKID
                # harvester can echo the AP's exact RSN config in Assoc Req.
                parsed["rsn_ie_raw"] = bytes(frame[ptr : tag_end])
                rsn = WlanFrameParser._parse_rsn_ie(tag_data)
                if rsn is not None:
                    pairwise_cipher = rsn["pairwise"]
                    akms = rsn["akms"]
                    akm_suites = rsn["akm_suites"]
                    pmf_capable = rsn["pmf_capable"]
                    pmf_required = rsn["pmf_required"]
                    # SAE-family => WPA3; SAE + a PSK-family suite => transition.
                    # Suite-number based so WPA3-H2E (SAE-EXT-KEY, 24) is caught.
                    has_wpa3 = bool(WlanFrameParser._SAE_SUITES.intersection(akm_suites))
                    transition_mode = has_wpa3 and bool(
                        WlanFrameParser._PSK_SUITES.intersection(akm_suites)
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
                                WlanFrameParser._parse_wps_ie(tag_data[4:])
                            )

            ptr = tag_end

        # Pick the best channel signal we have. DS Param IE is authoritative
        # when present (matches the 2.4 GHz "official" channel byte). HT Op
        # IE is the only universal cross-band source. Caller (interface.py)
        # falls back to its tuned channel when we report nothing.
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
        parsed["pairwise_cipher"] = pairwise_cipher
        parsed["akms"] = akms
        parsed["akm_suites"] = akm_suites
        parsed["encryption"] = WlanFrameParser._format_encryption_label(
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
    # Mirrors the crackability split in engine.wpa.handshake (duplicated here to
    # avoid a wlan->engine import) — keep the two in sync.
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
        pmf_required — or None if the IE is malformed.

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
                    akm_suites.append(sid)   # raw 00-0F-AC:N — drives crackability
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
            # contains "SAE", so this also catches WPA3-H2E — matching the
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
                    # Unknown AKM(s) — fall back to listing them.
                    akm_tag = "+".join(akms) if akms else None

            parts = [wpa_tag]
            if akm_tag:
                parts.append(akm_tag)
            if pairwise_cipher:
                parts.append(pairwise_cipher)
            return "-".join(parts)

        if has_wpa:
            # Legacy WPA1 vendor IE — TKIP is the universal assumption.
            return "WPA-PSK-TKIP"

        if len(frame) >= 36:
            cap_info = int.from_bytes(frame[34:36], byteorder='little')
            if cap_info & 0x0010:  # Privacy bit
                return "WEP"
        return "OPEN"