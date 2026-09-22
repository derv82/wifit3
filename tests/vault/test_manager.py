import pytest
from unittest.mock import MagicMock, patch
from wifit3.vault.manager import JobManager
from wifit3.models.jobs import ToolStatus, ToolResult
from wifit3.models import PersistedCapture

class DummyTool:
    name = "dummy"
    capabilities = 0
    
    def can_crack(self, capture):
        return True
        
    def launch(self, capture, config):
        return {"pid": 1234, "log_path": "dummy.log", "api_id": None, "capture_path": capture.path, "config": config}
        
    def poll_status(self, tracking_data):
        return ToolResult(status=ToolStatus.RUNNING, value="Running...")

@pytest.fixture
def manager(mocker):
    vault = MagicMock()
    # Mock save to avoid writing to disk
    mocker.patch("wifit3.vault.manager.JobManager._save")
    mocker.patch("wifit3.vault.manager.JobManager._load")
    
    mgr = JobManager(vault)
    mgr.tools["dummy"] = DummyTool()
    return mgr

def test_job_lifecycle(manager):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="test.pcap", bssid="00:11:22:33:44:55")
    manager.vault.all_captures.return_value = [cap]
    
    job_id = manager.submit_job("dummy", cap, {"some": "config"})
    assert job_id in manager.jobs
    job = manager.jobs[job_id]
    assert job.status == ToolStatus.QUEUED
    
    # First poll launches it
    manager.poll_jobs()
    assert job.status == ToolStatus.RUNNING
    assert job.pid == 1234
    
    # Second poll updates status
    with patch.object(manager.tools["dummy"], "poll_status") as mock_poll:
        mock_poll.return_value = ToolResult(status=ToolStatus.SUCCESS, value="Cracked! Key: 1234")
        manager.poll_jobs()
        
    assert job.status == ToolStatus.SUCCESS
    assert job.progress_msg == "Cracked! Key: 1234"


def test_crack_success_persists_key(manager, tmp_path, monkeypatch):
    from wifit3.models.access_point import CaptureType
    from wifit3.persist.config import Config
    monkeypatch.setattr(Config, "captures_dir", str(tmp_path))
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="test.pcap",
                           bssid="00:11:22:33:44:55", ssid="TestNet")
    manager.vault.all_captures.return_value = [cap]

    job_id = manager.submit_job("dummy", cap, {})
    manager.poll_jobs()  # QUEUED -> RUNNING

    with patch.object(manager.tools["dummy"], "poll_status") as mock_poll:
        mock_poll.return_value = ToolResult(status=ToolStatus.SUCCESS, value="Cracked!",
                                            result_data={"key": "hunter2"})
        manager.poll_jobs()

    assert manager.jobs[job_id].status == ToolStatus.SUCCESS
    written = list(tmp_path.glob("*_wpa_psk.txt"))
    assert len(written) == 1
    assert "hunter2" in written[0].read_text()


def test_reconcile_resolves_stale_running_job(manager):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="test.pcap", bssid="00:11:22:33:44:55")
    manager.vault.all_captures.return_value = [cap]

    job_id = manager.submit_job("dummy", cap, {})
    manager.poll_jobs()  # -> RUNNING, log_path set from DummyTool.launch
    assert manager.jobs[job_id].status == ToolStatus.RUNNING

    with patch.object(manager.tools["dummy"], "poll_status") as mock_poll:
        mock_poll.return_value = ToolResult(status=ToolStatus.ERROR, value="ended")
        manager.reconcile_on_startup()
        assert mock_poll.call_args.kwargs.get("assume_dead") is True

    assert manager.jobs[job_id].status == ToolStatus.ERROR
