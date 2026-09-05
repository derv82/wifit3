"""Contract tests for scripts/id/frame.py. No hardware: frames are crafted from raw bytes."""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from frame import (   # noqa: E402
    Akm, Cipher, ExtCap, ExtIE, Frame, HeBssColor, HeMacCap, HeOpParams, HePhyCap, HTCap, IE,
    VHTCap, fmt, render,
)


def ie(tag: int, value: bytes) -> bytes:
    return bytes([tag, len(value)]) + value


def beacon(ies: bytes = b"", src: bytes = b"\x02\x00\x00\x00\x00\x01",
           dst: bytes = b"\xff" * 6) -> bytes:
    """A minimal beacon: 24-byte header + 12 fixed bytes + the given IE bytes."""
    hdr = b"\x80\x00" + b"\x00\x00" + dst + src + b"\x00" * 6 + b"\x00\x00"
    return hdr + b"\x00" * 12 + ies


_RSN_PSK = bytes.fromhex("0100" "000fac04" "0100" "000fac04" "0100" "000fac02" "0c00")


# --- vocabularies -------------------------------------------------------------------------
def test_ie_is_id_and_label():
    assert IE.of(0) is IE.SSID
    assert IE.SSID.value == 0
    assert IE.SSID.label == "SSID"


def test_ie_of_unknown_is_none():
    assert IE.of(3) is None       # DSParam: not in the allowlist
    assert IE.of(250) is None


def test_cross_vocabulary_members_never_equal():
    # Both are the int 4, but distinct vocabularies must not compare equal (plain Enum, not Int).
    assert Cipher.CCMP.value == Akm.FT_PSK.value == 4
    assert Cipher.CCMP != Akm.FT_PSK


# --- allowlist ----------------------------------------------------------------------------
def test_parser_keeps_only_allowlisted_ies():
    raw = beacon(ie(0, b"net") + ie(3, b"\x06"))   # SSID (kept) + DSParam (dropped)
    f = Frame.parse(raw)
    assert set(f.ies) == {IE.SSID}
    assert f.ies[IE.SSID] == b"net"                # lossless bytes, not a lossy str


def test_addresses_and_name():
    f = Frame.parse(beacon(ie(0, b"net")))
    assert f.name == "beacon"
    assert f.src == "02:00:00:00:00:01"
    assert f.dst == "ff:ff:ff:ff:ff:ff"


def test_extension_element_namespace():
    # element 255 keyed on its extension byte: HE Caps (35) kept, ext 99 dropped.
    f = Frame.parse(beacon(ie(255, b"\x23\xaa\xbb") + ie(255, b"\x63\x01")))
    assert set(f.ies) == {ExtIE.HE_CAPS}
    assert set(f.ies[ExtIE.HE_CAPS]) == {"mac", "phy", "rest"}   # ext byte stripped, body decoded


# --- decoders -----------------------------------------------------------------------------
def test_rates_decode():
    f = Frame.parse(beacon(ie(1, bytes([0x82, 0x84, 0x8b, 0x96, 0x24]))))
    assert f.ies[IE.SUPP_RATES] == ["1b", "2b", "5.5b", "11b", "18"]


def test_rsn_decode_to_enums():
    v = Frame.parse(beacon(ie(48, _RSN_PSK))).ies[IE.RSN]
    assert v["group"] is Cipher.CCMP
    assert v["pair"] == [Cipher.CCMP]
    assert v["akm"] == [Akm.PSK]
    assert v["caps"] == b"\x0c\x00"


def test_rsn_bounds_bogus_count():
    # pairwise count 0xffff with no bytes behind it must not build a huge junk list.
    bogus = bytes.fromhex("0100" "000fac04" "ffff")
    v = Frame.parse(beacon(ie(48, bogus))).ies[IE.RSN]
    assert v["group"] is Cipher.CCMP
    assert v["pair"] == []
    assert v["akm"] == []


def test_vendor_recurses_into_wsc():
    wsc = b"\x10\x21\x00\x04" + b"Acme"          # ATTR_MANUFACTURER (0x1021), len 4, "Acme"
    v = Frame.parse(beacon(ie(221, b"\x00\x50\xf2\x04" + wsc))).ies[IE.VENDOR]
    assert v["oui"] == "00:50:f2"
    assert v["type"] == 4
    assert v["wsc"] == {"mfr": b"Acme"}          # nested dict, lossless bytes leaf


