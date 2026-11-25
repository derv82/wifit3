"""Information-element decoding: the paths no other test reaches."""
import struct

import pytest

from wifit3.dot11.parser import WlanFrameParser
from wifit3.dot11.rsn import RsnElement, WpaElement, parse_rsn_element, parse_wpa_element

RSN_OUI = b"\x00\x0f\xac"
WPA_OUI = b"\x00\x50\xf2"
SSID_ELEMENT = b"\x00\x07TestNet"
RATES_ELEMENT = b"\x01\x04\x82\x84\x8b\x96"


def beacon(elements: bytes, ssid_element: bytes = SSID_ELEMENT) -> bytes:
    header = b"\x80\x00" + b"\x00\x00" + b"\xff" * 6 + bytes.fromhex("112233445566") * 2 + b"\x00\x00"
    fixed = bytes(8) + b"\x64\x00" + b"\x01\x00"      # capability: ESS, no Privacy
    return header + fixed + ssid_element + RATES_ELEMENT + elements


def parse_beacon(elements: bytes, ssid_element: bytes = SSID_ELEMENT):
    packet = WlanFrameParser.parse_80211_frame(beacon(elements, ssid_element), -40)
    assert packet is not None
    return packet


def element(element_id: int, body: bytes) -> bytes:
    return bytes([element_id, len(body)]) + body


def wps_element(attributes: bytes) -> bytes:
    return element(221, WPA_OUI + b"\x04" + attributes)


def tlv(attribute: int, value: bytes) -> bytes:
    return struct.pack(">HH", attribute, len(value)) + value


# ---- element dispatch ---------------------------------------------------------------------

def test_an_ssid_element_over_32_bytes_gives_no_ssid():
    packet = parse_beacon(b"", ssid_element=element(0, b"A" * 33))
    assert packet.ssid is None
    assert packet.ssid_bytes is None


def test_a_ds_element_not_1_byte_long_is_ignored_for_the_channel():
    packet = parse_beacon(element(3, b"\x06\x00") + element(61, b"\x24" + bytes(21)))
    assert packet.channel == 36


def test_empty_ht_and_1_byte_vht_operation_elements_give_no_channel():
    packet = parse_beacon(element(61, b"") + element(192, b"\x00"))
    assert packet.channel is None


def test_an_extended_capabilities_element_under_11_bytes_gives_no_beacon_protection():
    packet = parse_beacon(element(127, b"\xff" * 10))
    assert packet.beacon_protection is False


def test_a_second_unparseable_rsn_element_keeps_the_first_parsed_one():
    good = element(48, bytes.fromhex("0100000fac040100000fac040100000fac020000"))
    bad = element(48, b"\x02\x00" + bytes(10))
    packet = parse_beacon(good + bad)
    assert packet.rsn_valid is True
    assert packet.rsn_ie_raw == good
    assert packet.akms == ["PSK"]


def test_two_unparseable_rsn_elements_keep_the_first_raw_element():
    first = element(48, b"\x02\x00" + bytes(10))
    second = element(48, b"\x03\x00" + bytes(10))
    packet = parse_beacon(first + second)
    assert packet.rsn_valid is False
    assert packet.rsn_ie_raw == first


def test_a_second_wpa_element_is_ignored():
    first = element(221, WPA_OUI + b"\x01\x01\x00" + WPA_OUI + b"\x02\x01\x00" + WPA_OUI + b"\x02"
                    + b"\x01\x00" + WPA_OUI + b"\x02")
    second = element(221, WPA_OUI + b"\x01\x01\x00" + WPA_OUI + b"\x04\x01\x00" + WPA_OUI + b"\x04"
                     + b"\x01\x00" + WPA_OUI + b"\x01")
    packet = parse_beacon(first + second)
    assert packet.pairwise_cipher == "TKIP"
    assert packet.akms == ["PSK"]


def test_a_vendor_element_under_4_bytes_is_ignored():
    packet = parse_beacon(element(221, WPA_OUI))
    assert packet.wps is False
    assert packet.chipset is None
    assert packet.encryption == "OPEN"


def test_a_wpa_oui_element_that_is_neither_wpa_nor_wps_is_ignored():
    packet = parse_beacon(element(221, WPA_OUI + b"\x02\x01\x01\x00"))      # WMM
    assert packet.wps is False
    assert packet.encryption == "OPEN"
    assert packet.akms == []


def test_a_vendor_element_with_an_unknown_oui_sets_no_chipset():
    packet = parse_beacon(element(221, b"\xaa\xbb\xcc\x01\x00"))
    assert packet.chipset is None


# ---- RSN element ------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    bytes.fromhex("0100000fac0401000000"[:18]),             # 9 bytes
    bytes.fromhex("0200000fac040100000fac040100000fac02"),  # version 2
    bytes.fromhex("0100000fac040200000fac040100"),          # 2 pairwise suites, room for 1
    bytes.fromhex("0100000fac040100000fac04"),               # no room for the AKM count
    bytes.fromhex("0100000fac040100000fac040200000fac02"),  # 2 AKM suites, room for 1
])
def test_a_truncated_or_unknown_version_rsn_element_is_not_parsed(body):
    assert parse_rsn_element(body) is None


