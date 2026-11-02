"""Family selection for RTL8188FTV: the DKMS port claims 0bda:f179."""
from wifit3.chips.rtl8188ftv_dkms import SUPPORTED_IDS
from wifit3.chips.rtl8188ftv_dkms.driver import Rtl8188ftvDkmsDriver


def test_claims_f179():
    assert (0x0BDA, 0xF179) in {(e.vid, e.pid) for e in SUPPORTED_IDS}


def test_f179_resolves_to_dkms():
    from wifit3.device import manager
    manager.supported_ids.cache_clear()
    try:
        claim = manager.driver_for(0x0BDA, 0xF179)
        assert claim == (Rtl8188ftvDkmsDriver, "rtl8188ftv")
    finally:
        manager.supported_ids.cache_clear()
