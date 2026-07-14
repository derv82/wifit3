"""The WPS Association vendor IE — announces registrar (PIN) or enrollee (PBC)
intent in the Association Request so a WPS AP starts the EAP-WSC exchange.

Kept out of the generic ``auth_assoc.Association`` (which knows no protocol above the
802.11 skeleton): the assoc engine appends whatever ``assoc_trailer_ies`` it is handed,
and WPS callers hand it ``wps_assoc_ie(...)``.
"""
from __future__ import annotations

# tag 221 vendor IE: OUI 00:50:F2 type 04 (WPS), Version=1.0, Request Type byte.
# (reaver src/builder.c WPS_REGISTRAR_TAG ends in 02 = Registrar.)
_WPS_IE_PREFIX = bytes.fromhex("0050f204104a000110103a0001")
WPS_REQ_ENROLLEE = 0x01
WPS_REQ_REGISTRAR = 0x02


def wps_assoc_ie(request_type: int) -> bytes:
    """The complete WPS vendor IE (tag 0xDD + len + body) for an Assoc Request."""
    body = _WPS_IE_PREFIX + bytes([request_type])
    return bytes([0xDD, len(body)]) + body
