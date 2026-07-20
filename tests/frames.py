"""Test helper: build a typed parsed ``Packet`` from a partial parsed-frame dict.

Tests that feed a fake parsed frame to ``WlanInterface._on_frame_parsed`` wrap their dict
in ``pkt(...)``; it fills the base fields a test omits and dispatches to the right subclass
by ``d["type"]``. Dict keys use the parser's old internal dialect, including the
``eapol_``/``wep_`` prefixes, kept here as a compact construction shorthand for the many
call sites, even though the parser itself now builds the subclasses directly.
"""
from wifit3.dot11.packet import (
    AssocRequestPacket, BeaconPacket, EapolPacket, Packet, WepDataPacket,
)

_BASE = {
    "type_id": 0, "subtype_id": 0, "bssid": "00:00:00:00:00:00",
    "source": "00:00:00:00:00:00", "dest": "00:00:00:00:00:00",
    "to_ds": False, "from_ds": False, "rssi": -100, "raw": b"",
}

_BASE_FIELDS = (
    "type", "type_id", "subtype_id", "bssid", "source", "dest",
    "to_ds", "from_ds", "rssi", "raw", "ssid",
)


def pkt(d: dict) -> Packet:
    r = {**_BASE, **d}
    t = r["type"]
    if t in ("beacon", "probe_resp"):
        return BeaconPacket(**r)
    if t in ("assoc_req", "reassoc_req"):
        return AssocRequestPacket(**r)
    base = {k: r[k] for k in _BASE_FIELDS if k in r}
    if t == "eapol":
        return EapolPacket(
            **base,
            msg_num=r.get("eapol_msg_num", 0),
            replay_counter=r.get("eapol_replay_counter"),
            nonce=r.get("eapol_nonce"),
            mic=r.get("eapol_mic"),
            key_data_len=r.get("eapol_key_data_len", 0),
            payload=r.get("eapol_payload", b""),
            pmkid=r.get("eapol_pmkid"),
            akm=r.get("eapol_akm"),
            key_info=r.get("eapol_key_info"),
        )
    if t == "wep_data":
        return WepDataPacket(
            **base,
            iv=r.get("wep_iv"), keyid=r.get("wep_keyid"), cipher=r.get("wep_cipher"),
        )
    return Packet(**r)
