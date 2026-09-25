"""Family selection for the RTL8188FTV mainline/DKMS pair.

The DKMS port is a scaffold (nothing ported): it must still win by default so
the pair behaves like every other Realtek family, with the working mainline
port one env var away. asyncio_mode=auto runs the async tests undecorated.
"""
from wifit3.chips.rtl8188ftv.driver import RTL8188FTVDriver
from wifit3.chips.rtl8188ftv_dkms import SUPPORTED_IDS
from wifit3.chips.rtl8188ftv_dkms.driver import Rtl8188ftvDkmsDriver


def test_both_drivers_claim_f179():
    assert (0x0BDA, 0xF179) in {(e.vid, e.pid) for e in SUPPORTED_IDS}


def test_ftv_default_is_dkms(monkeypatch):
    from wifit3.device import manager
    monkeypatch.delenv("WIFIT3_RTL8188FTV", raising=False)
    manager.supported_ids.cache_clear()
    try:
        assert manager.driver_for(0x0BDA, 0xF179)[0] is Rtl8188ftvDkmsDriver
    finally:
        manager.supported_ids.cache_clear()


def test_ftv_mainline_opt_in(monkeypatch):
    from wifit3.device import manager
    monkeypatch.setenv("WIFIT3_RTL8188FTV", "MainLine")   # case-insensitive
    manager.supported_ids.cache_clear()
    try:
        assert manager.driver_for(0x0BDA, 0xF179)[0] is RTL8188FTVDriver
    finally:
        manager.supported_ids.cache_clear()


def test_ftv_unknown_value_stays_dkms(monkeypatch):
    from wifit3.device import manager
    monkeypatch.setenv("WIFIT3_RTL8188FTV", "dkms")   # any non-"mainline" -> default
    manager.supported_ids.cache_clear()
    try:
        assert manager.driver_for(0x0BDA, 0xF179)[0] is Rtl8188ftvDkmsDriver
    finally:
        manager.supported_ids.cache_clear()
