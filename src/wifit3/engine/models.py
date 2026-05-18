from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List, Set, Dict, Tuple


class EapolFrame(BaseModel):
    """
    A single parsed EAPOL-Key frame, classified by its role in the 4-way
    handshake (M1/M2/M3/M4) and tagged with the fields a cracker needs.
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


def _replay_to_int(replay_hex: str) -> int:
    return int.from_bytes(bytes.fromhex(replay_hex), "big")


class Handshake(BaseModel):
    """
    Captured WPA/WPA2 4-way handshake (or PMKID) for one (BSSID, client_mac)
    pair. ``is_complete`` is True only when the stored EAPOL frames actually
    form a hashcat-crackable pair (M1+M2, M2+M3, M3+M4, or M1+M4) — two M1
    retries with the same replay counter no longer mistakenly qualify.
    """
    bssid: str
    client_mac: str
    beacon_frame: Optional[bytes] = None

    # All EAPOL-Key frames we've seen for this client, in arrival order.
    # We never wipe — only append-with-dedup — so once a valid pair is
    # captured nothing can clobber it.
    eapol_frames: List[EapolFrame] = Field(default_factory=list)

    # Captured PMKID bytes (16 B from RSN IE in EAPOL M1). Forward-compat —
    # populated by the PMKID attack path when it lands.
    pmkid: Optional[bytes] = None

    # -- Crack-validity -------------------------------------------------------

    def find_valid_pair(self) -> Optional[Tuple[EapolFrame, EapolFrame]]:
        """Return the lowest-numbered hashcat-valid (a, b) pair, else None.

        Accepted pairs (per `hcxpcapngtool` semantics):
          M1+M2  (same replay counter)
          M2+M3  (M3.replay == M2.replay + 1)
          M3+M4  (same replay counter)
          M1+M4  (M4.replay == M1.replay + 1)
        """
        by_msg: Dict[int, List[EapolFrame]] = {}
        for f in self.eapol_frames:
            if f.msg_num:
                by_msg.setdefault(f.msg_num, []).append(f)

        for a in by_msg.get(1, []):
            for b in by_msg.get(2, []):
                if a.replay_hex == b.replay_hex:
                    return (a, b)
        for a in by_msg.get(2, []):
            for b in by_msg.get(3, []):
                if _replay_to_int(b.replay_hex) == _replay_to_int(a.replay_hex) + 1:
                    return (a, b)
        for a in by_msg.get(3, []):
            for b in by_msg.get(4, []):
                if a.replay_hex == b.replay_hex:
                    return (a, b)
        for a in by_msg.get(1, []):
            for b in by_msg.get(4, []):
                if _replay_to_int(b.replay_hex) == _replay_to_int(a.replay_hex) + 1:
                    return (a, b)
        return None

    @property
    def is_complete(self) -> bool:
        if not self.beacon_frame:
            return False
        return self.find_valid_pair() is not None

    @property
    def captured_messages(self) -> Set[int]:
        """Set of message numbers (1-4) we've seen, ignoring unclassified."""
        return {f.msg_num for f in self.eapol_frames if f.msg_num}

    @property
    def total_eapol_frames(self) -> int:
        return len(self.eapol_frames)

    def has_frame(self, raw: bytes) -> bool:
        return any(f.raw == raw for f in self.eapol_frames)

class AccessPoint(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    
    bssid: str
    ssid: Optional[str] = Field(default=None)
    channel: int = Field(default=1)
    signal: int = Field(default=-100)
    encryption: Optional[str] = Field(default="Unknown")
    beacons: int = Field(default=0)
    first_seen: float = Field(default_factory=lambda: __import__("time").time())
    wpa3: bool = Field(default=False)
    transition_mode: bool = Field(default=False)
    pmf_capable: bool = Field(default=False)
    pmf_required: bool = Field(default=False)

    # Most recent raw beacon bytes — used so per-client handshakes created
    # AFTER the first beacon still get a beacon stamped onto them.
    last_beacon_frame: Optional[bytes] = Field(default=None)

    # Per-client handshake captures, keyed by client MAC. Replaces the old
    # single-handshake-per-AP field — multiple clients can be capturing
    # simultaneously and we must not overwrite a complete one when a new
    # client's EAPOL arrives.
    handshakes: Dict[str, Handshake] = Field(default_factory=dict)

    @property
    def has_capture(self) -> bool:
        """True iff at least one client has a complete handshake or a PMKID."""
        return any(hs.is_complete or hs.pmkid for hs in self.handshakes.values())

class Client(BaseModel):
    """
    Represents a wireless client (e.g., a phone or laptop).
    Maps to the bottom table in airodump-ng.
    """
    model_config = ConfigDict(validate_assignment=True)
    
    mac: str
    bssid: Optional[str] = Field(default=None) # The AP it is currently connected to or probing for
    signal: int = Field(default=-100)
    packets: int = Field(default=0)
    probed_ssids: Set[str] = Field(default_factory=set) # List of SSIDs this client is actively searching for
