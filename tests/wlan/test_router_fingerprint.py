from __future__ import annotations

from wifit3.id.router_helpers import canonical_vendor
from wifit3.models import AccessPoint, ApIdentity, IdKey, IdSource
from wifit3.models.identity import clean_text


def test_oui_seeded_on_access_point_creation():
    ap = AccessPoint(bssid="00:00:0b:aa:bb:cc")
    assert ap.identity.manufacturer == "Matrix"
    assert ap.identity.model is None
    assert ap.identity.summary == "Matrix"
    val, src = ap.identity.get(IdKey.MANUFACTURER)
    assert val == "Matrix"
    assert src == IdSource.OUI

    local_ap = AccessPoint(bssid="02:00:00:00:00:01")
    assert local_ap.identity.manufacturer is None
    assert local_ap.identity.summary == ""


def test_priority_ladder_m1_beats_probe_beats_beacon_beats_oui():
    ap = AccessPoint(bssid="00:0a:eb:11:22:33")  # TP-Link OUI
    assert ap.identity.manufacturer == "TP-Link"
    assert ap.identity.summary == "TP-Link"

    # WSC Beacon overrides OUI
    ap.identity.set(IdSource.WSC_BEACON, IdKey.MANUFACTURER, "Realtek")
    ap.identity.set(IdSource.WSC_BEACON, IdKey.MODEL_NAME, "RTL8196")
    assert ap.identity.manufacturer == "Realtek"
    assert ap.identity.model == "RTL8196"
    assert ap.identity.summary == "Realtek RTL8196"

    # Active Probe overrides Beacon
    ap.identity.set(IdSource.WINBOX_PROBE, IdKey.MANUFACTURER, "MikroTik")
    assert ap.identity.manufacturer == "MikroTik"

    # WSC M1 overrides Active Probe
    ap.identity.set(IdSource.WSC_M1, IdKey.MANUFACTURER, "Netgear")
    ap.identity.set(IdSource.WSC_M1, IdKey.MODEL_NAME, "RAX10")
    assert ap.identity.manufacturer == "Netgear"
    assert ap.identity.model == "RAX10"
    assert ap.identity.summary == "Netgear RAX10"

    # Lower-priority source does not override higher-priority source
    ap.identity.set(IdSource.WSC_BEACON, IdKey.MANUFACTURER, "Late Beacon")
    assert ap.identity.manufacturer == "Netgear"


def test_historical_provenance_preserved():
    ap = AccessPoint(bssid="00:0a:eb:11:22:33")  # TP-Link OUI
    ap.identity.set(IdSource.WSC_BEACON, IdKey.MANUFACTURER, "Realtek")
    ap.identity.set(IdSource.WSC_M1, IdKey.MANUFACTURER, "Netgear")

    assert ap.identity.manufacturer == "Netgear"
    assert ap.identity.get_source_value(IdKey.MANUFACTURER, IdSource.OUI) == "TP-Link"
    assert ap.identity.get_source_value(IdKey.MANUFACTURER, IdSource.WSC_BEACON) == "Realtek"
    assert ap.identity.get_source_value(IdKey.MANUFACTURER, IdSource.WSC_M1) == "Netgear"


def test_clean_text_rejects_dummy_strings():
    for dummy in (
        "12345", "00000000", "none", "NONE", "default", "n/a", "N/A", "unknown", "???", "   ", "",
        "Wi-Fi Protected Setup Router", "wifi protected setup router", "WPS Router",
    ):
        assert clean_text(dummy) is None

    ident = ApIdentity()
    ident.set(IdSource.WSC_BEACON, IdKey.MODEL_NAME, "12345")
    ident.set(IdSource.WSC_BEACON, IdKey.MANUFACTURER, "none")
    assert ident.model is None
    assert ident.manufacturer is None
    assert ident.summary == ""


