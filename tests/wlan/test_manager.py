"""Driver-registry ordering — the env-var A/B switch for the Realtek 11ac pairs.

Both the DKMS port and the mainline driver claim the same VID:PID; `_match_driver`
returns the first in `_all_drivers()`, so the env var decides which one wins.
"""
from wifit3.wlan import manager


def _names():
    return [d.__name__ for d in manager._all_drivers()]


class _FakeDev:
    def __init__(self, vid, pid):
        self.idVendor = vid
        self.idProduct = pid


def test_rtl8821_default_is_mainline(monkeypatch):
    monkeypatch.delenv("WIFIT3_RTL8821", raising=False)
    n = _names()
    assert n.index("RTL8821AUDriver") < n.index("Rtl8821auDkmsDriver")
    cls, entry = manager._match_driver(_FakeDev(0x0BDA, 0x0811))
    assert cls.__name__ == "RTL8821AUDriver"


def test_rtl8821_dkms_opt_in(monkeypatch):
    monkeypatch.setenv("WIFIT3_RTL8821", "dkms")
    n = _names()
    assert n.index("Rtl8821auDkmsDriver") < n.index("RTL8821AUDriver")
    cls, entry = manager._match_driver(_FakeDev(0x0BDA, 0x0811))
    assert cls.__name__ == "Rtl8821auDkmsDriver"


def test_rtl8821_explicit_mainline(monkeypatch):
    monkeypatch.setenv("WIFIT3_RTL8821", "mainline")
    cls, _ = manager._match_driver(_FakeDev(0x0BDA, 0x0811))
    assert cls.__name__ == "RTL8821AUDriver"


def test_both_8821_drivers_claim_0811():
    from wifit3.chips.rtl8821au_dkms.driver import Rtl8821auDkmsDriver

    ids = {(e.vid, e.pid) for e in Rtl8821auDkmsDriver.SUPPORTED_IDS}
    assert (0x0BDA, 0x0811) in ids


def test_rtl8814_default_unchanged(monkeypatch):
    # The 8814 pair stays DKMS-default — the 8821 wiring must not disturb it.
    monkeypatch.delenv("WIFIT3_RTL8814", raising=False)
    n = _names()
    assert n.index("Rtl8814auDkmsDriver") < n.index("RTL8814AUDriver")
