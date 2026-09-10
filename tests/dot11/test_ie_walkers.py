from __future__ import annotations

import struct

from wifit3.dot11.ie import iter_information_elements
from wifit3.dot11.parser import WlanFrameParser
from wifit3.dot11.wsc.messages import ATTR_MANUFACTURER, ATTR_MODEL_NAME, iter_wsc_tlvs, parse_tlvs


def test_iter_information_elements_walks_elements_and_yields_raw():
    ie0 = b"\x00\x04Test"
    ie1 = b"\x01\x02\x82\x84"
    data = ie0 + ie1
    elements = list(iter_information_elements(data))
    assert len(elements) == 2
    assert elements[0] == (0, b"Test", ie0)
    assert elements[1] == (1, b"\x82\x84", ie1)


def test_iter_information_elements_empty_or_too_short():
    assert list(iter_information_elements(b"")) == []
    assert list(iter_information_elements(b"\x00")) == []


def test_iter_information_elements_stops_on_truncated_length():
    # Tag 0 claims 10 bytes, but only 4 bytes follow.
    data = b"\x00\x0a1234"
    assert list(iter_information_elements(data)) == []

    # First element valid, second truncated: first is returned, second discarded.
    valid = b"\x00\x04Test"
    truncated = b"\x01\x10Short"
    elements = list(iter_information_elements(valid + truncated))
    assert len(elements) == 1
    assert elements[0] == (0, b"Test", valid)


def test_iter_information_elements_supports_start_offset():
    prefix = b"PADDING1234"
    ie0 = b"\x00\x04Test"
    elements = list(iter_information_elements(prefix + ie0, start=len(prefix)))
    assert len(elements) == 1
    assert elements[0] == (0, b"Test", ie0)


def test_iter_wsc_tlvs_walks_big_endian_attributes():
    tlv1 = struct.pack(">HH", ATTR_MANUFACTURER, 8) + b"MikroTik"
    tlv2 = struct.pack(">HH", ATTR_MODEL_NAME, 7) + b"hAP ac2"
    data = tlv1 + tlv2
    tlvs = list(iter_wsc_tlvs(data))
    assert len(tlvs) == 2
    assert tlvs[0] == (ATTR_MANUFACTURER, b"MikroTik")
    assert tlvs[1] == (ATTR_MODEL_NAME, b"hAP ac2")


def test_iter_wsc_tlvs_empty_or_too_short():
    assert list(iter_wsc_tlvs(b"")) == []
    assert list(iter_wsc_tlvs(b"\x10\x21\x00")) == []


def test_iter_wsc_tlvs_stops_on_truncated_length():
    # Length specifies 20 bytes, only 4 exist.
    data = struct.pack(">HH", ATTR_MANUFACTURER, 20) + b"1234"
    assert list(iter_wsc_tlvs(data)) == []


def test_parse_tlvs_keeps_last_repeated_attribute():
    first = struct.pack(">HH", ATTR_MANUFACTURER, 5) + b"First"
    second = struct.pack(">HH", ATTR_MANUFACTURER, 6) + b"Second"
    d = parse_tlvs(first + second)
    assert d[ATTR_MANUFACTURER] == b"Second"


def test_multi_ie_wsc_fragmentation_reassembles_split_tlv():
    """When an AP fragments WPS across multiple Tag 221 elements, a single TLV can cross
    the 255-byte boundary. The parser must concatenate all WPS fragments before decoding."""
    # Build a TLV that is split across two Tag 221 elements
    full_tlv = struct.pack(">HH", ATTR_MODEL_NAME, 10) + b"LongModelX"  # 14 bytes total
    frag1 = full_tlv[:8]  # attr(2) + len(2) + "Long" (4)
    frag2 = full_tlv[8:]  # "ModelX" (6)

    oui_wps = b"\x00\x50\xf2\x04"
    wps_ie1 = bytes([221, len(oui_wps) + len(frag1)]) + oui_wps + frag1
    wps_ie2 = bytes([221, len(oui_wps) + len(frag2)]) + oui_wps + frag2

    # Assemble beacon with standard header + SSID + rates + both WPS fragments
    fc = b"\x80\x00\x00\x00"
    addrs = b"\xff\xff\xff\xff\xff\xff\x11\x22\x33\x44\x55\x66\x11\x22\x33\x44\x55\x66\x00\x00"
    fixed = b"\x00" * 8 + b"\x64\x00\x01\x00"
    tag_ssid = b"\x00\x04Test"
    tag_rates = b"\x01\x01\x82"
    beacon = fc + addrs + fixed + tag_ssid + tag_rates + wps_ie1 + wps_ie2

    pkt = WlanFrameParser.parse_80211_frame(beacon, -60)
    assert pkt is not None
    assert pkt.wps is True
    assert pkt.wsc_model_name == "LongModelX"
