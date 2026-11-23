from wifit3.chips.rtl8821cu_dkms import firmware


def test_send_h2c_by_reg_waits_for_a_busy_box(mocker):
    sleep = mocker.patch("wifit3.chips.rtl8821cu_dkms.firmware.time.sleep")
    t = mocker.Mock(last_hme_box=1)
    t.read8.side_effect = [0x02, 0x00]
    assert firmware.send_h2c_by_reg(t, bytes(8)) is True
    sleep.assert_called_once_with(0.001)
    assert t.write32.call_count == 2
    assert t.last_hme_box == 2


def test_send_h2c_by_reg_skips_the_h2c_when_the_box_stays_busy(mocker):
    mocker.patch("wifit3.chips.rtl8821cu_dkms.firmware.time.sleep")
    t = mocker.Mock(last_hme_box=1)
    t.read8.return_value = 0x02
    assert firmware.send_h2c_by_reg(t, bytes(8)) is False
    assert t.read8.call_count == 102
    t.write32.assert_not_called()
    assert t.last_hme_box == 1