def test_vendor_names_are_canonicalized():
    assert canonical_vendor("ASUSTeK Computer Inc.") == "ASUS"
    assert canonical_vendor("ASUSTek COMPUTER") == "ASUS"
    assert canonical_vendor("Netgear, Inc.") == "Netgear"
    assert canonical_vendor("Cisco Systems, Inc.") == "Cisco"
    assert canonical_vendor("Tp-Link Technologies") == "TP-Link"
    assert canonical_vendor("TP-Link") == "TP-Link"
    assert canonical_vendor("AVM Audiovisuelles Marketing und Computersysteme") == "AVM"
    assert canonical_vendor("AMV Audio") == "AMV"
    assert canonical_vendor("Kaon Group") == "Kaon"
    assert canonical_vendor("Kaon") == "Kaon"
    assert canonical_vendor("Routerboard.com") == "MikroTik"
    assert canonical_vendor("MikroTik") == "MikroTik"
    assert canonical_vendor("Seiko Epson") == "Epson"
    assert canonical_vendor("Apple, Inc.") == "Apple"
    assert canonical_vendor("Custom Vendor LLC") == "Custom Vendor LLC"


def test_model_resolution_and_fallback():
    ident = ApIdentity()
    ident.set(IdSource.WSC_BEACON, IdKey.MODEL_NUMBER, "R9000")
    assert ident.model == "R9000"

    ident.set(IdSource.WSC_BEACON, IdKey.MODEL_NAME, "Nighthawk X10")
    assert ident.model == "Nighthawk X10"


def test_model_resolution_falls_back_to_device_name_when_model_is_none():
    ident = ApIdentity()
    ident.set(IdSource.WSC_BEACON, IdKey.MANUFACTURER, "ASUSTeK Computer Inc.")
    ident.set(IdSource.WSC_BEACON, IdKey.MODEL_NAME, "Wi-Fi Protected Setup Router")
    ident.set(IdSource.WSC_BEACON, IdKey.DEVICE_NAME, "RT-AC66U")
    assert ident.manufacturer == "ASUS"
    assert ident.model_name is None
    assert ident.device_name == "RT-AC66U"
    assert ident.model == "RT-AC66U"
    assert ident.model_source == IdSource.WSC_BEACON
    assert ident.summary == "ASUS RT-AC66U"


def test_model_resolution_falls_back_to_device_name_when_model_equals_manufacturer():
    ident = ApIdentity()
    ident.set(IdSource.WSC_M1, IdKey.MANUFACTURER, "Netgear")
    ident.set(IdSource.WSC_M1, IdKey.MODEL_NAME, "Netgear")
    ident.set(IdSource.WSC_M1, IdKey.DEVICE_NAME, "C3700-100NAS")
    assert ident.manufacturer == "Netgear"
    assert ident.model == "C3700-100NAS"
    assert ident.model_source == IdSource.WSC_M1
    assert ident.summary == "Netgear C3700-100NAS"


def test_identity_summary_formatting():
    ident = ApIdentity()
    assert ident.summary == ""

    ident.set(IdSource.WSC_BEACON, IdKey.MANUFACTURER, "TP-Link")
    assert ident.summary == "TP-Link"

    ident.set(IdSource.WSC_BEACON, IdKey.MODEL_NAME, "Archer AX10")
    assert ident.summary == "TP-Link Archer AX10"

    # Avoid duplicating vendor when model already includes it
    ident2 = ApIdentity()
    ident2.set(IdSource.WSC_BEACON, IdKey.MANUFACTURER, "Netgear")
    ident2.set(IdSource.WSC_BEACON, IdKey.MODEL_NAME, "Netgear Nighthawk")
    assert ident2.summary == "Netgear Nighthawk"

    # Model only when manufacturer missing
    ident3 = ApIdentity()
    ident3.set(IdSource.WSC_BEACON, IdKey.MODEL_NAME, "Archer AX10")
    assert ident3.summary == "Archer AX10"
