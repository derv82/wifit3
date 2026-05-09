import pytest
import time
import subprocess
from unittest.mock import patch, MagicMock
from pathlib import Path
from wifit3.scripts.capture import Capture, LogHelper

def test_log_helper(tmp_path):
    logger = LogHelper(tmp_path)
    
    with patch('time.time', return_value=12345.678):
        logger.log_cmd(["echo", "hello"], "hello\n", 0, 0.5)
        
    log_file = tmp_path / "echo.log"
    assert log_file.exists()
    content = log_file.read_text()
    
    assert "[12345.678] --------------" in content
    assert "[12345.678] Executing: echo hello" in content
    assert "hello\n" in content
    assert "[12345.678] Execution completed in 0.500s, return code: 0" in content

def test_capture_run_seq_success(tmp_path):
    with patch('wifit3.scripts.capture.tempfile.TemporaryDirectory') as mock_tempdir:
        mock_tempdir.return_value.name = str(tmp_path)
        cap = Capture()
        cap.start_time = 1000.0
        cap.current_offset = 10.0
        
        mock_res = MagicMock()
        mock_res.stdout = "success_output"
        mock_res.stderr = ""
        mock_res.returncode = 0
        
        # Patch time to pretend we are perfectly on time
        with patch('time.time', return_value=1010.0), \
             patch('time.sleep') as mock_sleep, \
             patch('subprocess.run', return_value=mock_res) as mock_run:
             
            output = cap.run_seq(["fake_cmd"], 5.0)
            
            assert output == "success_output"
            assert cap.current_offset == 15.0  # 10.0 + 5.0
            mock_run.assert_called_once_with(["fake_cmd"], capture_output=True, text=True, timeout=5.0)
            mock_sleep.assert_not_called()

def test_capture_run_seq_sleeps_if_early(tmp_path):
    with patch('wifit3.scripts.capture.tempfile.TemporaryDirectory') as mock_tempdir:
        mock_tempdir.return_value.name = str(tmp_path)
        cap = Capture()
        cap.start_time = 1000.0
        cap.current_offset = 10.0
        
        mock_res = MagicMock(stdout="", stderr="", returncode=0)
        
        # We are at 1009.0, but we need to start at 1010.0
        with patch('time.time', return_value=1009.0), \
             patch('time.sleep') as mock_sleep, \
             patch('subprocess.run', return_value=mock_res):
             
            cap.run_seq(["fake_cmd"], 2.0)
            mock_sleep.assert_called_once_with(1.0)

def test_capture_run_seq_throws_on_drift(tmp_path):
    with patch('wifit3.scripts.capture.tempfile.TemporaryDirectory') as mock_tempdir:
        mock_tempdir.return_value.name = str(tmp_path)
        cap = Capture()
        cap.start_time = 1000.0
        cap.current_offset = 10.0
        
        # We are at 1011.0, 1 second late! Drift limit is 0.1
        with patch('time.time', return_value=1011.0):
            with patch.object(cap, 'throw', side_effect=SystemExit) as mock_throw:
                with pytest.raises(SystemExit):
                    cap.run_seq(["fake_cmd"], 2.0)
                mock_throw.assert_called_once()
                assert "TIMELINE CORRUPTION" in mock_throw.call_args[0][0]

def test_capture_run_seq_throws_on_timeout(tmp_path):
    with patch('wifit3.scripts.capture.tempfile.TemporaryDirectory') as mock_tempdir:
        mock_tempdir.return_value.name = str(tmp_path)
        cap = Capture()
        cap.start_time = 1000.0
        cap.current_offset = 10.0
        
        with patch('time.time', return_value=1010.0), \
             patch('subprocess.run', side_effect=subprocess.TimeoutExpired(["fake"], 2.0)):
             
            with patch.object(cap, 'throw', side_effect=SystemExit) as mock_throw:
                with pytest.raises(SystemExit):
                    cap.run_seq(["fake_cmd"], 2.0)
                mock_throw.assert_called_once()
                assert "TIMEOUT EXPIRED" in mock_throw.call_args[0][0]
