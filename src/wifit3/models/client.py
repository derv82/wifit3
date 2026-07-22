"""The wireless-client scan model."""
from dataclasses import dataclass, field
from typing import Dict, Optional, Set


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
    # Smoothed RSSI per receiving card (card name -> dBm). `signal` is the strongest of these;
    # the scalar `signal` field above is a transitional mirror kept in sync by WlanSink.
    signal_by_card: Dict[str, int] = field(default_factory=dict)
