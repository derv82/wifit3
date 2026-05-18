from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List, Set, Dict

class Handshake(BaseModel):
    """
    Represents a captured WPA/WPA2 4-way handshake (or a PMKID capture)
    for a specific (BSSID, client_mac) pair.
    Contains the raw binary frames needed to generate a valid .pcap for hashcat -m 22000.
    """
    bssid: str
    client_mac: str
    beacon_frame: Optional[bytes] = None

    # Key: Replay Counter (hex string) -> Value: List of raw EAPOL frames
    # M1 and M2 share the exact same replay counter, guaranteeing a valid pair!
    # We never wipe entries — only accumulate, so a fresh M3/M4 pair can't
    # destroy an already-complete M1/M2 pair before the user saves it.
    eapol_frames_by_replay: Dict[str, List[bytes]] = Field(default_factory=dict)

    # Captured PMKID bytes (16 B from RSN IE in EAPOL M1). Forward-compat —
    # populated by the PMKID attack path when it lands.
    pmkid: Optional[bytes] = None

    @property
    def is_complete(self) -> bool:
        """
        A handshake is usable for cracking if we have the AP's beacon and
        at least two EAPOL frames sharing the same replay counter (an M1/M2 pair).
        """
        if not self.beacon_frame:
            return False
        return any(len(frames) >= 2 for frames in self.eapol_frames_by_replay.values())

    @property
    def total_eapol_frames(self) -> int:
        return sum(len(frames) for frames in self.eapol_frames_by_replay.values())

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
