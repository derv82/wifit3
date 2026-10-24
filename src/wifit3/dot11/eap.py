"""EAP (802.1X EAPOL-Type-0) codes, method names, and a passive method lookup.

Spec only (RFC 3748): classify an EAP header's Code and Type, never the credential body.
"""
from typing import Optional

# EAP Codes (RFC 3748 section 4).
EAP_REQUEST = 1
EAP_RESPONSE = 2
EAP_SUCCESS = 3
EAP_FAILURE = 4

# Expanded Type: WSC rides it and stays on the EapolPacket path.
EAP_TYPE_EXPANDED = 0xFE

EAP_METHOD_NAMES = {
    1: "Identity",
    2: "Notification",
    3: "Nak",
    4: "MD5-Challenge",
    5: "OTP",
    6: "GTC",
    13: "EAP-TLS",
    17: "LEAP",
    18: "EAP-SIM",
    21: "EAP-TTLS",
    23: "EAP-AKA",
    25: "PEAP",
    26: "MSCHAPv2",
    43: "EAP-FAST",
    50: "EAP-AKA'",
    254: "Expanded",
    255: "Experimental",
}


def method_name(eap_type: Optional[int]) -> Optional[str]:
    """Human-readable EAP method for a Type byte, or None when absent or unassigned."""
    return None if eap_type is None else EAP_METHOD_NAMES.get(eap_type)
