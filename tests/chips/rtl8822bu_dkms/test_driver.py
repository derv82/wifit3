"""Hardware-free regression for the connect() detected-config log.

The reference EFUSE/chip-cut burn (rfe_type 3 iFEM, D-cut) is logged untagged; a burn that selects a
ported-but-HW-untested branch is tagged `[untested variant]`, and an rfe 15/18 board (RFE pinmux not
ported, iFEM fallback) gets an explicit warning.
"""
import logging
from types import SimpleNamespace

from wifit3.chips.rtl8822bu_dkms.driver import Rtl8822buDkmsDriver


def _chip(rfe_type=3, chip_ver=3):
    info = SimpleNamespace(chip_ver=chip_ver)
    e = SimpleNamespace(rfe_type=rfe_type, crystal_cap=0x2E, thermal_meter=0x12,
                        mac_address="00:11:22:33:44:55")
    return info, e


def _log(info, e, caplog):
    drv = object.__new__(Rtl8822buDkmsDriver)     # skip __init__ (no USB device needed)
    with caplog.at_level(logging.INFO, logger="wifit3.chips.rtl8822bu_dkms.driver"):
        drv._log_detected_config(info, e)
    return caplog.text


def test_reference_burn_is_untagged(caplog):
    txt = _log(*_chip(), caplog)
    assert "rfe_type=3" in txt and "cut=3" in txt
    assert "untested variant" not in txt


def test_non_reference_rfe_is_tagged(caplog):
    info, e = _chip(rfe_type=1)                    # eFEM
    txt = _log(info, e, caplog)
    assert "[untested variant]" in txt


def test_non_reference_cut_is_tagged(caplog):
    info, e = _chip(chip_ver=1)                    # B-cut
    txt = _log(info, e, caplog)
    assert "[untested variant]" in txt


def test_unported_rfe_pinmux_warns(caplog):
    info, e = _chip(rfe_type=15)                   # phydm_8822b_type15_rfe not ported
    with caplog.at_level(logging.WARNING, logger="wifit3.chips.rtl8822bu_dkms.driver"):
        _log(info, e, caplog)
    assert "not ported" in caplog.text and "iFEM fallback" in caplog.text
