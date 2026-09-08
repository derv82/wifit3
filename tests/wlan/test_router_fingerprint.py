from wifit3.models import AccessPoint
from wifit3.wlan.fingerprinting.router import RouterClaim, RouterEvidence, fingerprint_router
from wifit3.wlan.fingerprinting.router_helpers import canonical_vendor
from wifit3.wlan.fingerprinting.router_rules import wifi_generation_rule, wps_model_rule


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
    assert fp.kind is None
    assert fp.kind_confidence == 0.0
    assert fp.confidence == 0.99
    assert fp.label == "MikroTik hAP ac²"
    assert {e.name for e in fp.evidence} >= {"manufacturer", "model", "device_name"}


def test_wps_primary_device_type_identifies_router_kind():
    fp = AccessPoint(
        bssid="02:00:00:00:00:01",
        wps_primary_device_type="network_infrastructure",
    ).router_fingerprint
    assert fp is not None
    assert fp.kind == "router"
    assert fp.kind_confidence == 0.99
    assert fp.label == "router"
    assert any(e.source == "wps.passive" and e.name == "primary_device_type"
               and e.value == "network_infrastructure" for e in fp.evidence)


def test_wps_m1_primary_device_type_identifies_printer_kind():
    fp = AccessPoint(
        bssid="02:00:00:00:00:01",
        wps_m1_primary_device_type="printer",
    ).router_fingerprint
    assert fp is not None
    assert fp.kind == "printer"
    assert fp.kind_confidence == 0.99
    assert fp.label == "printer"
    assert any(e.source == "wps.m1" and e.name == "primary_device_type" and e.value == "printer"
               for e in fp.evidence)


def test_wifi_generation_alone_is_not_router_identity():
    ap = AccessPoint(bssid="02:00:00:00:00:01", wifi_generation=7)
    assert ap.router_fingerprint is None

    claims = list(wifi_generation_rule(ap))
    assert len(claims) == 1
    assert claims[0].name == "wifi_generation"
    assert claims[0].value == "7"
    assert claims[0].evidence[0].source == "wifi.generation"
    assert claims[0].evidence[0].value == "Wi-Fi 7"


def test_wifi_generation_claim_is_kept_with_router_identity():
    fp = AccessPoint(bssid="00:0a:eb:11:22:33", wifi_generation=5).router_fingerprint
    assert fp is not None
    assert fp.vendor == "TP-Link"
    assert fp.wifi_generation == 5
    assert fp.wifi_generation_confidence == 0.99
    assert any(e.source == "wifi.generation" and e.name == "generation" and e.value == "Wi-Fi 5"
               for e in fp.evidence)


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


def test_strong_conflicting_claims_flag_possible_spoof_without_dropping_evidence():
    mikrotik_evidence = RouterEvidence("mikrotik.mac_winbox", "reachable", "true", 0.99, passive=False)
    ubnt_evidence = RouterEvidence("ubnt.discovery", "reachable", "true", 0.99, passive=False)
    ap = AccessPoint(
        bssid="02:00:00:00:00:01",
        router_claims=(
            RouterClaim("vendor", "MikroTik", 0.99, (mikrotik_evidence,)),
            RouterClaim("vendor", "Ubiquiti", 0.99, (ubnt_evidence,)),
        ),
    )

    fp = ap.router_fingerprint
    assert fp is not None
    assert fp.spoof_suspected is True
    assert len(fp.conflicts) == 1
    assert fp.conflicts[0].name == "vendor"
    assert {claim.value for claim in fp.conflicts[0].claims} == {"MikroTik", "Ubiquiti"}
    assert {e.source for e in fp.evidence} >= {"mikrotik.mac_winbox", "ubnt.discovery"}
    assert {claim.value for claim in fp.claims if claim.name == "vendor"} >= {"MikroTik", "Ubiquiti"}


def test_weak_conflicting_claims_do_not_flag_possible_spoof():
    weak = RouterEvidence("ssid.pattern", "ssid", "fake", 0.30)
    strong = RouterEvidence("wps.m1", "manufacturer", "MikroTik", 0.99)
    ap = AccessPoint(
        bssid="02:00:00:00:00:01",
        router_claims=(
            RouterClaim("vendor", "Ubiquiti", 0.30, (weak,)),
            RouterClaim("vendor", "MikroTik", 0.99, (strong,)),
        ),
    )

    fp = ap.router_fingerprint
    assert fp is not None
    assert fp.spoof_suspected is False
    assert fp.conflicts == ()


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
        wps_model_name="O2SMARTBOX",
    )
    fp = ap.router_fingerprint
    assert fp is not None
    assert fp.brand == "O2"
    assert fp.brand_confidence == 0.99
    assert fp.vendor == "Kaon"
    assert fp.vendor_confidence == 0.99
    assert fp.label == "O2 O2SMARTBOX router"
    assert any(e.source == "wps.passive" and e.name == "model" and e.value == "O2SMARTBOX"
               for e in fp.evidence)


def test_o2_smartbox_ssid_pattern_does_not_set_brand():
    fp = AccessPoint(bssid="02:00:00:00:00:01", ssid="O2SMARTBOX-123456").router_fingerprint
    assert fp is None


def test_o2_internet_ssid_clue_is_weak_because_ssids_are_renamable():
    fp = AccessPoint(bssid="02:00:00:00:00:01", ssid="O2-Internet-123456").router_fingerprint
    assert fp is not None
    assert fp.brand == "O2"
    assert round(fp.brand_confidence, 2) == 0.30
    assert fp.vendor is None
    assert fp.kind is None
    assert fp.label == "Possible O2"
    assert any(e.source == "ssid.o2" and e.name == "ssid" and e.value == "O2-Internet-123456"
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
    assert canonical_vendor("Routerboard.com") == "MikroTik"
    assert canonical_vendor("MikroTik") == "MikroTik"
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
    assert any(e.source == "ssid.apple" and e.name == "ssid" and e.value == "Alice’s iPhone"
               for e in fp.evidence)


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



def test_mikrotik_routerboard_oui_weakly_identifies_router_type():
    fp = AccessPoint(bssid="00:0c:42:11:22:33").router_fingerprint
    assert fp is not None
    assert fp.vendor == "MikroTik"
    assert round(fp.vendor_confidence, 2) == 0.30
    assert fp.kind == "router"
    assert round(fp.kind_confidence, 2) == 0.30
    assert fp.model is None
    assert fp.label == "Possible MikroTik router"
    assert any(e.source == "oui.mikrotik" and e.name == "kind" and e.value == "router"
               for e in fp.evidence)


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


def test_ubiquiti_oui_only_identifies_vendor():
    fp = AccessPoint(bssid="00:15:6d:11:22:33").router_fingerprint
    assert fp is not None
    assert fp.vendor == "Ubiquiti"
    assert round(fp.vendor_confidence, 2) == 0.30
    assert fp.kind is None
    assert fp.kind_confidence == 0.0
    assert fp.model is None
    assert fp.label == "Possible Ubiquiti"


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
    assert fp.label == "TP-Link"


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
    claims = list(wps_model_rule(ap))
    model_claim = next(claim for claim in claims if claim.name == "model")
    assert model_claim.value == "R9000"
