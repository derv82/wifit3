"""Offline tests for the WSC M1 identity extractor.

Covers wps_text() byte hygiene, identity_from_attrs() attribute mapping,
the None-vs-empty rules in _text(), and the WpsM1Identity.present property.
End-to-end: parse a real build_m1() blob and confirm the device TLVs surface.
"""

from wifit3.dot11.wsc import identity as I
from wifit3.dot11.wsc import messages as M


# ---- wps_text -------------------------------------------------------------
def test_wps_text_strips_trailing_nulls():
    assert I.wps_text(b"Microsoft\x00\x00\x00") == "Microsoft"


def test_wps_text_strips_surrounding_whitespace():
    assert I.wps_text(b"  Windows \t\n") == "Windows"


def test_wps_text_decodes_utf8():
    assert I.wps_text("Réseau".encode("utf-8")) == "Réseau"


def test_wps_text_replaces_invalid_utf8_without_raising():
    # A lone 0xFF is not valid UTF-8; "replace" yields U+FFFD, never a crash.
    out = I.wps_text(b"AP\xff")
    assert out.startswith("AP") and "�" in out


def test_wps_text_all_nulls_is_empty():
    assert I.wps_text(b"\x00\x00\x00\x00") == ""


def test_wps_text_empty_bytes_is_empty():
    assert I.wps_text(b"") == ""


# ---- identity_from_attrs --------------------------------------------------
def _attrs(**kv: bytes) -> dict[int, bytes]:
    return dict(kv)


def test_identity_from_attrs_full():
    attrs = {
        M.ATTR_MANUFACTURER: b"ASUSTeK Computer Inc.",
        M.ATTR_MODEL_NAME: b"RT-AC68U",
        M.ATTR_MODEL_NUMBER: b"AC1900",
        M.ATTR_DEV_NAME: b"ASUS Router",
        M.ATTR_PRIMARY_DEV_TYPE: b"network_infrastructure"
    }
    ident = I.identity_from_attrs(attrs)
    assert ident.manufacturer == "ASUSTeK Computer Inc."
    assert ident.model_name == "RT-AC68U"
    assert ident.model_number == "AC1900"
    assert ident.device_name == "ASUS Router"
    assert ident.present is True


def test_identity_from_attrs_missing_fields_are_none():
    ident = I.identity_from_attrs({M.ATTR_MANUFACTURER: b"Netgear"})
    assert ident.manufacturer == "Netgear"
    assert ident.model_name is None
    assert ident.model_number is None
    assert ident.device_name is None
    assert ident.present is True


def test_identity_from_attrs_empty_dict_is_absent():
    ident = I.identity_from_attrs({})
    assert ident == I.WpsM1Identity()
    assert ident.present is False


def test_identity_from_attrs_empty_value_is_none():
    # A present-but-zero-length TLV must map to None, not "".
    ident = I.identity_from_attrs({M.ATTR_MANUFACTURER: b""})
    assert ident.manufacturer is None
    assert ident.present is False


def test_identity_from_attrs_whitespace_only_value_is_none():
    # wps_text() strips to "", and _text() collapses that to None.
    ident = I.identity_from_attrs({M.ATTR_MODEL_NAME: b"   \x00"})
    assert ident.model_name is None
    assert ident.present is False


def test_identity_from_attrs_trailing_nulls_trimmed():
    ident = I.identity_from_attrs({M.ATTR_DEV_NAME: b"DESKTOP-7H2K9P3\x00"})
    assert ident.device_name == "DESKTOP-7H2K9P3"


# ---- present property -----------------------------------------------------
def test_present_true_for_any_single_field():
    assert I.WpsM1Identity(manufacturer="x").present is True
    assert I.WpsM1Identity(model_name="x").present is True
    assert I.WpsM1Identity(model_number="x").present is True
    assert I.WpsM1Identity(device_name="x").present is True


def test_present_false_when_all_none():
    assert I.WpsM1Identity().present is False


# ---- end-to-end against a real M1 blob ------------------------------------
def test_identity_from_built_m1():
    m1 = M.build_m1(
        uuid_e=b"\x11" * 16,
        mac_e=b"\x22" * 6,
        nonce_e=b"\x33" * 16,
        pke=b"\x44" * 192,
    )
    ident = I.identity_from_attrs(M.parse_tlvs(m1))
    # build_m1 embeds the impersonated Windows registrar descriptor.
    assert ident.manufacturer == M._MANUFACTURER.decode()
    assert ident.model_name == M._MODEL_NAME.decode()
    assert ident.model_number == M._MODEL_NUMBER.decode()
    assert ident.device_name == M._DEVICE_NAME.decode()
    assert ident.present is True
