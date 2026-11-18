"""set_channel failure paths against the C: ath9k_htc_config -> ath9k_htc_set_channel ->
ath9k_hw_reset, and the WMI layer under them. C paths: driver_sources/ath9k-source-v6.18.12/."""
import errno
import logging
import struct
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import usb.core

from wifit3.chips.ar9271_v2 import bringup, chan as chanmod, constants as C, hw, phy, reg as R, wmi
from wifit3.chips.ar9271_v2.driver import AR9271V2Driver
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import (
    WMI, WMI_DISABLE_INTR_CMDID, WMI_DRAIN_TXQ_ALL_CMDID, WMI_ENABLE_INTR_CMDID,
    WMI_SET_MODE_CMDID, WMI_START_RECV_CMDID, WMI_STOP_RECV_CMDID)
from wifit3.errors import SetChannelError

TIMEOUT = usb.core.USBTimeoutError("Operation timed out")
DEVICE_GONE = usb.core.USBError("No such device", errno=19)


@pytest.fixture(autouse=True)
def _fake_clock(monkeypatch):
    monkeypatch.setattr(wmi.time, "monotonic", lambda: FakeDev.clock)


class FakeDev:
    """Answers every command with a zero value; ``fail_cmds`` maps a command id to the error its
    REG_IN read raises. A failed read costs its whole timeout on the fake clock."""
    clock = 0.0

    def __init__(self, fail_cmds=None):
        self.cmds = []
        self.fail_cmds = fail_cmds or {}
        self._cmd_id = 0

    def write(self, ep, data, timeout=None):
        cmd_id, seq = struct.unpack_from(">HH", data, 8)
        self._cmd_id = cmd_id
        self.cmds.append((cmd_id, bytes(data[12:])))
        body = struct.pack(">HH", cmd_id, seq) + b"\x00\x00\x00\x00"
        self._resp = struct.pack(">BBH", 1, 0, len(body)) + b"\x00\x00\x00\x00" + body
        return len(data)

    def read(self, ep, length, timeout=None):
        if self._cmd_id in self.fail_cmds:
            FakeDev.clock += timeout / 1000
            raise self.fail_cmds[self._cmd_id]
        return bytearray(self._resp)


def _wmi(dev):
    return WMI(AR9271Transport(dev), ctrl_epid=1)


def _cmd_ids(dev):
    return [cmd_id for cmd_id, _ in dev.cmds]


# ---- WMI response framing -----------------------------------------------------------------

def test_short_wmi_frame_is_skipped():
    # ath9k_wmi_ctrl_rx drops a frame shorter than wmi_cmd_hdr [SRC] wmi.c:227-228
    dev = FakeDev()
    w = _wmi(dev)
    good = dev.read

    frames = iter([bytearray(b"\x01\x00\x00\x02\x00\x00\x00\x00\x00\x00")])

    def read(ep, length, timeout=None):
        return next(frames, None) or good(ep, length, timeout)

    dev.read = read
    assert w.reg_read(R.AR_CFG) == 0


def test_events_past_the_deadline_time_the_command_out():
    # wait_for_completion_timeout counts from the send; events don't extend it [SRC] wmi.c:348-357
    dev = FakeDev()
    w = _wmi(dev)
    good = dev.read
    event = struct.pack(">BBH", 1, 0, 8) + bytes(4) + struct.pack(">HH", 0x1001, 0) + bytes(4)
    events = iter([bytearray(event)] * 50)

    def read(ep, length, timeout=None):
        FakeDev.clock += 0.1
        return next(events, None) or good(ep, length, timeout)

    dev.read = read
    with pytest.raises(usb.core.USBTimeoutError):
        w.cmd(WMI_START_RECV_CMDID, b"")