def test_hecaps_decode_from_real_capture():
    # real HE Caps body: MAC 6 bytes, PHY 11 bytes, then the MCS/NSS + PPE tail kept raw.
    body = bytes.fromhex("050018120010222002c00f03851800cc00aaffaaff1b1cc7711cc771")
    v = Frame.parse(beacon(ie(255, b"\x23" + body))).ies[ExtIE.HE_CAPS]
    mac, phy = v["mac"], v["phy"]
    assert mac["cap"] == (HeMacCap.HTC_HE | HeMacCap.TWT_RESPONDER | HeMacCap.BSR
                          | HeMacCap.BCAST_TWT | HeMacCap.OMI_CONTROL
                          | HeMacCap.OM_CTRL_UL_MU_DATA_DIS_RX)
    assert mac["max_ampdu_len_exp"] == 2
    assert "+0x" not in fmt(mac["cap"])                 # all 48 bits named, nothing anonymous
    assert phy["channel_width_set"] == 0x11
    assert phy["beamformee_max_sts_under_80mhz"] == 3 and phy["max_nc"] == 3
    assert phy["nominal_packet_padding"] == 3
    assert HePhyCap.SU_BEAMFORMER in phy["cap"] and HePhyCap.PPE_THRESHOLD_PRESENT in phy["cap"]
    assert v["rest"] == bytes.fromhex("aaffaaff1b1cc7711cc771")


def test_heop_decode_from_real_capture():
    # real HE Operation body: params (3) + BSS color (1) + basic MCS/NSS (2), no optional tails.
    v = Frame.parse(beacon(ie(255, b"\x24" + bytes.fromhex("f43f0028fcff")))).ies[ExtIE.HE_OP]
    assert v["params"]["default_pe_duration"] == 4
    assert v["params"]["txop_dur_rts_threshold"] == 1023
    assert v["params"]["cap"] == HeOpParams(0)          # no presence/TWT bits set
    assert v["color"]["bss_color"] == 40
    assert v["color"]["cap"] == HeBssColor(0)
    assert v["rest"] == b"\xfc\xff"


# --- capability bitfields -----------------------------------------------------------------
def test_htcaps_decode_flag_and_rest():
    # cap 0x00e3 = LDPC|CH_WIDTH_40|SGI_20|SGI_40|TX_STBC; wide fields zero; two trailing bytes raw.
    v = Frame.parse(beacon(ie(45, b"\xe3\x00\xaa\xbb"))).ies[IE.HT_CAPS]
    assert v["cap"] == HTCap.LDPC | HTCap.CH_WIDTH_40 | HTCap.SGI_20 | HTCap.SGI_40 | HTCap.TX_STBC
    assert HTCap.SGI_40 in v["cap"]
    assert v["sm_power_save"] == 0 and v["rx_stbc"] == 0
    assert v["rest"] == b"\xaa\xbb"


def test_htcaps_wide_fields_decode_as_ints():
    # word 0x030c: sm_power_save (bits 2-3) = 3, rx_stbc (bits 8-9) = 3; no single bits remain.
    v = Frame.parse(beacon(ie(45, b"\x0c\x03"))).ies[IE.HT_CAPS]
    assert v["cap"] == HTCap(0)           # every set bit belongs to a wide field, so the flag is empty
    assert v["sm_power_save"] == 3
    assert v["rx_stbc"] == 3


def test_htcaps_reserved_bit_named_not_hex():
    # bit 13 is Reserved: named RESERVED_13 (so vendor use surfaces) rather than an anonymous residual.
    cap = Frame.parse(beacon(ie(45, b"\x00\x20"))).ies[IE.HT_CAPS]["cap"]
    assert HTCap.RESERVED_13 in cap
    assert fmt(cap) == "RESERVED_13"


