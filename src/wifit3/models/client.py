"""The wireless-client scan model."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from wifit3.id import Fingerprint


@dataclass
class Client:
    """A wireless client (e.g. a phone or laptop)."""
    mac: str
    bssid: Optional[str] = None  # AP of its latest (Re)Association Request or data frame
    packets: int = 0
    # AKM from the latest (Re)Association Request to `bssid`; cleared when that association ends.
    akm_selected: Optional[int] = None
    # Smoothed RSSI per receiving card (card name -> dBm), written by WlanSink.
    signal_by_card: Dict[str, int] = field(default_factory=dict)
    signal_history: Dict[str, deque[int]] = field(default_factory=dict, repr=False)

    @property
    def signal(self) -> int:
        """Strongest smoothed RSSI (dBm) across the cards that hear this client; -100 if none yet."""
        return max(self.signal_by_card.values(), default=-100)

    @cached_property
    def fingerprint(self) -> Optional[Fingerprint]:
        """OUI vendor for this client; looked up once, then cached on the instance."""
        from wifit3.id import fingerprint_client
        return fingerprint_client(self.mac)