def test_reg_in_error_keeps_waiting_for_the_reply():
    # ath9k_hif_usb_reg_in_cb resubmits after an error status [SRC] hif_usb.c
    dev = FakeDev()
    w = _wmi(dev)
    good = dev.read
    errors = iter([usb.core.USBError("Input/Output Error", errno=errno.EIO)])

    def read(ep, length, timeout=None):
        error = next(errors, None)
        if error is not None:
            raise error
        return good(ep, length, timeout)

    dev.read = read
    assert w.reg_read(R.AR_CFG) == 0


# ---- register ops: log and carry on [SRC] htc_drv_init.c:234-509 --------------------------

def test_hw_wait_timeout_logs_the_register_it_gave_up_on(caplog):
    # ath9k_hw_wait's timeout ath_dbg [SRC] hw.c:90-92
    h = hw.AthHw(_wmi(FakeDev()))
    caplog.set_level(logging.DEBUG, logger="wifit3.chips.ar9271_v2.hw")
    assert h.wait(R.AR_RTC_RC, 0x3, 0x3, timeout=2 * R.AH_TIME_QUANTUM) is False
    assert "timeout (20 us) on reg 0x7000: 0x00000000 & 0x00000003 != 0x00000003" in caplog.text


def test_failed_reg_read_returns_all_ones_after_100_jiffies(caplog):
    dev = FakeDev(fail_cmds={wmi.WMI_REG_READ_CMDID: TIMEOUT})
    start = FakeDev.clock
    with caplog.at_level(logging.DEBUG, logger="wifit3.chips.ar9271_v2.wmi"):
        assert _wmi(dev).reg_read(R.AR_CFG) == 0xFFFFFFFF
    assert FakeDev.clock - start == pytest.approx(wmi.REG_TIMEOUT_MS / 1000, abs=0.002)
    assert "REGISTER READ FAILED" in caplog.text


def test_failed_multi_reg_read_returns_all_ones():
    dev = FakeDev(fail_cmds={wmi.WMI_REG_READ_CMDID: TIMEOUT})
    assert _wmi(dev).multi_reg_read([R.AR_CFG, R.AR_CFG]) == [0xFFFFFFFF, 0xFFFFFFFF]


def test_failed_reg_write_and_rmw_carry_on(caplog):
    dev = FakeDev(fail_cmds={wmi.WMI_REG_WRITE_CMDID: TIMEOUT, wmi.WMI_REG_RMW_CMDID: TIMEOUT})
    w = _wmi(dev)
    with caplog.at_level(logging.DEBUG, logger="wifit3.chips.ar9271_v2.wmi"):
        w.reg_write(R.AR_CFG, 1)
        w.reg_rmw(R.AR_CFG, 1, 0)
        w.enable_write_buffer()
        w.reg_write(R.AR_CFG, 1)
        w.write_flush()
        w.enable_rmw_buffer()
        w.reg_rmw(R.AR_CFG, 1, 0)
        w.rmw_flush()
    assert _cmd_ids(dev) == [wmi.WMI_REG_WRITE_CMDID, wmi.WMI_REG_RMW_CMDID,
                             wmi.WMI_REG_WRITE_CMDID, wmi.WMI_REG_RMW_CMDID]
    assert [r.message.split(":")[0].split(",")[0] for r in caplog.records
            if "REGISTER" in r.message] == ["REGISTER WRITE FAILED", "REGISTER RMW FAILED",
                                            "REGISTER WRITE FAILED", "REGISTER RMW FAILED"]


def test_failed_reg_out_logs_wmi_failure_and_carries_on(caplog):
    dev = FakeDev()
    good = dev.write
    errors = iter([usb.core.USBError("Pipe error", errno=errno.EPIPE)])

    def write(ep, data, timeout=None):
        error = next(errors, None)
        if error is not None:
            raise error
        return good(ep, data, timeout)

    dev.write = write
    w = _wmi(dev)
    with caplog.at_level(logging.DEBUG, logger="wifit3.chips.ar9271_v2.wmi"):
        w.reg_write(R.AR_CFG, 1)
    assert "WMI failure for: 0x0015" in caplog.text
    assert w.reg_read(R.AR_CFG) == 0


