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


def test_rtl8821_default_is_dkms(monkeypatch):
    monkeypatch.delenv("WIFIT3_RTL8821", raising=False)
    n = _names()
    assert n.index("Rtl8821auDkmsDriver") < n.index("RTL8821AUDriver")
    cls, entry = manager._match_driver(_FakeDev(0x0BDA, 0x0811))
    assert cls.__name__ == "Rtl8821auDkmsDriver"


def test_rtl8821_mainline_opt_in(monkeypatch):
    monkeypatch.setenv("WIFIT3_RTL8821", "MainLine")   # case-insensitive
    n = _names()
    assert n.index("RTL8821AUDriver") < n.index("Rtl8821auDkmsDriver")
    cls, entry = manager._match_driver(_FakeDev(0x0BDA, 0x0811))
    assert cls.__name__ == "RTL8821AUDriver"


def test_rtl8821_unknown_value_stays_dkms(monkeypatch):
    monkeypatch.setenv("WIFIT3_RTL8821", "dkms")   # any non-"mainline" -> default DKMS
    cls, _ = manager._match_driver(_FakeDev(0x0BDA, 0x0811))
    assert cls.__name__ == "Rtl8821auDkmsDriver"


def test_both_8821_drivers_claim_0811():
    from wifit3.chips.rtl8821au_dkms.driver import Rtl8821auDkmsDriver

    ids = {(e.vid, e.pid) for e in Rtl8821auDkmsDriver.SUPPORTED_IDS}
    assert (0x0BDA, 0x0811) in ids


def test_rtl8814_default_unchanged(monkeypatch):
    # The 8814 pair stays DKMS-default — the 8821 wiring must not disturb it.
    monkeypatch.delenv("WIFIT3_RTL8814", raising=False)
    n = _names()
    assert n.index("Rtl8814auDkmsDriver") < n.index("RTL8814AUDriver")


def test_rtl8812_default_is_dkms(monkeypatch):
    # A/B proved the DKMS port out: it survives the 2.4+5 GHz hop that RF-synth-wedges
    # mainline, so it is now the default (matching the 8821/8814 polarity).
    monkeypatch.delenv("WIFIT3_RTL8812", raising=False)
    n = _names()
    assert n.index("Rtl8812auDkmsDriver") < n.index("RTL8812AUDriver")
    cls, _ = manager._match_driver(_FakeDev(0x0BDA, 0x8812))
    assert cls.__name__ == "Rtl8812auDkmsDriver"


def test_rtl8812_mainline_opt_in(monkeypatch):
    monkeypatch.setenv("WIFIT3_RTL8812", "MainLine")   # case-insensitive
    n = _names()
    assert n.index("RTL8812AUDriver") < n.index("Rtl8812auDkmsDriver")
    cls, _ = manager._match_driver(_FakeDev(0x0BDA, 0x8812))
    assert cls.__name__ == "RTL8812AUDriver"


def test_rtl8812_unknown_value_stays_dkms(monkeypatch):
    monkeypatch.setenv("WIFIT3_RTL8812", "dkms")   # any non-"mainline" -> default DKMS
    cls, _ = manager._match_driver(_FakeDev(0x0BDA, 0x8812))
    assert cls.__name__ == "Rtl8812auDkmsDriver"


def test_both_8812_drivers_claim_8812():
    from wifit3.chips.rtl8812au_dkms.driver import Rtl8812auDkmsDriver

    ids = {(e.vid, e.pid) for e in Rtl8812auDkmsDriver.SUPPORTED_IDS}
    assert (0x0BDA, 0x8812) in ids
