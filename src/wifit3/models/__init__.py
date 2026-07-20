"""Engine data contracts: the AP / client / handshake / capture dataclasses shared
across the parser, attacks, persistence, and UI.
"""
import time
from dataclasses import dataclass, field
from typing import Optional, List, Literal, Set, Dict, Tuple


@dataclass
class HandshakeMessage:
    """One EAPOL-Key frame, classified by its role in the 4-way handshake (M1-M4) and
    tagged with the fields a cracker needs.
    """
    raw: bytes
    msg_num: int  # 1, 2, 3, 4, or 0 if unclassified (group rekey etc.)
    replay_hex: str
    nonce: bytes
    mic: bytes
    key_data_len: int
    # 802.1X version byte through end of key data — i.e. the portion of the
    # frame hashcat embeds in the mode-22000 hashline. May be empty if the
    # capture was truncated.
    eapol_payload: bytes = b""
    # Arrival time (epoch seconds), stamped by the interface on capture. Used to bind
    # frames to a single handshake instance. 0.0 = unset.
    timestamp: float = 0.0


@dataclass
class Handshake:
    """Captured WPA/WPA2 4-way handshake (or PMKID) for one (BSSID, client_mac) pair.
    ``is_complete`` is True only when the stored messages form a hashcat-crackable pair
    (M1+M2, M2+M3, M3+M4, or M1+M4).
    """
    bssid: str
    client_mac: str
    beacon_frame: Optional[bytes] = None

    # All EAPOL-Key messages we've seen for this client, in arrival order.
    # We never wipe — only append-with-dedup — so once a valid pair is
    # captured nothing can clobber it.
    messages: List[HandshakeMessage] = field(default_factory=list)

    # Captured PMKID bytes (16 B from the RSN IE in EAPOL M1).
    pmkid: Optional[bytes] = None

    # Negotiated AKM for THIS association (00-0F-AC:N).
    # Read from the cleartext RSN IE from the client's EAPOL M2 (Client-level).
    # None until an M2 is seen. Authoritative over `akm_offered` (AP-level).
    akm_client: Optional[int] = None
    # The AP's offered AKM suites (00-0F-AC:N) from its beacon RSN IE.
    # Stamped by the interface so crackability can be decided without the AP in hand.
    # See crack.handshake (eapol_crackable / pmkid_crackable).
    akm_offered: List[int] = field(default_factory=list)

    # -- Crack-validity --------------------------------------------------------
    # Delegated to crack.handshake — the single source of truth shared with
    # the hc22000 / auto-save path, so "captured" and "saveable" can't diverge.
    # Deferred import: that module imports this one.

    def _crackable_pairs(self):
        from wifit3.crack import handshake as _wpa
        return _wpa.crackable_pairs(self)

    @staticmethod
    def _ordered(pair) -> Tuple[HandshakeMessage, HandshakeMessage]:
        """A CrackablePair rendered as a (lower-msg, higher-msg) frame tuple."""
        return tuple(sorted((pair.anonce_frame, pair.mic_frame),
                            key=lambda f: f.msg_num))

    def find_valid_pair(self) -> Optional[Tuple[HandshakeMessage, HandshakeMessage]]:
        """Highest-confidence crackable (lower-msg, higher-msg) pair, or None."""
        pairs = self._crackable_pairs()
        return self._ordered(pairs[0]) if pairs else None

    def valid_pairs_by_instance(self) -> Dict[bytes, Tuple[HandshakeMessage, HandshakeMessage]]:
        """Each crackable handshake instance (keyed by its ANonce — fresh per
        association) mapped to its (lower-msg, higher-msg) pair. Gated on a
        beacon: we don't announce a capture for an AP we've never heard beacon."""
        if not self.beacon_frame:
            return {}
        return {p.instance_key: self._ordered(p) for p in self._crackable_pairs()}

    @property
    def complete_instances(self) -> int:
        """Distinct crackable 4-way handshakes captured for this client."""
        return len(self.valid_pairs_by_instance())

    @property
    def is_complete(self) -> bool:
        return bool(self.beacon_frame) and bool(self._crackable_pairs())

    @property
    def captured_messages(self) -> Set[int]:
        """Set of message numbers (1-4) we've seen, ignoring unclassified."""
        return {f.msg_num for f in self.messages if f.msg_num}

    @property
    def total_messages(self) -> int:
        return len(self.messages)

    def has_message(self, raw: bytes) -> bool:
        return any(f.raw == raw for f in self.messages)


@dataclass
class WepStats:
    """WEP IV counters for one AP: unique IVs and total WEP frames seen."""
    unique_ivs: int = 0
    total_frames: int = 0


