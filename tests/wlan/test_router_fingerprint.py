from wifit3.models import AccessPoint
from wifit3.wlan.fingerprinting.router import RouterClaim, RouterEvidence, fingerprint_router
from wifit3.wlan.fingerprinting.router_helpers import canonical_vendor
from wifit3.wlan.fingerprinting.router_rules import passive_wps_model_rule


def test_oui_only_is_possible_vendor_not_exact_router():
    fp = AccessPoint(bssid="00:00:0b:aa:bb:cc").router_fingerprint
    assert fp is not None
    assert fp.vendor == "Matrix"
    assert fp.model is None
    assert fp.vendor_confidence <= 0.45
    assert fp.model_confidence == 0.0
    assert fp.kind is None
    assert fp.kind_confidence == 0.0
    assert fp.confidence == fp.vendor_confidence
    assert fp.label == "Possible Matrix"


def test_passive_wps_manufacturer_and_model_make_stronger_router_fingerprint():
    ap = AccessPoint(
        bssid="02:00:00:00:00:01",
        wps_manufacturer="MikroTik",
        wps_model_name="hAP ac²",
        wps_device_name="Office AP",
    )
    fp = ap.router_fingerprint
    assert fp is not None
    assert fp.vendor == "MikroTik"
    assert fp.model == "hAP ac²"
    assert fp.vendor_confidence == 0.99
    assert fp.model_confidence == 0.99
    assert fp.kind_confidence == 0.99
    assert fp.confidence == 0.99
    assert fp.label == "MikroTik hAP ac² router"
    assert {e.name for e in fp.evidence} >= {"manufacturer", "model", "device_name"}


def test_rules_are_pluggable_for_router_specific_checks():
    def mikrotik_tool_rule(ap):
        evidence = RouterEvidence("mikrotik.mac_winbox", "mac_server", "reachable", 0.92)
        return (
            RouterClaim("vendor", "MikroTik", 0.92, (evidence,)),
            RouterClaim("kind", "router", 0.92, (evidence,)),
        )

    ap = AccessPoint(bssid="02:00:00:00:00:01")
    fp = fingerprint_router(ap, rules=(mikrotik_tool_rule,))
    assert fp is not None
    assert fp.vendor == "MikroTik"
    assert fp.vendor_confidence == 0.92
    assert fp.model_confidence == 0.0
    assert fp.confidence == 0.92
    assert fp.label == "MikroTik router"
    assert fp.evidence[0].source == "mikrotik.mac_winbox"


def test_active_probe_claims_are_part_of_router_fingerprint():
    evidence = RouterEvidence("mikrotik.mac_winbox", "reachable", "true", 0.99, passive=False)
    ap = AccessPoint(
        bssid="02:00:00:00:00:01",
        router_claims=(
            RouterClaim("vendor", "MikroTik", 0.99, (evidence,)),
            RouterClaim("kind", "router", 0.99, (evidence,)),
        ),
    )
    fp = ap.router_fingerprint
    assert fp is not None
    assert fp.vendor == "MikroTik"
    assert fp.vendor_confidence == 0.99
    assert fp.kind == "router"
    assert fp.kind_confidence == 0.99
    assert fp.label == "MikroTik router"
    assert fp.evidence[0].passive is False


def test_o2_smartbox_pattern_sets_brand_without_replacing_vendor():
    ap = AccessPoint(
        bssid="02:00:00:00:00:01",
        wps_manufacturer="Kaon Group",
        wps_device_name="O2SMARTBOX",
    )
    fp = ap.router_fingerprint
    assert fp is not None
    assert fp.brand == "O2"
    assert fp.brand_confidence == 0.95
    assert fp.vendor == "Kaon"
    assert fp.vendor_confidence == 0.99
    assert fp.label == "O2 router"


def test_o2_smartbox_ssid_pattern_sets_brand():
    fp = AccessPoint(bssid="02:00:00:00:00:01", ssid="O2SMARTBOX-123456").router_fingerprint
    assert fp is not None
    assert fp.brand == "O2"
    assert fp.vendor is None
    assert fp.label == "O2 router"


def test_vodafone_ssid_clue_is_weak_because_ssids_are_renamable():
    fp = AccessPoint(bssid="02:00:00:00:00:01", ssid="Vodafone-123456").router_fingerprint
    assert fp is not None
    assert fp.brand == "Vodafone"
    assert round(fp.brand_confidence, 2) == 0.30
    assert fp.vendor is None
    assert fp.kind is None
    assert fp.label == "Possible Vodafone"


