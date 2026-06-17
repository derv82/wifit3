"""The env-var A/B switch for the Realtek 11ac pairs.

The DKMS port and the mainline driver claim the same VID:PID. The registry resolves each pair to
a single driver via ``_env_driver``; the env var picks which, defaulting to DKMS.
"""
import pytest

from wifit3.wlan import manager


@pytest.fixture(autouse=True)
def _fresh_registry():
    # The registry is built once and cached; null it so each test's env var is read fresh.
    manager._DRIVER_CLASSES = None
    yield
    manager._DRIVER_CLASSES = None


class _FakeDev:
    def __init__(self, vid, pid):
        self.idVendor, self.idProduct = vid, pid


def _driver_for(vid, pid):
    cls, _ = manager._match_driver(_FakeDev(vid, pid))
    return cls.__name__


def test_rtl8821_default_is_dkms(monkeypatch):
    monkeypatch.delenv("WIFIT3_RTL8821", raising=False)
    assert _driver_for(0x0BDA, 0x0811) == "Rtl8821auDkmsDriver"


def test_rtl8821_mainline_opt_in(monkeypatch):
    monkeypatch.setenv("WIFIT3_RTL8821", "MainLine")   # case-insensitive
    assert _driver_for(0x0BDA, 0x0811) == "RTL8821AUDriver"


def test_rtl8821_unknown_value_stays_dkms(monkeypatch):
    monkeypatch.setenv("WIFIT3_RTL8821", "dkms")   # any non-"mainline" -> default DKMS
    assert _driver_for(0x0BDA, 0x0811) == "Rtl8821auDkmsDriver"


def test_both_8821_drivers_claim_0811():
    from wifit3.chips.rtl8821au_dkms.driver import Rtl8821auDkmsDriver
    assert (0x0BDA, 0x0811) in {(e.vid, e.pid) for e in Rtl8821auDkmsDriver.SUPPORTED_IDS}


def test_rtl8814_default_is_dkms(monkeypatch):
    monkeypatch.delenv("WIFIT3_RTL8814", raising=False)
    assert _driver_for(0x0BDA, 0x8813) == "Rtl8814auDkmsDriver"


def test_rtl8814_mainline_opt_in(monkeypatch):
    monkeypatch.setenv("WIFIT3_RTL8814", "mainline")
    assert _driver_for(0x0BDA, 0x8813) == "RTL8814AUDriver"


def test_rtl8812_default_is_dkms(monkeypatch):
    monkeypatch.delenv("WIFIT3_RTL8812", raising=False)
    assert _driver_for(0x0BDA, 0x8812) == "Rtl8812auDkmsDriver"


def test_rtl8812_mainline_opt_in(monkeypatch):
    monkeypatch.setenv("WIFIT3_RTL8812", "MainLine")   # case-insensitive
    assert _driver_for(0x0BDA, 0x8812) == "RTL8812AUDriver"


def test_rtl8812_unknown_value_stays_dkms(monkeypatch):
    monkeypatch.setenv("WIFIT3_RTL8812", "dkms")
    assert _driver_for(0x0BDA, 0x8812) == "Rtl8812auDkmsDriver"


def test_both_8812_drivers_claim_8812():
    from wifit3.chips.rtl8812au_dkms.driver import Rtl8812auDkmsDriver
    assert (0x0BDA, 0x8812) in {(e.vid, e.pid) for e in Rtl8812auDkmsDriver.SUPPORTED_IDS}


def test_rtl8822_default_is_dkms(monkeypatch):
    monkeypatch.delenv("WIFIT3_RTL8822", raising=False)
    assert _driver_for(0x2357, 0x0138) == "Rtl8822buDkmsDriver"


def test_rtl8822_mainline_opt_in(monkeypatch):
    monkeypatch.setenv("WIFIT3_RTL8822", "MainLine")   # case-insensitive
    assert _driver_for(0x2357, 0x0138) == "RTL8822BUDriver"


def test_rtl8822_unknown_value_stays_dkms(monkeypatch):
    monkeypatch.setenv("WIFIT3_RTL8822", "dkms")
    assert _driver_for(0x2357, 0x0138) == "Rtl8822buDkmsDriver"


def test_both_8822_drivers_claim_0138():
    from wifit3.chips.rtl8822bu_dkms.driver import Rtl8822buDkmsDriver
    assert (0x2357, 0x0138) in {(e.vid, e.pid) for e in Rtl8822buDkmsDriver.SUPPORTED_IDS}
