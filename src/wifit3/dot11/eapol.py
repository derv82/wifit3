"""802.11 Data-frame header + EAPOL-Key assembly (802.11 spec, no I/O).

EAPOL-Key multi-byte fields are network order.
"""
import struct

LLC_SNAP_EAPOL = bytes.fromhex("aaaa03000000888e")   # SNAP header + EtherType 0x888E (EAPOL)

MIC_LEN = 16
NONCE_LEN = 32
MIC_OFFSET = 81                 # MIC start within the 802.1X payload (hashcat -m 22000 reads it here)

_EAPOL_VERSION = 2
_EAPOL_TYPE_KEY = 3
_KEY_DESC_RSN = 2
_KEY_IV = bytes(16)
_KEY_RSC = bytes(8)
_KEY_ID = bytes(8)


def data_header(*, to_ds: bool, bssid: bytes, client: bytes) -> bytes:
    """24-byte Data header. to_ds (client->AP): addr1=bssid addr2=client addr3=bssid;
    else (AP->client): addr1=client addr2=bssid addr3=bssid. Duration + sequence zeroed."""
    if to_ds:
        return b"\x08\x01" + b"\x00\x00" + bssid + client + bssid + b"\x00\x00"
    return b"\x08\x02" + b"\x00\x00" + client + bssid + bssid + b"\x00\x00"


def eapol_key(*, key_info: int, key_len: int, replay: int, nonce: bytes,
              key_data: bytes = b"", mic: bytes = bytes(MIC_LEN)) -> bytes:
    """The 802.1X EAPOL-Key payload (version byte through Key Data). The MIC is computed
    over these exact bytes with the MIC field zeroed, then spliced back at ``MIC_OFFSET``."""
    if len(nonce) != NONCE_LEN:
        raise ValueError(f"nonce must be {NONCE_LEN} bytes, got {len(nonce)}")
    if len(mic) != MIC_LEN:
        raise ValueError(f"mic must be {MIC_LEN} bytes, got {len(mic)}")
    body = (
        bytes([_KEY_DESC_RSN])
        + struct.pack(">H", key_info)
        + struct.pack(">H", key_len)
        + struct.pack(">Q", replay)
        + nonce
        + _KEY_IV + _KEY_RSC + _KEY_ID
        + mic
        + struct.pack(">H", len(key_data))
        + key_data
    )
    return bytes([_EAPOL_VERSION, _EAPOL_TYPE_KEY]) + struct.pack(">H", len(body)) + body


def set_mic(payload: bytes, mic: bytes) -> bytes:
    if len(mic) != MIC_LEN:
        raise ValueError(f"mic must be {MIC_LEN} bytes, got {len(mic)}")
    return payload[:MIC_OFFSET] + mic + payload[MIC_OFFSET + MIC_LEN:]