def test_celeno_manufacturer_with_vodafone_ssid_sets_brand():
    fp = AccessPoint(
        bssid="02:00:00:00:00:01",
        ssid="Vodafone-123456",
        wps_manufacturer="Celeno",
    ).router_fingerprint
    assert fp is not None
    assert fp.brand == "Vodafone"
    assert fp.brand_confidence == 0.70
    assert fp.vendor == "Celeno"
    assert fp.vendor_confidence == 0.99
    assert fp.kind == "router"
    assert fp.kind_confidence == 0.99
    assert fp.label == "Possible Vodafone router"
    assert any(e.source == "brand.vodafone" and e.name == "ssid" and e.value == "Vodafone-123456"
               for e in fp.evidence)


def test_brand_and_hardware_vendor_are_separate_claims():
    def isp_brand_rule(ap):
        brand_ev = RouterEvidence("ssid.pattern", "brand", "O2", 0.82)
        vendor_ev = RouterEvidence("oui.vendor", "vendor", "Kaon", 0.99)
        return (
            RouterClaim("brand", "O2", 0.82, (brand_ev,)),
            RouterClaim("vendor", "Kaon", 0.99, (vendor_ev,)),
            RouterClaim("kind", "router", 0.99, (vendor_ev,)),
        )

    fp = fingerprint_router(AccessPoint(bssid="02:00:00:00:00:01"), rules=(isp_brand_rule,))
    assert fp is not None
    assert fp.brand == "O2"
    assert fp.brand_confidence == 0.82
    assert fp.vendor == "Kaon"
    assert fp.vendor_confidence == 0.99
    assert fp.confidence == 0.82
    assert fp.label == "Likely O2 router"


def test_low_confidence_model_claim_does_not_enter_headline_label():
    def weak_model_rule(ap):
        evidence = RouterEvidence("ssid.pattern", "model", "hAP ac²", 0.40)
        return (
            RouterClaim("vendor", "MikroTik", 0.92, (evidence,)),
            RouterClaim("model", "hAP ac²", 0.40, (evidence,)),
        )

    fp = fingerprint_router(AccessPoint(bssid="02:00:00:00:00:01"), rules=(weak_model_rule,))
    assert fp is not None
    assert fp.vendor_confidence == 0.92
    assert fp.model == "hAP ac²"
    assert fp.model_confidence == 0.40
    assert fp.kind is None
    assert fp.label == "MikroTik"


def test_vendor_names_are_canonicalized():
    assert canonical_vendor("Tp-Link Technologies") == "TP-Link"
    assert canonical_vendor("TP-Link") == "TP-Link"
    assert canonical_vendor("AVM Audiovisuelles Marketing und Computersysteme") == "AVM"
    assert canonical_vendor("AMV Audio") == "AMV"
    assert canonical_vendor("Kaon Group") == "Kaon"
    assert canonical_vendor("Kaon") == "Kaon"
    assert canonical_vendor("Seiko Epson") == "Epson"
    assert canonical_vendor("Apple, Inc.") == "Apple"


def test_apple_ssid_clue_is_weak_because_ssids_are_renamable():
    fp = AccessPoint(bssid="02:00:00:00:00:01", ssid="Alice’s iPhone").router_fingerprint
    assert fp is not None
    assert fp.brand == "Apple"
    assert fp.brand_confidence == 0.40
    assert fp.kind == "hotspot"
    assert fp.kind_confidence == 0.40
    assert fp.vendor is None
    assert fp.label == "Possible Apple hotspot"


def test_apple_oui_identifies_likely_hotspot():
    fp = AccessPoint(bssid="00:03:93:11:22:33", ssid="Personal Hotspot").router_fingerprint
    assert fp is not None
    assert fp.vendor == "Apple"
    assert round(fp.vendor_confidence, 2) == 0.30
    assert fp.brand == "Apple"
    assert fp.brand_confidence == 0.85
    assert fp.kind == "hotspot"
    assert fp.kind_confidence == 0.85
    assert fp.label == "Likely Apple hotspot"


def test_apple_oui_and_iphone_ssid_strengthen_hotspot_identity():
    fp = AccessPoint(bssid="00:03:93:11:22:33", ssid="iPad").router_fingerprint
    assert fp is not None
    assert fp.brand == "Apple"
    assert fp.brand_confidence == 0.91
    assert fp.kind == "hotspot"
    assert fp.kind_confidence == 0.91
    assert fp.label == "Apple hotspot"


def test_oui_vendor_rule_uses_canonical_vendor_name():
    tplink = AccessPoint(bssid="00:0a:eb:11:22:33").router_fingerprint
    avm = AccessPoint(bssid="0c:72:74:11:22:33").router_fingerprint
    assert tplink is not None and tplink.vendor == "TP-Link"
    assert avm is not None and avm.vendor == "AVM"


