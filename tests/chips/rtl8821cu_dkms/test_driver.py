"""Hardware-free regression for the connect() detected-config log.

The reference EFUSE burn is logged untagged; a burn that selects a ported-but-HW-untested branch
is tagged `[untested variant]`, and a 2-antenna board (no ported coex module) is called out.
"""
import errno
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import usb.core

from wifit3.chips.rtl8821cu_dkms import driver as driver_mod
from wifit3.chips.rtl8821cu_dkms.driver import Rtl8821cuDkmsDriver
from wifit3.errors import SetChannelError


def _info(**kw):
    base = dict(rfe_type=0x22, chip_ver=4, default_rf_set=0, single_ant_path=1, ant_num=1,
                phydm_package_type=1, crystal_cap=0x2E, bt_coexist=True)
    base.update(kw)
    return SimpleNamespace(**base)


def _log(info, caplog):
    drv = object.__new__(Rtl8821cuDkmsDriver)     # skip __init__ (no USB device needed)
    with caplog.at_level(logging.INFO, logger="wifit3.chips.rtl8821cu_dkms.driver"):
        drv._log_detected_config(info)
    return caplog.text


def test_reference_burn_is_untagged(caplog):
    txt = _log(_info(), caplog)
    assert "rfe_type=0x22" in txt and "rf_set=BTG" in txt
    assert "untested variant" not in txt


def test_non_reference_rfe_is_tagged(caplog):
    txt = _log(_info(rfe_type=0x02, default_rf_set=0), caplog)
    assert "[untested variant]" in txt


def test_non_reference_cut_is_tagged(caplog):
    txt = _log(_info(chip_ver=0), caplog)      # A-cut
    assert "[untested variant]" in txt


def test_two_antenna_board_warned(caplog):
    txt = _log(_info(ant_num=2), caplog)
    assert "2-antenna board" in txt


async def test_a_failed_tune_keeps_the_latched_band(monkeypatch):
    """need_switch_band never rolls current_band_type back (rtl8821c_phy.c:492-494)."""
    def failing_set_channel(t, _info, channel):
        t.current_band = 1
        raise usb.core.USBError("no device", errno=errno.ENODEV)

    monkeypatch.setattr(driver_mod.chan, "set_channel", failing_set_channel)
    d = Rtl8821cuDkmsDriver(MagicMock())
    d.transport = MagicMock()
    with pytest.raises(usb.core.USBError):
        await d.set_channel(36)
    assert d.transport.current_band == 1


async def test_set_channel_rejects_an_unsupported_channel():
    d = Rtl8821cuDkmsDriver(MagicMock())
    with pytest.raises(SetChannelError):
        await d.set_channel(15)
