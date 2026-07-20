"""The wireless-client scan model."""
from dataclasses import dataclass, field
from typing import Optional, Set


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
