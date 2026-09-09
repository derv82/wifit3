"""Offline tests for the WSC M1 identity extractor.

Covers wps_text() byte hygiene, identity_from_attrs() attribute mapping,
the None-vs-empty rules in _text(), and the WpsM1Identity.present property.
End-to-end: parse a real build_m1() blob and confirm the device TLVs surface.
"""

from wifit3.dot11.wsc import identity as I
from wifit3.dot11.wsc import messages as M
from wifit3.models import ApIdentity, IdSource


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


# ---- apply_wsc_identity --------------------------------------------------
def _attrs(**kv: bytes) -> dict[int, bytes]:
    return dict(kv)


def test_apply_wsc_identity_full():
    attrs = {
        M.ATTR_MANUFACTURER: b"ASUSTeK Computer Inc.",
        M.ATTR_MODEL_NAME: b"RT-AC68U",
        M.ATTR_MODEL_NUMBER: b"AC1900",
        M.ATTR_DEV_NAME: b"ASUS Router",
        M.ATTR_PRIMARY_DEV_TYPE: b"network_infrastructure",
    }
    ident = ApIdentity()
    assert I.apply_wsc_identity(ident, IdSource.WSC_M1, attrs) is True
    assert ident.manufacturer == "ASUS"
    assert ident.model_name == "RT-AC68U"
    assert ident.model_number == "AC1900"
    assert ident.device_name == "ASUS Router"


def test_apply_wsc_identity_missing_fields_are_none():
    ident = ApIdentity()
    assert I.apply_wsc_identity(ident, IdSource.WSC_M1, {M.ATTR_MANUFACTURER: b"Netgear"}) is True
    assert ident.manufacturer == "Netgear"
    assert ident.model_name is None
    assert ident.model_number is None
    assert ident.device_name is None


def test_apply_wsc_identity_empty_dict_is_absent():
    ident = ApIdentity()
    assert I.apply_wsc_identity(ident, IdSource.WSC_M1, {}) is False
    assert ident.manufacturer is None


def test_apply_wsc_identity_empty_value_is_none():
    ident = ApIdentity()
    assert I.apply_wsc_identity(ident, IdSource.WSC_M1, {M.ATTR_MANUFACTURER: b""}) is False
    assert ident.manufacturer is None


def test_apply_wsc_identity_whitespace_only_value_is_none():
    ident = ApIdentity()
    assert I.apply_wsc_identity(ident, IdSource.WSC_M1, {M.ATTR_MODEL_NAME: b"   \x00"}) is False
    assert ident.model_name is None


def test_apply_wsc_identity_trailing_nulls_trimmed():
    ident = ApIdentity()
    assert I.apply_wsc_identity(ident, IdSource.WSC_M1, {M.ATTR_DEV_NAME: b"DESKTOP-7H2K9P3\x00"}) is True
    assert ident.device_name == "DESKTOP-7H2K9P3"


# ---- presence check ------------------------------------------------------
def test_present_true_for_any_single_field():
    for attr in (M.ATTR_MANUFACTURER, M.ATTR_MODEL_NAME, M.ATTR_MODEL_NUMBER, M.ATTR_DEV_NAME):
        ident = ApIdentity()
        assert I.apply_wsc_identity(ident, IdSource.WSC_M1, {attr: b"x"}) is True


def test_present_false_when_all_none():
    ident = ApIdentity()
    assert I.apply_wsc_identity(ident, IdSource.WSC_M1, {}) is False


# ---- end-to-end against a real M1 blob ------------------------------------
def test_identity_from_built_m1():
    m1 = M.build_m1(
        uuid_e=b"\x11" * 16,
        mac_e=b"\x22" * 6,
        nonce_e=b"\x33" * 16,
        pke=b"\x44" * 192,
    )
    ident = ApIdentity()
    assert I.apply_wsc_identity(ident, IdSource.WSC_M1, M.parse_tlvs(m1)) is True
    assert ident.manufacturer == M._MANUFACTURER.decode()
    assert ident.model_name == M._MODEL_NAME.decode()
    assert ident.model_number == M._MODEL_NUMBER.decode()
    assert ident.device_name == M._DEVICE_NAME.decode()