def test_rsn_pairwise_and_akm_suites_outside_00_0f_ac():
    body = (b"\x01\x00" + RSN_OUI + b"\x04"
            + b"\x02\x00" + b"\xaa\xbb\xcc\x04" + RSN_OUI + b"\x04"
            + b"\x05\x00" + RSN_OUI + b"\x02" + RSN_OUI + b"\x02" + RSN_OUI + b"\x63"
            + b"\xaa\xbb\xcc\x01" + b"\xdd\xee\xff\x01")
    assert parse_rsn_element(body) == RsnElement(
        group_cipher_suite=4,
        pairwise_cipher="CCMP",
        pairwise_cipher_suites=(4,),
        pairwise_cipher_count=2,
        akms=("PSK", "AKM-000fac63", "Vendor"),
        akm_suites=(2, 0x63),
        akm_suite_count=5,
        vendor_akm_offered=True,
        pmf_capable=False,
        pmf_required=False,
        group_management_cipher_suite=None,
    )


# ---- WPA element ------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    WPA_OUI + b"\x01\x01\x00" + WPA_OUI,                                  # 10 bytes
    WPA_OUI + b"\x01\x02\x00" + WPA_OUI + b"\x02\x01\x00" + WPA_OUI + b"\x02" + b"\x01\x00" + WPA_OUI + b"\x02",
    WPA_OUI + b"\x01\x01\x00" + WPA_OUI + b"\x02\x02\x00" + WPA_OUI + b"\x02" + b"\x01\x00",  # 2 pairwise, room for 1
])
def test_a_short_or_unknown_version_wpa_element_is_not_parsed(body):
    assert parse_wpa_element(body) is None


def test_wpa_pairwise_and_akm_suites_outside_00_50_f2():
    body = (WPA_OUI + b"\x01" + b"\x01\x00" + WPA_OUI + b"\x02"
            + b"\x02\x00" + b"\xaa\xbb\xcc\x04" + WPA_OUI + b"\x02"
            + b"\x05\x00" + WPA_OUI + b"\x02" + WPA_OUI + b"\x02" + WPA_OUI + b"\x09"
            + b"\xaa\xbb\xcc\x01" + WPA_OUI + b"\x01")
    assert parse_wpa_element(body) == WpaElement(
        pairwise_cipher="TKIP",
        akms=("PSK", "Vendor", "EAP"),
        akm_suites=(2, 1),
        vendor_akm_offered=True,
    )


def test_a_wpa_element_with_no_akm_suites():
    body = WPA_OUI + b"\x01" + b"\x01\x00" + WPA_OUI + b"\x02" + b"\x01\x00" + WPA_OUI + b"\x04" + b"\x00\x00"
    assert parse_wpa_element(body) == WpaElement(
        pairwise_cipher="CCMP", akms=(), akm_suites=(), vendor_akm_offered=False)


# ---- WPS element ------------------------------------------------------------------------

def test_wps_selected_registrar_and_device_type():
    packet = parse_beacon(wps_element(tlv(0x1041, b"\x01") + tlv(0x1054, b"\x00\x01" + bytes(6))))
    assert packet.wps_selected_registrar is True
    assert packet.wsc_device_type == "computer"


def test_wps_empty_identity_text_and_an_unknown_device_category_are_not_set():
    attributes = (tlv(0x1021, b"\x00\x00") + tlv(0x1023, b"") + tlv(0x1024, b" ")
                  + tlv(0x1011, b"\x00") + tlv(0x1054, b"\x00\xff" + bytes(6)) + tlv(0x104A, b""))
    packet = parse_beacon(wps_element(attributes))
    assert packet.wps is True
    assert packet.wsc_manufacturer is None
    assert packet.wsc_model_name is None
    assert packet.wsc_model_number is None
    assert packet.wsc_device_name is None
    assert packet.wsc_device_type is None
    assert packet.wps_version is None


@pytest.mark.parametrize("vendor_extension, wps_version", [
    (b"\x00\x11\x22" + b"\x00\x01\x20", "1.0"),               # not the WFA vendor id
    (b"\x00\x37\x2a" + b"\x00\x05\x20", "1.0"),               # Version2 subelement overruns
    (b"\x00\x37\x2a" + b"\x01\x01\xff" + b"\x00\x01\x20", "2.0"),   # Version2 after another subelement
    (b"\x00\x37\x2a" + b"\x01\x01\xff", "1.0"),                     # no Version2 subelement
])
def test_wps_version2_from_the_vendor_extension(vendor_extension, wps_version):
    packet = parse_beacon(wps_element(tlv(0x104A, b"\x10") + tlv(0x1049, vendor_extension)))
    assert packet.wps_version == wps_version
