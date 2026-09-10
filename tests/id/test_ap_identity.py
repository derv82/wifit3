import pytest

from wifit3.id.common import canonical_vendor
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


def test_priority_ladder_m1_beats_beacon_beats_oui():
    ap = AccessPoint(bssid="00:0a:eb:11:22:33")  # TP-Link OUI
    assert ap.identity.manufacturer == "TP-Link"
    assert ap.identity.summary == "TP-Link"

    # WSC Beacon overrides OUI
    ap.identity.set(IdSource.WSC_BEACON, IdKey.MANUFACTURER, "D-Link")
    ap.identity.set(IdSource.WSC_BEACON, IdKey.MODEL_NAME, "DIR-882")
    assert ap.identity.manufacturer == "D-Link"
    assert ap.identity.model == "DIR-882"
    assert ap.identity.summary == "D-Link DIR-882"

    # WSC M1 overrides Beacon
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
        "12345", "123456", "00000000", "none", "NONE", "default", "n/a", "N/A", "unknown", "???", "   ", "",
        "Wi-Fi Protected Setup Router", "wifi protected setup router", "WPS Router",
        "Ralink Wireless Access Point", "ralink wireless ap", "RalinkAPS",
        "Realtek Wireless Access Point", "realtek wireless ap",
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
    assert canonical_vendor("Ralink Technology, Corp.") == "Ralink"
    assert canonical_vendor("Nokia Solutions and Networks GmbH & Co. KG") == "Nokia"
    assert canonical_vendor("Hewlett-Packard Company") == "HP"
    assert canonical_vendor("HP Inc.") == "HP"
    assert canonical_vendor("CommScope, Inc.") == "CommScope"
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


def test_dummy_model_number_falls_back_to_device_name():
    ident = ApIdentity()
    ident.set(IdSource.WSC_BEACON, IdKey.MANUFACTURER, "Netgear")
    ident.set(IdSource.WSC_BEACON, IdKey.MODEL_NAME, "123456")
    ident.set(IdSource.WSC_BEACON, IdKey.DEVICE_NAME, "C6900")
    assert ident.manufacturer == "Netgear"
    assert ident.model == "C6900"
    assert ident.model_source == IdSource.WSC_BEACON
    assert ident.summary == "Netgear C6900"


def test_silicon_odm_yields_to_branded_oui():
    ident = ApIdentity()
    ident.set(IdSource.OUI, IdKey.MANUFACTURER, "Jensen Scandinavia AS")
    ident.set(IdSource.WSC_BEACON, IdKey.MANUFACTURER, "Ralink Technology, Corp.")
    ident.set(IdSource.WSC_BEACON, IdKey.MODEL_NAME, "Ralink Wireless Access Point")
    ident.set(IdSource.WSC_BEACON, IdKey.DEVICE_NAME, "Jensen of Scandinavia Air:Link 5000AC")

    assert ident.manufacturer == "Jensen"
    assert ident.manufacturer_source == IdSource.OUI
    assert ident.model == "Jensen of Scandinavia Air:Link 5000AC"
    assert ident.summary == "Jensen of Scandinavia Air:Link 5000AC"
    assert ident.get_source_value(IdKey.MANUFACTURER, IdSource.WSC_BEACON) == "Ralink"

    # When OUI is not present or is also an ODM, keep the ODM
    ident_odm = ApIdentity()
    ident_odm.set(IdSource.WSC_BEACON, IdKey.MANUFACTURER, "Realtek")
    ident_odm.set(IdSource.WSC_BEACON, IdKey.MODEL_NAME, "RTL8196")
    assert ident_odm.manufacturer == "Realtek"
    assert ident_odm.manufacturer_source == IdSource.WSC_BEACON
    assert ident_odm.summary == "Realtek RTL8196"


def test_device_type_formatting_in_summary():
    ident = ApIdentity(IdSource.WSC_BEACON, manufacturer="HP", model_name="LaserJet Pro", device_type="printer")
    assert ident.summary == "HP LaserJet Pro (printer)"

    camera_ident = ApIdentity(IdSource.WSC_BEACON, manufacturer="Nest", model_name="Cam", device_type="camera")
    assert camera_ident.summary == "Nest Cam (camera)"

    # Router/network infrastructure is None from parser, so no parenthetical tag
    router_ident = ApIdentity(IdSource.WSC_BEACON, manufacturer="Netgear", model_name="RAX10", device_type=None)
    assert router_ident.summary == "Netgear RAX10"


def test_init_with_attributes_requires_explicit_source():
    with pytest.raises(ValueError, match="IdSource must be specified"):
        ApIdentity(manufacturer="TP-Link")

    with pytest.raises(ValueError, match="IdSource must be specified"):
        ApIdentity(model_name="Archer AX10")


def test_init_with_explicit_m1_source():
    ident = ApIdentity(IdSource.WSC_M1, manufacturer="Netgear", model_name="RAX10")
    assert ident.manufacturer == "Netgear"
    assert ident.manufacturer_source == IdSource.WSC_M1
    assert ident.model == "RAX10"
    assert ident.model_source == IdSource.WSC_M1


def test_update_batches_multiple_attributes_for_source():
    ident = ApIdentity()
    ident.update(
        IdSource.WSC_BEACON,
        manufacturer="ASUSTeK Computer Inc.",
        model_name="RT-AC68U",
        model_number="AC1900",
        device_name="ASUS Router",
        device_type=None,
    )
    assert ident.manufacturer == "ASUS"
    assert ident.manufacturer_source == IdSource.WSC_BEACON
    assert ident.model_name == "RT-AC68U"
    assert ident.model_number == "AC1900"
    assert ident.device_name == "ASUS Router"
    assert ident.device_type is None
    assert ident.summary == "ASUS RT-AC68U"