def test_enodev_marks_the_device_removed_and_later_commands_raise_without_usb():
    dev = FakeDev(fail_cmds={wmi.WMI_REG_READ_CMDID: DEVICE_GONE})
    w = _wmi(dev)
    with pytest.raises(usb.core.USBError):
        w.reg_read(R.AR_CFG)
    with pytest.raises(usb.core.USBError) as e:
        w.reg_write(R.AR_CFG, 1)
    assert e.value.errno == errno.ENODEV
    assert _cmd_ids(dev) == [wmi.WMI_REG_READ_CMDID]


# ---- ath9k_htc_set_channel ----------------------------------------------------------------

def test_stop_commands_ignore_timeouts():
    # WMI_CMD(DISABLE_INTR / DRAIN_TXQ_ALL / STOP_RECV): ret not checked [SRC] htc_drv_main.c:266-268
    dev = FakeDev(fail_cmds={WMI_DISABLE_INTR_CMDID: TIMEOUT, WMI_DRAIN_TXQ_ALL_CMDID: TIMEOUT,
                             WMI_STOP_RECV_CMDID: TIMEOUT})
    bringup._stop_for_channel_change(_wmi(dev))
    assert _cmd_ids(dev) == [WMI_DISABLE_INTR_CMDID, WMI_DRAIN_TXQ_ALL_CMDID, WMI_STOP_RECV_CMDID]


def test_stop_commands_device_gone_propagates():
    dev = FakeDev(fail_cmds={WMI_DISABLE_INTR_CMDID: DEVICE_GONE})
    with pytest.raises(usb.core.USBError):
        bringup._stop_for_channel_change(_wmi(dev))


@pytest.mark.parametrize("failing_cmd", [WMI_START_RECV_CMDID, WMI_SET_MODE_CMDID,
                                         WMI_ENABLE_INTR_CMDID])
def test_restart_command_failure_raises(mocker, failing_cmd):
    # START_RECV / SET_MODE / ENABLE_INTR: if (ret) goto err [SRC] htc_drv_main.c:289-303
    mocker.patch.object(bringup, "rx")
    dev = FakeDev(fail_cmds={failing_cmd: TIMEOUT})
    with pytest.raises(SetChannelError):
        bringup._restart_after_channel_change(_wmi(dev), MagicMock())
    assert _cmd_ids(dev)[-1] == failing_cmd


def test_restart_device_gone_propagates(mocker):
    mocker.patch.object(bringup, "rx")
    dev = FakeDev(fail_cmds={WMI_START_RECV_CMDID: DEVICE_GONE})
    with pytest.raises(usb.core.USBError):
        bringup._restart_after_channel_change(_wmi(dev), MagicMock())


@pytest.mark.parametrize("status", [-errno.EINVAL, -errno.EIO])
def test_full_channel_change_reset_failure_skips_restart(mocker, caplog, status):
    # ath9k_hw_reset != 0 -> "Unable to reset channel", goto err (no START_RECV)
    # [SRC] htc_drv_main.c:278-283
    mocker.patch.object(bringup, "_full_channel_change_stopped", return_value=status)
    restart = mocker.patch.object(bringup, "_restart_after_channel_change")
    wmi = SimpleNamespace(cmd_or_none=MagicMock())
    with caplog.at_level(logging.ERROR), pytest.raises(SetChannelError):
        bringup.full_channel_change(wmi, MagicMock(), chanmod.channel_2ghz(6))
    restart.assert_not_called()
    assert any("Unable to reset channel (2437 Mhz)" in r.message for r in caplog.records)


def test_fast_channel_change_fallback_reset_failure_raises(mocker):
    mocker.patch.object(bringup, "_full_channel_change_stopped", return_value=-errno.EINVAL)
    restart = mocker.patch.object(bringup, "_restart_after_channel_change")
    wmi = SimpleNamespace(cmd_or_none=MagicMock())
    hw_mock = MagicMock()
    hw_mock.check_alive.return_value = False
    with pytest.raises(SetChannelError):
        bringup.fast_channel_change(wmi, hw_mock, chanmod.channel_2ghz(6))
    restart.assert_not_called()


