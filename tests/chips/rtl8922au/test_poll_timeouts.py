from wifit3.chips.rtl8922au import phy, rfk
from wifit3.chips.rtl8922au.constants import (
    B_HWSI_ADD_POLL_MASK, B_HWSI_BUSY, CR_BASE_BE, HWSI_ADD_ADDR, INV_RF_DATA, RF_AB, RF_PATH_A,
)


def _no_sleep(mocker):
    mocker.patch("wifit3.chips.rtl8922au.iopoll.time.sleep")


def test_hwsi_write_skips_the_write_when_the_bus_stays_busy(mocker, caplog):
    _no_sleep(mocker)
    t = mocker.Mock()
    t.read32.return_value = B_HWSI_BUSY
    phy._write_full_rf_v2_a(t, RF_PATH_A, 0x18, 0x1234)
    t.write32.assert_not_called()
    assert t.read32.call_count == 3798
    assert "HWSI is busy" in caplog.text


def test_hwsi_read_returns_inv_rf_data_when_the_bus_stays_busy(mocker):
    _no_sleep(mocker)
    t = mocker.Mock()
    t.read32.return_value = B_HWSI_BUSY
    assert phy._read_full_rf_v2_a(t, RF_PATH_A, 0x18) == INV_RF_DATA
    assert t.read32.call_count == 3798


def test_hwsi_read_returns_inv_rf_data_and_clears_the_poll_when_read_done_never_sets(mocker):
    _no_sleep(mocker)
    t = mocker.Mock()
    t.read32.return_value = 0
    assert phy._read_full_rf_v2_a(t, RF_PATH_A, 0x18) == INV_RF_DATA
    t.write32_mask.assert_called_with(HWSI_ADD_ADDR[RF_PATH_A] + CR_BASE_BE, B_HWSI_ADD_POLL_MASK, 0)


def test_wait_rx_mode_gives_up_after_2500_reads_per_path(mocker):
    _no_sleep(mocker)
    read_rf = mocker.patch.object(phy, "read_rf", return_value=2)
    rfk._wait_rx_mode(mocker.Mock(), RF_AB)
    assert read_rf.call_count == 2 * 2500
