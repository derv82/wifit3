from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List, Set, Dict

class Handshake(BaseModel):
    """
    Represents a captured WPA/WPA2 4-way handshake.
    Contains the raw binary frames needed to generate a valid .pcap for hashcat/hashcat.
    """
    bssid: str
    client_mac: str
    beacon_frame: Optional[bytes] = None
    
    # Key: Replay Counter (hex string) -> Value: List of raw EAPOL frames
    # M1 and M2 share the exact same replay counter, guaranteeing a valid pair!
    eapol_frames_by_replay: Dict[str, List[bytes]] = Field(default_factory=dict)
    
    @property
    def is_complete(self) -> bool:
        """
        A handshake is usable for cracking if we have the AP's beacon and
        at least two EAPOL frames sharing the same replay counter (an M1/M2 pair).
        """
        if not self.beacon_frame:
            return False
        return any(len(frames) >= 2 for frames in self.eapol_frames_by_replay.values())

class AccessPoint(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    
    bssid: str
    ssid: Optional[str] = Field(default=None)
    channel: int = Field(default=1)
    signal: int = Field(default=-100)
    encryption: Optional[str] = Field(default="Unknown")
    beacons: int = Field(default=0)
    
    handshake: Optional[Handshake] = None

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