def test_htcaps_diff_catches_wide_field_change():
    a = Frame.parse(beacon(ie(45, b"\x01\x00")))   # sm_power_save 0
    b = Frame.parse(beacon(ie(45, b"\x09\x00")))   # sm_power_save 2 (bit 3 set)
    assert set(a.diff(b)["changed"]) == {IE.HT_CAPS}


def test_vhtcaps_decode_flag_and_rest():
    # cap 0x000000e0 = SGI_80|SGI_160|TX_STBC; the 8-byte MCS/NSS map kept raw.
    mcs = bytes(range(8))
    v = Frame.parse(beacon(ie(191, b"\xe0\x00\x00\x00" + mcs))).ies[IE.VHT_CAPS]
    assert v["cap"] == VHTCap.SGI_80 | VHTCap.SGI_160 | VHTCap.TX_STBC
    assert v["rest"] == mcs


def test_vhtcaps_wide_fields_from_real_capture():
    # real VHTCaps word 0x33C379B2 (LE bytes b2 79 c3 33): the fields that were an unnamed hex smear.
    v = Frame.parse(beacon(ie(191, bytes.fromhex("b279c333") + bytes(8)))).ies[IE.VHT_CAPS]
    assert v["cap"] == (VHTCap.RXLDPC | VHTCap.SGI_80 | VHTCap.TX_STBC | VHTCap.SU_BEAMFORMER
                        | VHTCap.SU_BEAMFORMEE | VHTCap.HTC_VHT
                        | VHTCap.RX_ANTENNA_PATTERN | VHTCap.TX_ANTENNA_PATTERN)
    assert "+0x" not in fmt(v["cap"])     # every non-flag bit is a named field, so no residual
    assert v["max_mpdu"] == 2 and v["chan_width"] == 0 and v["rx_stbc"] == 1
    assert v["bf_sts"] == 3 and v["sounding_dims"] == 3 and v["max_ampdu_exp"] == 7
    assert v["link_adapt"] == 0 and v["ext_nss_bw"] == 0


def test_flags_of_different_classes_never_equal():
    # Both are bit 0x0080, but distinct Flag classes must not compare equal (Flag, not IntFlag).
    assert HTCap.TX_STBC.value == VHTCap.TX_STBC.value == 0x0080
    assert HTCap.TX_STBC != VHTCap.TX_STBC


def test_extcaps_decode_named_bits():
    # bit 19 = BSS_TRANSITION, bit 31 = INTERWORKING; a 4-byte element has neither wide field.
    v = Frame.parse(beacon(ie(127, b"\x00\x00\x08\x80"))).ies[IE.EXT_CAPS]
    assert v["cap"] == ExtCap.BSS_TRANSITION | ExtCap.INTERWORKING
    assert "service_interval_granularity" not in v and "max_msdus_in_amsdu" not in v


def test_extcaps_diff_catches_named_bit_change():
    a = Frame.parse(beacon(ie(127, b"\x00\x00\x08\x80")))
    b = Frame.parse(beacon(ie(127, b"\x00\x00\x08\xc0")))   # + bit 30 TDLS_CHANNEL_SWITCHING
    assert set(a.diff(b)["changed"]) == {IE.EXT_CAPS}


def test_extcaps_reserved_bit_named():
    # bit 35 is Reserved: named RESERVED_35 so a vendor setting it surfaces by name.
    v = Frame.parse(beacon(ie(127, b"\x00\x00\x00\x00\x08"))).ies[IE.EXT_CAPS]   # byte 4 bit 3 = bit 35
    assert ExtCap.RESERVED_35 in v["cap"]


def test_extcaps_wide_field_service_interval():
    # a 6-byte element carries Service Interval Granularity (bits 41-43) but not Max MSDUs (63-64).
    v = Frame.parse(beacon(ie(127, (5 << 41).to_bytes(6, "little")))).ies[IE.EXT_CAPS]
    assert v["service_interval_granularity"] == 5
    assert "max_msdus_in_amsdu" not in v


def test_extcaps_bit_past_map_kept_in_residual():
    # bit 88 (byte 11) is past the named range: KEEP holds it as residual hex, still diffable.
    v = Frame.parse(beacon(ie(127, bytes(11) + b"\x01"))).ies[IE.EXT_CAPS]
    assert v["cap"].value == 1 << 88
    assert fmt(v["cap"]) == f"0x{1 << 88:x}"   # unnamed bit kept, shown as residual hex


