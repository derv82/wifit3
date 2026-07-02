"""Test helper: build a typed parsed ``Packet`` from a partial parsed-frame dict.

T2 turned the parser output from a dict into a ``Packet`` hierarchy. Tests that feed a fake
parsed frame to ``WlanInterface._on_frame_parsed`` wrap their existing dict in ``pkt(...)``;
it fills the base fields a test omits and dispatches to the right subclass by ``d["type"]``.
Dict keys are exactly the parser's (including the ``eapol_``/``wep_`` prefixes).
"""
from wifit3.wlan.packet import Packet, WlanFrameParser

_BASE = {
    "type_id": 0, "subtype_id": 0, "bssid": "00:00:00:00:00:00",
    "source": "00:00:00:00:00:00", "dest": "00:00:00:00:00:00",
    "to_ds": False, "from_ds": False, "rssi": -100, "raw": b"",
}


def pkt(d: dict) -> Packet:
    return WlanFrameParser._to_packet({**_BASE, **d})
