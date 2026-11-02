"""WPA/WPA2 handshake capture models: the EAPOL-Key frames and the per-client
4-way handshake (or PMKID) they assemble into.
"""
from dataclasses import dataclass, field
from typing import ClassVar, Optional, List, Set, Dict, Tuple


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
    # 802.1X version byte through end of key data, i.e. the portion of the
    # frame hashcat embeds in the mode-22000 hashline. May be empty if the
    # capture was truncated.
    eapol_payload: bytes = b""
    # Arrival time (epoch seconds), stamped by the interface on capture. Used to bind
    # frames to a single handshake instance. 0.0 = unset.
    timestamp: float = 0.0
    # AKM (00-0F-AC:N) this association negotiated, snapshotted at capture (the frame's
    # own RSN IE, else the client's assoc selection). Gates crackability per instance.
    akm: Optional[int] = None
    akm_selection_rejected: bool = False  # the frame's RSN IE names no single AKM: no fallback
    # 802.11r identifiers an FT-PSK M2 carries in its Key Data; empty otherwise.
    mdid: bytes = b""
    r0khid: bytes = b""
    r1khid: bytes = b""


@dataclass
class Handshake:
    """Captured WPA/WPA2 4-way handshake (or PMKID) for one (BSSID, client_mac) pair.
    ``is_complete`` is True only when the stored messages form a hashcat-crackable pair
    (M1+M2, M2+M3, M3+M4, or M1+M4).
    """
    MAX_MESSAGES: ClassVar[int] = 256
    RETAINED_MESSAGES: ClassVar[int] = 192
    MAX_COMPLETE_INSTANCES: ClassVar[int] = 32

    bssid: str
    client_mac: str
    beacon_frame: Optional[bytes] = None

    # EAPOL-Key messages seen for this client, in arrival order, deduplicated.
    # Past MAX_MESSAGES, pruning keeps the best MAX_COMPLETE_INSTANCES pairs plus the newest messages.
    messages: List[HandshakeMessage] = field(default_factory=list)
    _raw_messages: Set[bytes] = field(default_factory=set, init=False, repr=False, compare=False)
    _indexed_messages: int = field(default=0, init=False, repr=False, compare=False)

    # Captured PMKID bytes (16 B from the RSN IE in EAPOL M1).
    pmkid: Optional[bytes] = None
    # AKM (00-0F-AC:N) in effect when the PMKID was captured; gates pmkid_crackable.
    pmkid_akm: Optional[int] = None
    # Key descriptor version of the M1 that carried the PMKID (3 = PSK-SHA256, 2 = PSK).
    pmkid_key_descriptor_version: Optional[int] = None

    # The AP's offered AKM suites (00-0F-AC:N) from its beacon RSN IE, the fallback
    # when a frame's own AKM is unknown. See crack.handshake.
    akm_offered: List[int] = field(default_factory=list)
    vendor_akm_offered: bool = False
    rsn_ie_seen: bool = False      # the AP sent an RSN IE, parseable or not

    # -- Crack-validity --------------------------------------------------------
    # Delegated to crack.handshake, the single source of truth shared with
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
        """Each crackable handshake instance (keyed by its ANonce, fresh per
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

    def _sync_message_index(self) -> None:
        if self._indexed_messages != len(self.messages):
            self._raw_messages = {message.raw for message in self.messages}
            self._indexed_messages = len(self.messages)

    def has_message(self, raw: bytes) -> bool:
        self._sync_message_index()
        return raw in self._raw_messages

    def add_message(self, message: HandshakeMessage) -> bool:
        self._sync_message_index()
        if message.raw in self._raw_messages:
            return False
        self.messages.append(message)
        self._raw_messages.add(message.raw)
        self._indexed_messages += 1
        if len(self.messages) > self.MAX_MESSAGES:
            self._prune_messages()
        return True

    def _prune_messages(self) -> None:
        keep_ids: Set[int] = set()
        instances: Set[bytes] = set()
        for pair in self._crackable_pairs():
            if pair.instance_key in instances:
                continue
            instances.add(pair.instance_key)
            keep_ids.update((id(pair.anonce_frame), id(pair.mic_frame)))
            if len(instances) >= self.MAX_COMPLETE_INSTANCES:
                break
        for message in reversed(self.messages):
            if len(keep_ids) >= self.RETAINED_MESSAGES:
                break
            keep_ids.add(id(message))
        self.messages = [message for message in self.messages if id(message) in keep_ids]
        self._raw_messages = {message.raw for message in self.messages}
        self._indexed_messages = len(self.messages)