@dataclass
class PersistedCapture:
    """One previously-saved capture artifact found under captures/."""
    kind: Literal["HS", "PMKID", "WEP", "WPS"]
    timestamp: int                  # epoch seconds, parsed from the filename
    path: str                       # source file under captures/
    value: Optional[str] = None     # WEP key (hex) / WPS PSK; None for HS/PMKID


@dataclass
class AccessPoint:
    bssid: str
    ssid: Optional[str] = None
    channel: int = 1
    signal: int = -100
    encryption: Optional[str] = "Unknown"
    # Structured security fields from the RSN IE; `encryption` (above) is the airodump-style string.
    akms: List[str] = field(default_factory=list)
    # AKM suite numbers (00-0F-AC:N) from the RSN IE, parallel to `akms` (the names).
    akm_suites: List[int] = field(default_factory=list)
    pairwise_cipher: Optional[str] = None
    beacons: int = 0
    first_seen: float = field(default_factory=time.time)
    # Most recent beacon/probe-resp timestamp.
    last_seen: float = field(default_factory=time.time)
    wpa3: bool = False
    transition_mode: bool = False
    pmf_capable: bool = False
    pmf_required: bool = False

    # WPS state decoded from the WPS vendor IE (tag 221, OUI 00:50:F2 type 4).
    wps: bool = False
    wps_locked: bool = False
    wps_version: Optional[str] = None  # "1.0" / "2.0"
    wps_config_methods: int = 0  # 0x1008 bitmask
    wps_device_password_id: Optional[int] = None  # 0x0004 = PBC
    # Set while the AP is advertising an active Registrar (PIN or, with
    # DevPwId 0x0004, a Push-Button walk window). Drives wps_pbc_active.
    wps_selected_registrar: bool = False

    # Most recent raw beacon bytes.
    last_beacon_frame: Optional[bytes] = None

    # Raw RSN IE bytes (tag 48, incl. the 2-byte tag header) as advertised in the AP's beacons.
    rsn_ie: Optional[bytes] = None

    # How this AP's SSID was learned, if it was ever hidden.
    # None = we never saw it hidden, or it's still hidden.
    decloak_method: Optional[str] = None

    # BSSIDs we believe are virtual interfaces of the same physical radio (Main + Guest +
    # IoT on one router). Bidirectional.
    siblings: List[str] = field(default_factory=list)

    # Per-client handshake captures, keyed by client MAC (clients can capture simultaneously).
    handshakes: Dict[str, Handshake] = field(default_factory=dict)

    # WEP IV counters, populated for WEP APs on the first encrypted Data frame (None otherwise).
    wep: Optional[WepStats] = None

    # Recovered WEP key (the cracker's payoff).
    wep_key: Optional[bytes] = None

    # Recovered WPS PSK from a successful Push-Button (PBC) capture.
    wps_pbc_psk: Optional[str] = None

    # Recovered WPS PIN + the passphrase it yielded, from a successful PIN brute-force.
    # Kept distinct from wps_pbc_psk (PIN vs Push-Button).
    wps_pin: Optional[str] = None
    wps_pin_psk: Optional[str] = None

    # Read-only capture history loaded from captures/ at scan start.
    persisted: List[PersistedCapture] = field(default_factory=list)

    @property
    def wps_pbc_active(self) -> bool:
        """True during a WPS Push-Button walk window — the AP advertises PBC
        (Device Password ID 0x0004) with an active Selected Registrar."""
        return (
            self.wps
            and self.wps_selected_registrar
            and self.wps_device_password_id == 0x0004
        )

    @property
    def known_psk(self) -> Optional[str]:
        """The passphrase we hold for this AP from any source — recovered this session
        (PBC/PIN) or loaded from a prior session's captures/ WPS file, else None. A WPS PIN
        alone (no PSK) does not count."""
        return (
            self.wps_pbc_psk
            or self.wps_pin_psk
            or next((p.value for p in self.persisted if p.kind == "WPS" and p.value), None)
        )

    @property
    def has_psk(self) -> bool:
        """True once we hold this AP's passphrase (see known_psk)."""
        return self.known_psk is not None


@dataclass
class Client:
    """A wireless client (e.g. a phone or laptop)."""
    mac: str
    bssid: Optional[str] = None  # The AP it is currently connected to or probing for
    signal: int = -100
    packets: int = 0
    probed_ssids: Set[str] = field(default_factory=set)  # SSIDs this client is actively searching for
    # AKM suite chosen by this client, read from the RSN IE in its (Re)Assoc Request. Latest-wins.
    akm_selected: Optional[int] = None
    # True for the forged STA *we* inject as (e.g. WEP fake-auth).
    is_self: bool = False
