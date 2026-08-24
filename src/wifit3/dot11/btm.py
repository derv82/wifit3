"""802.11v BSS Transition Management (BTM) Request builder (IEEE 802.11-2020 spec).

A WNM Action frame (category 10, action 7) an AP sends to steer one STA toward a named candidate
BSS: unicast, robust (unforgeable) once PMF is negotiated.
"""
from typing import Optional

from wifit3.dot11.chan import channel_operating_class

_SUBTYPE_ACTION = 0x0D            # mgmt subtype 13 → FC first byte 0xD0
_CAT_WNM = 0x0A                   # Action category 10 (WNM)
_WNM_BTM_REQUEST = 0x07          # WNM action 7 (BSS Transition Management Request)
_ELEMID_NEIGHBOR_REPORT = 0x34   # element ID 52
_SUBELEM_CANDIDATE_PREFERENCE = 0x03

# BTM Request Mode bits (802.11-2020 Figure 9-924).
BTM_PREFERRED_CANDIDATE_LIST = 0x01
BTM_ABRIDGED = 0x02
BTM_DISASSOC_IMMINENT = 0x04
BTM_BSS_TERMINATION_INCLUDED = 0x08
BTM_ESS_DISASSOC_IMMINENT = 0x10

# AP Reachability = Reachable (bits 0-1 = 3). Security bit (2) left clear = "different security from
# this AP, negotiate": the truthful signal for a twin whose security posture may differ.
_BSSID_INFO_REACHABLE = 0x00000003
_PHY_TYPE_HT = 7                 # dot11PHYType: ht(7); 5 GHz VHT is 9 (pass phy_type= to override)


def neighbor_report_ie(bssid: bytes, operating_class: int, channel: int, *,
                       bssid_info: int = _BSSID_INFO_REACHABLE,
                       phy_type: int = _PHY_TYPE_HT,
                       preference: Optional[int] = None) -> bytes:
    """Neighbor Report element (ID 52) + optional Candidate Preference subelement (ID 3, 1..255)."""
    if len(bssid) != 6:
        raise ValueError(f"bssid must be 6 bytes, got {len(bssid)}")
    body = bssid + bssid_info.to_bytes(4, "little") + bytes([operating_class & 0xFF,
                                                             channel & 0xFF, phy_type & 0xFF])
    if preference is not None:
        body += bytes([_SUBELEM_CANDIDATE_PREFERENCE, 0x01, preference & 0xFF])
    return bytes([_ELEMID_NEIGHBOR_REPORT, len(body)]) + body


def build_btm_request(a1: bytes, a2: bytes, a3: bytes, *,
                      candidate_bssid: bytes, candidate_channel: int,
                      operating_class: Optional[int] = None,
                      dialog_token: int = 1, disassoc_imminent: bool = True,
                      abridged: bool = True, disassoc_timer: int = 0,
                      validity_interval: int = 255, preference: int = 255,
                      phy_type: int = _PHY_TYPE_HT, duration: bytes = b"\x00\x00") -> bytes:
    """BTM Request MPDU (WNM category 10, action 7) steering client ``a1`` to one candidate BSS."""
    oc = operating_class if operating_class is not None else channel_operating_class(candidate_channel)
    mode = BTM_PREFERRED_CANDIDATE_LIST
    if abridged:
        mode |= BTM_ABRIDGED
    if disassoc_imminent:
        mode |= BTM_DISASSOC_IMMINENT
    candidate = neighbor_report_ie(candidate_bssid, oc, candidate_channel,
                                   phy_type=phy_type, preference=preference)
    header = bytes([_SUBTYPE_ACTION << 4, 0x00]) + duration + a1 + a2 + a3 + b"\x00\x00"
    body = (bytes([_CAT_WNM, _WNM_BTM_REQUEST, dialog_token & 0xFF, mode])
            + disassoc_timer.to_bytes(2, "little") + bytes([validity_interval & 0xFF]) + candidate)
    return header + body
