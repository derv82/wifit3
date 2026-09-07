"""Tests for persist.common: naming, filename parsing, and Hc22000 parsing."""
from wifit3.persist.common import (
    AGGREGATED_HC22000_RE,
    LEGACY_CAPTURE_RE,
    WEP_KEY_HEX_RE,
    WPS_PIN_RE,
    WPS_PSK_RE,
    bssid_to_colon,
    bssid_to_dashed,
    parse_hc22000,
    safe_ssid,
)


def test_safe_ssid_sanitization():
    assert safe_ssid("Normal_SSID") == "Normal_SSID"
    assert safe_ssid("With Spaces & Special!") == "With_Spaces___Special_"
    assert safe_ssid("") == "hidden"
    assert safe_ssid(None) == "hidden"
    assert len(safe_ssid("A" * 50)) == 32


def test_bssid_conversions():
    assert bssid_to_dashed("00:11:22:AA:BB:CC") == "00-11-22-aa-bb-cc"
    assert bssid_to_colon("00-11-22-AA-BB-CC") == "00:11:22:aa:bb:cc"


def test_legacy_capture_regex():
    m = LEGACY_CAPTURE_RE.match("HomeNet_00-11-22-33-44-55_1725000000_handshake.hc22000")
    assert m is not None
    assert m.group("ssid") == "HomeNet"
    assert m.group("bssid") == "00-11-22-33-44-55"
    assert m.group("epoch") == "1725000000"
    assert m.group("kind") == "handshake"
    assert m.group("ext") == "hc22000"

    assert LEGACY_CAPTURE_RE.match("invalid_name.txt") is None


def test_aggregated_hc22000_regex():
    m = AGGREGATED_HC22000_RE.match("HomeNet_00-11-22-33-44-55.hc22000")
    assert m is not None
    assert m.group("ssid") == "HomeNet"
    assert m.group("bssid") == "00-11-22-33-44-55"

    assert AGGREGATED_HC22000_RE.match("HomeNet_00-11-22-33-44-55_12345_handshake.hc22000") is None
    assert AGGREGATED_HC22000_RE.match("HomeNet_00-11-22-33-44-55.txt") is None


def test_payload_regexes():
    wep_text = "SSID: Test\nWEP key (hex): 0123456789ABCDEF\n"
    m_wep = WEP_KEY_HEX_RE.search(wep_text)
    assert m_wep is not None and m_wep.group(1) == "0123456789ABCDEF"

    wps_text = "SSID: Test\nBSSID: 00:11:22:33:44:55\nPSK: secretpass123\nPIN: 12345670\n"
    m_psk = WPS_PSK_RE.search(wps_text)
    m_pin = WPS_PIN_RE.search(wps_text)
    assert m_psk is not None and m_psk.group(1) == "secretpass123"
    assert m_pin is not None and m_pin.group(1) == "12345670"


def test_parse_hc22000_pmkid():
    line = "WPA*01*4e0828e60a163e469f49e6b77e669086*001122334455*aabbccddeeff*486f6d65***"
    parsed = parse_hc22000(line)
    assert parsed is not None
    assert parsed.kind == "01"
    assert parsed.pmkid_or_mic == "4e0828e60a163e469f49e6b77e669086"
    assert parsed.mac_ap == "001122334455"
    assert parsed.mac_sta == "aabbccddeeff"
    assert parsed.essid == "486f6d65"
    assert parsed.anonce == ""


def test_parse_hc22000_eapol():
    line = "WPA*02*aabbccddeeff00112233445566778899*001122334455*aabbccddeeff*486f6d65*1122334455667788990011223344556677889900112233445566778899001122*01020304*02"
    parsed = parse_hc22000(line)
    assert parsed is not None
    assert parsed.kind == "02"
    assert parsed.pmkid_or_mic == "aabbccddeeff00112233445566778899"
    assert parsed.mac_ap == "001122334455"
    assert parsed.mac_sta == "aabbccddeeff"
    assert parsed.essid == "486f6d65"
    assert parsed.anonce == "1122334455667788990011223344556677889900112233445566778899001122"
    assert parsed.eapol == "01020304"
    assert parsed.message_pair == "02"


def test_parse_hc22000_malformed():
    assert parse_hc22000("") is None
    assert parse_hc22000("NOTWPA*01*abc*def") is None
    assert parse_hc22000("WPA*01*short") is None