# --- vendor labels and WMM ----------------------------------------------------------------
_WMM_TAIL = (b"\x01\x01\x00\x00"          # subtype=param, version=1, qos=0, reserved=0
             + b"\x03\xa4\x00\x00"        # AC_BE: aifsn 3, aci 0, ecwmin 4, ecwmax 10, txop 0
             + b"\x27\xa5\x00\x00"        # AC_BK: aifsn 7, aci 1
             + b"\x42\x43\x5e\x00"        # AC_VI: aifsn 2, aci 2, txop 0x5e
             + b"\x62\x32\x2f\x00")       # AC_VO: aifsn 2, aci 3


def test_vendor_wmm_type_label_and_edca():
    v = Frame.parse(beacon(ie(221, b"\x00\x50\xf2\x02" + _WMM_TAIL))).ies[IE.VENDOR]
    assert v["type"] == 2
    assert v["type_name"] == "WMM"
    assert v["wmm"]["edca"][0] == {
        "aifsn": 3, "acm": False, "aci": 0, "ecwmin": 4, "ecwmax": 10, "txop": 0,
    }
    assert [ac["aci"] for ac in v["wmm"]["edca"]] == [0, 1, 2, 3]
    assert v["wmm"]["edca"][2]["txop"] == 0x5e
    assert "data" not in v


def test_vendor_wmm_info_element_stays_raw():
    # subtype 0 (info element, not the parameter element): label still shown, payload kept raw.
    tail = b"\x00\x01\x00"
    v = Frame.parse(beacon(ie(221, b"\x00\x50\xf2\x02" + tail))).ies[IE.VENDOR]
    assert v["type_name"] == "WMM"
    assert v["data"] == tail
    assert "wmm" not in v


def test_vendor_unknown_type_keeps_raw_data():
    v = Frame.parse(beacon(ie(221, b"\x00\x10\x18\x02\xde\xad"))).ies[IE.VENDOR]
    assert "type_name" not in v
    assert v["data"] == b"\xde\xad"


def test_vendor_wpa_decodes_ciphers_under_msft_oui():
    # WPA element: version + group TKIP + pair TKIP + akm PSK, all under OUI 00-50-f2.
    tail = bytes.fromhex("0100" "0050f202" "0100" "0050f202" "0100" "0050f202")
    v = Frame.parse(beacon(ie(221, b"\x00\x50\xf2\x01" + tail))).ies[IE.VENDOR]
    assert v["type_name"] == "WPA"
    assert v["wpa"]["group"] is Cipher.TKIP
    assert v["wpa"]["pair"] == [Cipher.TKIP]
    assert v["wpa"]["akm"] == [Akm.PSK]
    assert "data" not in v


def test_vendor_hs20_indication_decodes_config():
    # config 0x14: release nibble 1, DGAF not disabled, ANQP domain id present (0x1234, LE).
    v = Frame.parse(beacon(ie(221, b"\x50\x6f\x9a\x10" + b"\x14\x34\x12"))).ies[IE.VENDOR]
    assert v["type_name"] == "HS20"
    assert v["hs20"] == {"release": 1, "dgaf_disabled": False, "anqp_domain_id": 0x1234}
    assert "data" not in v


def test_vendor_mbo_attributes_decode():
    tail = bytes([4, 1, 3, 5, 1, 255, 1, 1, 0x40])   # assoc_disallowed=3, cell_pref=255, ap cell aware
    v = Frame.parse(beacon(ie(221, b"\x50\x6f\x9a\x16" + tail))).ies[IE.VENDOR]
    assert v["type_name"] == "MBO"
    assert v["mbo"] == {"ap_cell_aware": True, "assoc_disallowed": 3, "cell_pref": 255}


