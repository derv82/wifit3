import itertools

from wifit3.chips.rtl8922au import coex
from wifit3.chips.rtl8922au.constants import (
    BTC_BSCB_RFK_RUN, BTC_SCB_INV_VALUE, BTC_WSCB_INIT, BTC_WSCB_WLRFK,
)


def _transport(mocker, scoreboard):
    mocker.patch("wifit3.chips.rtl8922au.iopoll.time.perf_counter",
                 side_effect=(i / 1000 for i in itertools.count()))
    cfg_sb = mocker.patch.object(coex, "_cfg_sb")
    t = mocker.Mock(wl_scbd=BTC_WSCB_INIT, wl_scbd_change=False, bt_rfk_run=False, bt_rfk_req=False,
                    is_bt_iqk_timeout=False)
    t.read32.side_effect = scoreboard
    return t, cfg_sb


def test_rfk_start_waits_while_bt_calibrates(mocker):
    t, cfg_sb = _transport(mocker, [BTC_BSCB_RFK_RUN, BTC_BSCB_RFK_RUN, 0])
    coex.ntfy_wl_rfk(t, 0, start=True)
    # coex.c:6061-6081: a rejected try only updates wl->scbd; _run_coex writes it on ALLOW
    wlrfk = [bool(call.args[1] & BTC_WSCB_WLRFK) for call in cfg_sb.call_args_list]
    assert wlrfk == [True]
    assert t.is_bt_iqk_timeout is False


def test_rfk_start_times_out_once_then_tries_once(mocker, caplog):
    t, cfg_sb = _transport(mocker, itertools.repeat(BTC_BSCB_RFK_RUN))
    coex.ntfy_wl_rfk(t, 0, start=True)
    assert "RFK notify timeout" in caplog.text
    assert t.is_bt_iqk_timeout is True
    cfg_sb.assert_not_called()
    t.read32.reset_mock()
    coex.ntfy_wl_rfk(t, 0, start=True)
    assert t.read32.call_count == 1


def test_rfk_start_keeps_the_last_bt_state_on_an_invalid_scoreboard_read(mocker):
    t, cfg_sb = _transport(mocker, [BTC_BSCB_RFK_RUN, BTC_SCB_INV_VALUE, 0])
    coex.ntfy_wl_rfk(t, 0, start=True)
    assert t.read32.call_count == 3
    assert cfg_sb.call_count == 1
