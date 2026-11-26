"""displayable_ssid: the SSID text the UI shows."""
import pytest

from wifit3.text import displayable_ssid


@pytest.mark.parametrize(("ssid_bytes", "displayed"), [
    pytest.param(b"Caf\xc3\xa9", "Caf\xe9", id="utf-8"),
    pytest.param(b"Caf\xe9", "Caf\xe9", id="latin-1"),
    pytest.param(b"Cr\xe8me", "Cr\xe8me", id="latin-1-between-ascii"),
    pytest.param(b"\xd6\xd0\xce\xc4", b"\xe4\xb8\xad\xe6\x96\x87".decode("utf-8"), id="gbk"),
    pytest.param(b"ab\xc2\x85", "ab\\xc2\\x85", id="utf-8-c1-control"),
    pytest.param(b"Caf\x81", "Caf\\x81", id="cp1252-unassigned-byte"),
    pytest.param(b"a\x01b\xe9", "a\\x01b\xe9", id="cp1252-control"),
    pytest.param(b"", "", id="empty"),
])
def test_displayable_ssid(ssid_bytes, displayed):
    assert displayable_ssid(ssid_bytes) == displayed
