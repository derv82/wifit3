from unittest.mock import Mock

import pytest

from wifit3.chips.rtl8822cu import cal


def test_measure_rejects_insufficient_samples(monkeypatch):
    sample = Mock(return_value=(101, 0))
    monkeypatch.setattr(cal, "_read_sample", sample)
    with pytest.raises(cal.DackCalibrationError, match="0/100 samples"):
        cal.measure(object())
    assert sample.call_count == 10000


def test_measure_rejects_unsettled_spread(monkeypatch):
    sample = Mock(side_effect=[(0, 0), (10, 10)] * 150)
    monkeypatch.setattr(cal, "_read_sample", sample)
    with pytest.raises(cal.DackCalibrationError, match="spread"):
        cal.measure(object())
    assert sample.call_count == 300


def test_poll_accepts_last_read_and_reports_timeout(monkeypatch):
    read = Mock(side_effect=[0] * 99999 + [1])
    monkeypatch.setattr(cal, "get_bb_reg", read)
    cal._poll(object(), 0x2808, 1, 1)
    assert read.call_count == 100000
    read.reset_mock(side_effect=True)
    read.return_value = 0
    with pytest.raises(cal.DackCalibrationError, match="0x2808"):
        cal._poll(object(), 0x2808, 1, 1)
    assert read.call_count == 100000


@pytest.mark.parametrize("path", [cal._PATH_A, cal._PATH_B])
def test_adck_convergence_limit(monkeypatch, path):
    monkeypatch.setattr(cal, "set_bb_reg", Mock())
    measure = Mock(return_value=(5, 0))
    monkeypatch.setattr(cal, "measure", measure)
    with pytest.raises(cal.DackCalibrationError, match="ADCK did not converge"):
        cal._adck_loop(object(), path)
    assert measure.call_count == 20


@pytest.mark.parametrize("path", [cal._PATH_A, cal._PATH_B])
def test_dack_convergence_limit(monkeypatch, path):
    monkeypatch.setattr(cal, "set_bb_reg", Mock())
    monkeypatch.setattr(cal, "_poll", Mock())
    monkeypatch.setattr(cal.time, "sleep", Mock())
    measure = Mock(side_effect=[(0, 0), (21, 16)] * 10)
    monkeypatch.setattr(cal, "measure", measure)
    with pytest.raises(cal.DackCalibrationError, match="DACK did not converge"):
        cal._dack_loop(Mock(), path, 0, 0, 0)
    assert measure.call_count == 20


def test_failed_calibration_is_not_cached(monkeypatch):
    state = cal.DackState(valid=True)
    monkeypatch.setattr(cal, "get_bb_reg", Mock(return_value=0))
    monkeypatch.setattr(cal, "get_rf_reg", Mock(return_value=0))
    monkeypatch.setattr(cal, "set_bb_reg", Mock())
    monkeypatch.setattr(cal, "set_rf_reg", Mock())
    monkeypatch.setattr(cal, "_adck_loop", Mock(side_effect=cal.DackCalibrationError("failed")))
    backup = Mock()
    monkeypatch.setattr(cal, "_dack_backup", backup)
    with pytest.raises(cal.DackCalibrationError):
        cal.dac_cal(Mock(), state)
    assert not state.valid and not state.dack_en
    backup.assert_not_called()


def test_success_sets_valid_only_after_backup(monkeypatch):
    state = cal.DackState()
    monkeypatch.setattr(cal, "get_bb_reg", Mock(return_value=0))
    monkeypatch.setattr(cal, "get_rf_reg", Mock(return_value=0))
    monkeypatch.setattr(cal, "set_bb_reg", Mock())
    monkeypatch.setattr(cal, "set_rf_reg", Mock())
    monkeypatch.setattr(cal, "_adck_loop", Mock(return_value=(0, 0, 0)))
    monkeypatch.setattr(cal, "_dack_loop", Mock())

    def backup(t, dack):
        assert not dack.valid and not dack.dack_en

    monkeypatch.setattr(cal, "_dack_backup", backup)
    cal.dac_cal(Mock(), state)
    assert state.valid and state.dack_en