def test_tplink_oui_weakly_identifies_router_type():
    fp = AccessPoint(bssid="00:0a:eb:11:22:33").router_fingerprint
    assert fp is not None
    assert fp.vendor == "TP-Link"
    assert round(fp.vendor_confidence, 2) == 0.30
    assert fp.kind == "router"
    assert round(fp.kind_confidence, 2) == 0.30
    assert fp.model is None
    assert fp.label == "Possible TP-Link router"
    assert any(e.source == "oui.tplink" and e.name == "kind" and e.value == "router"
               for e in fp.evidence)


def test_ubiquiti_oui_weakly_identifies_router_type():
    fp = AccessPoint(bssid="00:15:6d:11:22:33").router_fingerprint
    assert fp is not None
    assert fp.vendor == "Ubiquiti"
    assert round(fp.vendor_confidence, 2) == 0.30
    assert fp.kind == "router"
    assert round(fp.kind_confidence, 2) == 0.30
    assert fp.model is None
    assert fp.label == "Possible Ubiquiti router"
    assert any(e.source == "oui.ubiquiti" and e.name == "kind" and e.value == "router"
               for e in fp.evidence)


def test_epson_oui_identifies_likely_printer_type():
    fp = AccessPoint(bssid="00:00:48:11:22:33").router_fingerprint
    assert fp is not None
    assert fp.vendor == "Epson"
    assert round(fp.vendor_confidence, 2) == 0.30
    assert fp.kind == "printer"
    assert round(fp.kind_confidence, 2) == 0.90
    assert fp.model is None
    assert fp.label == "Possible Epson printer"
    assert any(e.source == "oui.epson" and e.name == "kind" and e.value == "printer"
               for e in fp.evidence)


def test_epson_direct_ssid_identifies_likely_printer():
    fp = AccessPoint(bssid="02:00:00:00:00:01", ssid="DIRECT-AB-EPSON-XP-4100").router_fingerprint
    assert fp is not None
    assert fp.vendor == "Epson"
    assert round(fp.vendor_confidence, 2) == 0.30
    assert fp.kind == "printer"
    assert round(fp.kind_confidence, 2) == 0.30
    assert fp.label == "Possible Epson printer"
    assert any(e.source == "ssid.epson" and e.name == "ssid"
               and e.value == "DIRECT-AB-EPSON-XP-4100" for e in fp.evidence)


def test_wps_m1_fields_use_m1_evidence_source():
    ap = AccessPoint(
        bssid="02:00:00:00:00:01",
        wps_manufacturer="RalinkAPS",
        wps_model_name="Generic AP",
        wps_m1_manufacturer="Netgear",
        wps_m1_model_name="RAX10",
    )
    fp = ap.router_fingerprint
    assert fp is not None
    assert fp.vendor == "Netgear"
    assert fp.model == "RAX10"
    assert any(e.source == "wps.m1" and e.name == "manufacturer" and e.value == "Netgear"
               for e in fp.evidence)
    assert any(e.source == "wps.m1" and e.name == "model" and e.value == "RAX10"
               for e in fp.evidence)


def test_wps_manufacturer_uses_canonical_vendor_name():
    fp = AccessPoint(
        bssid="02:00:00:00:00:01",
        wps_manufacturer="Tp-Link Technologies",
    ).router_fingerprint
    assert fp is not None
    assert fp.vendor == "TP-Link"
    assert fp.label == "TP-Link router"


def test_specific_model_claim_can_imply_vendor():
    def known_model_rule(ap):
        evidence = RouterEvidence("rule.known_model", "model", "CCR2004", 0.97)
        return (RouterClaim("model", "CCR2004", 0.97, (evidence,), vendor="MikroTik"),)

    fp = fingerprint_router(AccessPoint(bssid="02:00:00:00:00:01"), rules=(known_model_rule,))
    assert fp is not None
    assert fp.vendor == "MikroTik"
    assert fp.vendor_confidence == 0.97
    assert fp.model == "CCR2004"
    assert fp.model_confidence == 0.97
    assert fp.kind is None
    assert fp.label == "MikroTik CCR2004"


def test_identify_and_distinguish_rules_can_run_separately():
    ap = AccessPoint(
        bssid="02:00:00:00:00:01",
        wps_manufacturer="MikroTik",
        wps_model_name="hAP ac²",
    )
    identify_only = fingerprint_router(ap, distinguish_rules=())
    full = fingerprint_router(ap)
    assert identify_only is not None and identify_only.vendor == "MikroTik"
    assert identify_only.model is None
    assert full is not None and full.model == "hAP ac²"


def test_wps_model_number_is_model_fallback():
    ap = AccessPoint(bssid="02:00:00:00:00:01", wps_manufacturer="Acme", wps_model_number="R9000")
    claims = list(passive_wps_model_rule(ap))
    model_claim = next(claim for claim in claims if claim.name == "model")
    assert model_claim.value == "R9000"
