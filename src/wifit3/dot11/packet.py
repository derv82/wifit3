"""The typed 802.11 ``Packet`` hierarchy: one dataclass per frame type, built from raw MPDU
bytes by ``dot11.parser.WlanFrameParser``. Pure data (no parsing, no device), so it stays a
foundational leaf that ``parser`` and everything upstream can import.
"""
from dataclasses import dataclass, field
from typing import Optional, List

from wifit3.dot11.mac import mac_to_str


def is_group_mac(mac: str) -> bool:
    """True for a group (multicast/broadcast) MAC: the I/G bit (LSB of the first octet) is
    set. These are frame destinations (IPv6 ``33:33:…``, IPv4 ``01:00:5e:…``, broadcast
    ``ff:…``), never client stations, whose NICs are always unicast (even first octet)."""
    try:
        return bool(int(mac.split(":", 1)[0], 16) & 1)
    except (ValueError, IndexError):
        return True   # unparseable → never treat as a client


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

    @property
    def transmitter(self) -> str:
        """Addr2 (TA): the station that actually transmitted this frame -- the source for ToDS/mgmt,
        the BSSID for FromDS. Identifies a frame as one we sent (our TA), which ``source`` (addr3 on
        a FromDS frame) does not."""
        return mac_to_str(self.raw[10:16]) if len(self.raw) >= 16 else self.bssid

    @property
    def client_mac(self) -> Optional[str]:
        """The client (non-AP) STA MAC, decided by the DS bits, or None when there isn't one
        (WDS 4-address, or a group destination). An AP→client frame carries the wired-side
        origin in addr3, so key off direction; picking "the address that isn't the BSSID" would
        mint phantom clients from the gateway/router MAC on a bridged network."""
        mac = None
        if self.to_ds and not self.from_ds:        # client -> AP
            mac = self.source
        elif self.from_ds and not self.to_ds:      # AP -> client
            mac = self.dest
        elif not self.to_ds and not self.from_ds:  # mgmt / IBSS: the endpoint that isn't the AP
            if self.source and self.source != self.bssid:
                mac = self.source
            elif self.dest and self.dest != self.bssid:
                mac = self.dest
        return mac if mac and not is_group_mac(mac) else None


@dataclass(slots=True, kw_only=True)
class BeaconPacket(Packet):
    """A beacon or probe response: carries the AP's advertised capabilities (IEs)."""
    channel: Optional[int] = None
    encryption: str = "OPEN"
    akms: List[str] = field(default_factory=list)
    akm_suites: List[int] = field(default_factory=list)
    pairwise_cipher: Optional[str] = None
    wpa3: bool = False
    transition_mode: bool = False
    pmf_capable: bool = False
    pmf_required: bool = False
    beacon_protection: bool = False
    wps: bool = False
    wps_locked: bool = False
    wps_state: Optional[int] = None
    wps_version: Optional[str] = None
    wps_config_methods: int = 0
    wps_device_password_id: Optional[int] = None
    wps_selected_registrar: bool = False
    wps_manufacturer: Optional[str] = None
    wps_model_name: Optional[str] = None
    wps_model_number: Optional[str] = None
    wps_device_name: Optional[str] = None
    rsn_ie_raw: Optional[bytes] = None


@dataclass(slots=True, kw_only=True)
class EapolPacket(Packet):
    """An EAPOL-Key frame of the 4-way handshake. Fields may be unset (None) on a frame too
    short to fully extract. The interface guards on ``replay_counter`` before storing."""
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
    """A WEP-encrypted Data frame: the IV + leading ciphertext the WEP suite feeds on."""
    iv: Optional[bytes] = None
    keyid: Optional[int] = None
    cipher: Optional[bytes] = None


@dataclass(slots=True, kw_only=True)
class AssocRequestPacket(Packet):
    """A (Re)Association Request: carries the client's selected AKM."""
    assoc_akm: Optional[int] = None


@dataclass(slots=True, kw_only=True)
class AuthPacket(Packet):
    """An Open-System Authentication frame; ``status`` is the result code (0 = success)."""
    status: Optional[int] = None


@dataclass(slots=True, kw_only=True)
class AssocRespPacket(Packet):
    """A (Re)Association Response; ``status`` is the result code (0 = success)."""
    status: Optional[int] = None


@dataclass(slots=True, kw_only=True)
class DeauthPacket(Packet):
    """A Deauthentication or Disassociation; ``reason`` is the 802.11 reason code."""
    reason: Optional[int] = None


@dataclass(slots=True, kw_only=True)
class ProbeReqPacket(Packet):
    """A Probe Request; the requested SSID is the base ``ssid`` field."""
