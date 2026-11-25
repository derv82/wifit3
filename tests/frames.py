"""Test helper: build a typed parsed ``Packet`` from a partial parsed-frame dict.

Tests that feed a fake parsed frame to ``WlanInterface._on_frame_parsed`` wrap their dict
in ``pkt(...)``; it fills the base fields a test omits and dispatches to the right subclass
by ``d["type"]``. Dict keys use the parser's old internal dialect, including the
``eapol_``/``wep_`` prefixes, kept here as a compact construction shorthand for the many
call sites, even though the parser itself now builds the subclasses directly.
"""
from wifit3.dot11.ie import Elements
from wifit3.dot11.packet import (
    AssocRequestPacket, BeaconPacket, EapolPacket, Packet, ProbeReqPacket, ProbeResponsePacket,
    WepDataPacket,
)
from wifit3.dot11.rsn import RsnElement, WpaElement
from wifit3.dot11.wsc.element import WpsElement

_BASE = {
    "type_id": 0, "subtype_id": 0, "bssid": "00:00:00:00:00:00",
    "source": "00:00:00:00:00:00", "dest": "00:00:00:00:00:00",
    "to_ds": False, "from_ds": False, "rssi": -100, "raw": b"",
}

_BASE_FIELDS = (
    "type", "type_id", "subtype_id", "bssid", "source", "dest",
    "to_ds", "from_ds", "rssi", "raw", "ssid",
)


def _elements(r: dict) -> Elements:
    """The flat beacon fields a test passes, as decoded Elements: the AKM fields go into an RSN
    element when ``rsn_valid`` or a PMF flag is set (only RSN carries PMF), else into a WPA element."""
    akms = tuple(r.get("akms", ()))
    akm_suites = tuple(r.get("akm_suites", ()))
    rsn = None
    wpa = None
    if r.get("rsn_valid") or r.get("pmf_capable") or r.get("pmf_required"):
        rsn = RsnElement(
            group_cipher_suite=None, pairwise_cipher=r.get("pairwise_cipher"), pairwise_cipher_suites=(),
            pairwise_cipher_count=0, akms=akms, akm_suites=akm_suites, akm_suite_count=len(akm_suites),
            vendor_akm_offered=r.get("vendor_akm_offered", False), pmf_capable=r.get("pmf_capable", False),
            pmf_required=r.get("pmf_required", False), group_management_cipher_suite=None)
    elif akms or akm_suites or r.get("vendor_akm_offered") or r.get("pairwise_cipher"):
        wpa = WpaElement(pairwise_cipher=r.get("pairwise_cipher"), akms=akms, akm_suites=akm_suites,
                         vendor_akm_offered=r.get("vendor_akm_offered", False))
    wps = None
    if r.get("wps"):
        wps = WpsElement(
            locked=r.get("wps_locked", False), state=r.get("wps_state"), version=r.get("wps_version"),
            config_methods=r.get("wps_config_methods", 0), device_password_id=r.get("wps_device_password_id"),
            selected_registrar=r.get("wps_selected_registrar", False), manufacturer=r.get("wsc_manufacturer"),
            model_name=r.get("wsc_model_name"), model_number=r.get("wsc_model_number"),
            device_name=r.get("wsc_device_name"), device_type=r.get("wsc_device_type"))
    return Elements(
        ssid=r.get("ssid"), ssid_bytes=r.get("ssid_bytes"), channel=r.get("channel"),
        encryption=r.get("encryption", "OPEN"), rsn=rsn, rsn_element_raw=r.get("rsn_ie_raw"), wpa=wpa,
        wps=wps, chipset=r.get("chipset"), beacon_protection=r.get("beacon_protection", False),
        wpa3=r.get("wpa3", False), transition_mode=r.get("transition_mode", False))


def pkt(d: dict) -> Packet:
    r = {**_BASE, **d}
    t = r["type"]
    if t in ("beacon", "probe_resp"):
        base = {k: r[k] for k in _BASE_FIELDS + ("ssid_bytes",) if k in r}
        if t == "beacon":
            return BeaconPacket(**base, elements=_elements(r))
        return ProbeResponsePacket(**base, elements=_elements(r))
    if t in ("assoc_req", "reassoc_req"):
        return AssocRequestPacket(**r)
    if t == "probe_req":
        return ProbeReqPacket(**r)
    base = {k: r[k] for k in _BASE_FIELDS if k in r}
    if t == "eapol":
        return EapolPacket(
            **base,
            msg_num=r.get("eapol_msg_num", 0),
            replay_counter=r.get("eapol_replay_counter"),
            nonce=r.get("eapol_nonce"),
            mic=r.get("eapol_mic"),
            payload=r.get("eapol_payload", b""),
            pmkid=r.get("eapol_pmkid"),
            akm=r.get("eapol_akm"),
            key_info=r.get("eapol_key_info"),
        )
    if t == "wep_data":
        return WepDataPacket(
            **base,
            iv=r.get("wep_iv"), cipher=r.get("wep_cipher"),
        )
    return Packet(**r)