def test_vendor_p2p_device_info_and_capability():
    info = (b"\x02\xaa\xbb\xcc\xdd\xee"              # p2p device address
            + b"\x01\x08"                            # config methods (big endian)
            + b"\x00\x0a\x00\x50\xf2\x04\x00\x05"    # primary dev type: category 10, sub 5
            + b"\x00"                                # zero secondary device types
            + b"\x10\x11\x00\x04" + b"Pix1")         # nested WPS device name (big endian type/len)
    cap = b"\x25\x03"
    tail = (b"\x02" + len(cap).to_bytes(2, "little") + cap
            + b"\x0d" + len(info).to_bytes(2, "little") + info)
    v = Frame.parse(beacon(ie(221, b"\x50\x6f\x9a\x09" + tail))).ies[IE.VENDOR]
    assert v["type_name"] == "P2P"
    assert v["p2p"]["capability"] == {"device": 0x25, "group": 0x03}
    di = v["p2p"]["device_info"]
    assert di["addr"] == "02:aa:bb:cc:dd:ee"
    assert di["name"] == b"Pix1"
    assert di["primary_dev_type"] == {"category": 10, "sub_category": 5}
    assert di["config_methods"] == 0x0108


# --- Flag presentation --------------------------------------------------------------------
def test_fmt_renders_flag():
    assert fmt(HTCap.LDPC | HTCap.SGI_40) == "LDPC|SGI_40"
    assert fmt(HTCap(0x0009)) == "LDPC+0x8"             # named bit plus an unnamed residual as hex
    assert fmt(HTCap(0x0008)) == "0x8"                  # only unnamed bits: hex, no leading +
    assert fmt(HTCap(0x0000)) == "0"                    # empty flag
    assert fmt({"cap": HTCap.LDPC | HTCap.TX_STBC, "rest": b"\xaa"}) == "{cap=LDPC|TX_STBC,rest=aa}"


# --- diff ---------------------------------------------------------------------------------
def test_diff_added_removed_changed():
    a = Frame.parse(beacon(ie(0, b"net") + ie(1, bytes([0x82]))))
    b = Frame.parse(beacon(ie(0, b"NEW") + ie(48, _RSN_PSK)))
    d = a.diff(b)
    assert set(d["added"]) == {IE.RSN}
    assert set(d["removed"]) == {IE.SUPP_RATES}
    assert set(d["changed"]) == {IE.SSID}
    assert d["changed"][IE.SSID] == (b"net", b"NEW")


def test_diff_identical_is_empty():
    a = Frame.parse(beacon(ie(0, b"net")))
    b = Frame.parse(beacon(ie(0, b"net")))
    assert a.diff(b) == {"added": {}, "removed": {}, "changed": {}}


def test_diff_deep_change_detected():
    # same SSID, RSN akm differs (PSK vs SAE) — a change nested inside the RSN dict.
    sae = bytes.fromhex("0100" "000fac04" "0100" "000fac04" "0100" "000fac08" "0c00")
    a = Frame.parse(beacon(ie(48, _RSN_PSK)))
    b = Frame.parse(beacon(ie(48, sae)))
    assert set(a.diff(b)["changed"]) == {IE.RSN}


def test_diff_distinguishes_bytes_that_share_a_hex_spelling():
    # b"face" (ascii) and b"\xfa\xce" would collide if SSID were stored as text; as bytes they differ.
    a = Frame.parse(beacon(ie(0, b"face")))
    b = Frame.parse(beacon(ie(0, b"\xfa\xce")))
    assert set(a.diff(b)["changed"]) == {IE.SSID}


# --- presentation -------------------------------------------------------------------------
def test_fmt_recurses_any_depth():
    assert fmt([Cipher.CCMP, Akm.PSK]) == "[CCMP,PSK]"
    assert fmt(b"net") == "'net'"                       # printable bytes -> quoted text
    assert fmt(b"\x01\x00\x00\x00") == "01+6z"          # non-printable -> squeezed hex
    assert fmt({"a": [1, {"b": Cipher.CCMP}]}) == "{a=[1,{b=CCMP}]}"


def test_render_line():
    f = Frame.parse(beacon(ie(0, b"net") + ie(1, bytes([0x82, 0x84]))))
    assert render(f) == "02:00:00:00:00:01->ff:ff:ff:ff:ff:ff [beacon] SSID='net',SuppRates=[1b,2b]"


def test_render_none_when_no_allowlisted_ies():
    assert render(Frame.parse(beacon(ie(3, b"\x06")))) is None   # only a dropped IE