# ---- ath9k_hw_reset -----------------------------------------------------------------------

def _hw(dev):
    h = hw.AthHw(_wmi(dev))
    h.macVersion = R.AR_SREV_VERSION_9271
    h.reset_power_on = True
    return h


def test_chip_reset_failure_returns_einval(mocker, caplog):
    # ath9k_hw_chip_reset false -> "Chip reset failed", -EINVAL [SRC] hw.c:1931-1933, :1528-1529
    h = _hw(FakeDev())
    mocker.patch.object(h, "set_reset_reg", return_value=False)
    init_pll = mocker.patch.object(h, "init_pll")
    with caplog.at_level(logging.ERROR):
        assert h.reset_begin(chanmod.channel_2ghz(6)) == -errno.EINVAL
    init_pll.assert_not_called()
    assert any("Chip reset failed" in r.message for r in caplog.records)


def test_chip_reset_failure_leaves_curchan(mocker):
    # ah->curchan moves only in ar9002_hw_set_channel, after the chip reset [SRC] ar9002_phy.c:155
    h = _hw(FakeDev())
    h.curchan = chanmod.channel_2ghz(1)
    mocker.patch.object(h, "set_reset_reg", return_value=False)
    wmi = SimpleNamespace(cmd_or_none=MagicMock())
    mocker.patch.object(h, "getnf")
    with pytest.raises(SetChannelError):
        bringup.full_channel_change(wmi, h, chanmod.channel_2ghz(6))
    assert h.curchan.channel == 1


def test_rf_set_freq_records_curchan():
    # ar9002_hw_set_channel: ah->curchan = chan [SRC] ar9002_phy.c:155
    h = _hw(FakeDev())
    phy.rf_set_freq(h, chanmod.channel_2ghz(6))
    assert h.curchan.channel == 6


def test_init_cal_failure_returns_eio_and_skips_reset_tail(mocker):
    # if (!ath9k_hw_init_cal) return -EIO [SRC] hw.c:2032-2033
    for module in ("phy", "phy_board", "mac_queue"):
        mocker.patch.object(bringup, module)
    calib = mocker.patch.object(bringup, "calib")
    calib.init_cal.return_value = False
    hw_mock = MagicMock()
    assert bringup.hw_reset_body(MagicMock(), hw_mock, chanmod.channel_2ghz(6)) == -errno.EIO
    hw_mock.reset_tail.assert_not_called()


# ---- driver.set_channel -------------------------------------------------------------------

def _driver():
    wmi = SimpleNamespace(t=SimpleNamespace(wlan_out=MagicMock()))
    endpoints = {svc: i + 1 for i, svc in enumerate(C.SERVICE_CONNECT_ORDER)}
    return AR9271V2Driver.for_replay(wmi, SimpleNamespace(curchan=chanmod.channel_2ghz(1)),
                                     endpoints)


@pytest.mark.parametrize("channel", [0, 15, 36, -1])
async def test_set_channel_rejects_channel_outside_supported(mocker, channel):
    # mac80211 only hands ath9k_htc channels from its registered 2.4 GHz band
    # [SRC] htc_drv_main.c:1210-1216 (priv->ah->channels[hw_value]); reject before any I/O.
    change = mocker.patch.object(AR9271V2Driver, "_do_channel_change")
    with pytest.raises(SetChannelError):
        await _driver().set_channel(channel)
    change.assert_not_called()


async def test_set_channel_propagates_reset_failure(mocker):
    mocker.patch.object(bringup, "full_channel_change",
                        side_effect=SetChannelError("Unable to reset channel"))
    with pytest.raises(SetChannelError):
        await _driver().set_channel(6)
