from wifit3.chips.rtl8922au import rfk
from wifit3.chips.rtl8922au.constants import H2C_FUNC_OUTSRC_RF_MCC_INFO, H2C_FUNC_RFK_PRE_NOTIFY


def _sent_funcs(h2c):
    return [c.args[3] for c in h2c.call_args_list]


def test_pre_ntfy_mcc_is_skipped_when_the_pre_ntfy_report_times_out(mocker):
    h2c = mocker.patch.object(rfk, "_h2c")
    t = mocker.Mock()
    t.rfk_wait.enabled = True
    t.rfk_wait.wait.return_value = None
    rfk._pre_ntfy_and_wait(t, 2, b"pre", b"mcc")
    assert _sent_funcs(h2c) == [H2C_FUNC_RFK_PRE_NOTIFY]


def test_pre_ntfy_mcc_follows_a_pre_ntfy_report_ok(mocker):
    h2c = mocker.patch.object(rfk, "_h2c")
    t = mocker.Mock()
    t.rfk_wait.enabled = True
    t.rfk_wait.wait.return_value = rfk._RFK_STATE_OK
    rfk._pre_ntfy_and_wait(t, 2, b"pre", b"mcc")
    assert _sent_funcs(h2c) == [H2C_FUNC_RFK_PRE_NOTIFY, H2C_FUNC_OUTSRC_RF_MCC_INFO]
