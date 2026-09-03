"""The wireless-client scan model."""
from dataclasses import dataclass, field
from functools import cached_property
from typing import Dict, Optional, Set

from wifit3.wlan.fingerprint import Fingerprint, fingerprint as _fingerprint


@dataclass
class Client:
    """A wireless client (e.g. a phone or laptop)."""
    mac: str
    bssid: Optional[str] = None  # The AP it is currently connected to or probing for
    packets: int = 0
    probed_ssids: Set[str] = field(default_factory=set)  # SSIDs this client is actively searching for
    # AKM suite chosen by this client, read from the RSN IE in its (Re)Assoc Request. Latest-wins.
    akm_selected: Optional[int] = None
    # Smoothed RSSI per receiving card (card name -> dBm), written by WlanSink.
    signal_by_card: Dict[str, int] = field(default_factory=dict)

    @property
    def signal(self) -> int:
        """Strongest smoothed RSSI (dBm) across the cards that hear this client; -100 if none yet."""
        return max(self.signal_by_card.values(), default=-100)

    @cached_property
    def fingerprint(self) -> Optional[Fingerprint]:
        """OUI vendor for this client; looked up once, then cached on the instance."""
        return _fingerprint(self.mac)
