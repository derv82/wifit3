import pytest
from unittest.mock import MagicMock, patch
from wifit3.vault.manager import JobManager
from wifit3.models.jobs import ToolCapability, ToolStatus, ToolResult
from wifit3.models import PersistedCapture

class DummyTool:
    def __init__(self, name="dummy", capabilities=ToolCapability.NONE):
        self.name = name
        self.capabilities = capabilities
        self.killed = []

    def can_crack(self, capture):
        return True

    def launch(self, capture, config):
        return {"pid": 1234, "log_path": "dummy.log", "api_id": None, "capture_path": capture.path, "config": config}

    def poll_status(self, tracking_data, assume_dead=False):
        return ToolResult(status=ToolStatus.RUNNING, value="Running...")

    def kill(self, tracking_data):
        self.killed.append(tracking_data.get("pid"))

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


def test_crack_success_writes_real_essid(manager, tmp_path, monkeypatch):
    from wifit3.models.access_point import CaptureType
    from wifit3.persist.config import Config
    monkeypatch.setattr(Config, "captures_dir", str(tmp_path))
    # The hashline essid ("Cafe WiFi") is the lossless SSID, vs the sanitized filename name.
    cap_file = tmp_path / "Cafe_WiFi_00-11-22-33-44-55.hc22000"
    essid_hex = "Cafe WiFi".encode("utf-8").hex()
    cap_file.write_text(f"WPA*01*{'0' * 32}*001122334455*aabbccddeeff*{essid_hex}***\n")
    cap = PersistedCapture(type=CaptureType.PMKID, timestamp=0, path=str(cap_file),
                           bssid="00:11:22:33:44:55", ssid="Cafe_WiFi")
    manager.vault.all_captures.return_value = [cap]

    job_id = manager.submit_job("dummy", cap, {})
    manager.poll_jobs()
    with patch.object(manager.tools["dummy"], "poll_status") as mock_poll:
        mock_poll.return_value = ToolResult(status=ToolStatus.SUCCESS, value="Cracked!",
                                            result_data={"key": "hunter2"})
        manager.poll_jobs()

    assert manager.jobs[job_id].status == ToolStatus.SUCCESS
    written = list(tmp_path.glob("*_wpa_psk.txt"))[0].read_text()
    assert "SSID: Cafe WiFi" in written


def test_reconcile_resolves_dead_running_job(manager):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="test.pcap", bssid="00:11:22:33:44:55")
    manager.vault.all_captures.return_value = [cap]

    job_id = manager.submit_job("dummy", cap, {})
    manager.poll_jobs()  # -> RUNNING, log_path set from DummyTool.launch
    assert manager.jobs[job_id].status == ToolStatus.RUNNING

    with patch.object(manager.tools["dummy"], "poll_status") as mock_poll:
        mock_poll.return_value = ToolResult(status=ToolStatus.ERROR, value="ended")
        manager.reconcile_on_startup()

    assert manager.jobs[job_id].status == ToolStatus.ERROR


def test_reconcile_adopts_live_running_job(manager):
    """A process still alive at startup (tool reports RUNNING) is kept RUNNING, not resolved."""
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="test.pcap", bssid="00:11:22:33:44:55")
    manager.vault.all_captures.return_value = [cap]

    job_id = manager.submit_job("dummy", cap, {})
    manager.poll_jobs()  # -> RUNNING
    # DummyTool.poll_status reports RUNNING, i.e. the adopted process is still alive.
    manager.reconcile_on_startup()

    assert manager.jobs[job_id].status == ToolStatus.RUNNING


def test_reconcile_isolates_a_failing_job(manager):
    """One job whose poll_status raises is marked ERROR without aborting the loop."""
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="test.pcap", bssid="00:11:22:33:44:55")
    manager.vault.all_captures.return_value = [cap]
    job_id = manager.submit_job("dummy", cap, {})
    manager.poll_jobs()  # -> RUNNING

    with patch.object(manager.tools["dummy"], "poll_status", side_effect=RuntimeError("boom")):
        manager.reconcile_on_startup()  # must not raise

    assert manager.jobs[job_id].status == ToolStatus.ERROR


def test_singleton_tool_runs_one_at_a_time(manager):
    from wifit3.models.access_point import CaptureType
    manager.tools["dummy"].capabilities = ToolCapability.SINGLETON
    cap1 = PersistedCapture(type=CaptureType.HS, timestamp=0, path="a.pcap", bssid="00:11:22:33:44:55")
    cap2 = PersistedCapture(type=CaptureType.HS, timestamp=0, path="b.pcap", bssid="00:11:22:33:44:66")
    manager.vault.all_captures.return_value = [cap1, cap2]

    id1 = manager.submit_job("dummy", cap1, {})
    id2 = manager.submit_job("dummy", cap2, {})
    manager.poll_jobs()

    assert manager.jobs[id1].status == ToolStatus.RUNNING
    assert manager.jobs[id2].status == ToolStatus.QUEUED


def test_non_singleton_tool_allows_concurrent_jobs(manager):
    from wifit3.models.access_point import CaptureType
    cap1 = PersistedCapture(type=CaptureType.HS, timestamp=0, path="a.pcap", bssid="00:11:22:33:44:55")
    cap2 = PersistedCapture(type=CaptureType.HS, timestamp=0, path="b.pcap", bssid="00:11:22:33:44:66")
    manager.vault.all_captures.return_value = [cap1, cap2]

    id1 = manager.submit_job("dummy", cap1, {})
    id2 = manager.submit_job("dummy", cap2, {})
    manager.poll_jobs()

    assert manager.jobs[id1].status == ToolStatus.RUNNING
    assert manager.jobs[id2].status == ToolStatus.RUNNING


def test_kill_all_running_respects_killable(manager):
    from wifit3.models.access_point import CaptureType
    killable = DummyTool(name="killable", capabilities=ToolCapability.KILLABLE)
    headless = DummyTool(name="headless", capabilities=ToolCapability.NONE)
    manager.tools["killable"] = killable
    manager.tools["headless"] = headless
    capk = PersistedCapture(type=CaptureType.HS, timestamp=0, path="k.pcap", bssid="00:11:22:33:44:55")
    caph = PersistedCapture(type=CaptureType.HS, timestamp=0, path="h.pcap", bssid="00:11:22:33:44:66")
    manager.vault.all_captures.return_value = [capk, caph]
    manager.submit_job("killable", capk, {})
    manager.submit_job("headless", caph, {})
    manager.poll_jobs()  # both RUNNING

    manager.kill_all_running()

    assert killable.killed == [1234]
    assert headless.killed == []


def test_kill_all_running_leaves_adoptable_alive(manager):
    """An ADOPTABLE tool (e.g. hashcat) is left running on shutdown so its job survives to be
    adopted on the next launch."""
    from wifit3.models.access_point import CaptureType
    adoptable = DummyTool(name="adoptable",
                          capabilities=ToolCapability.KILLABLE | ToolCapability.ADOPTABLE)
    manager.tools["adoptable"] = adoptable
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="ad.pcap", bssid="00:11:22:33:44:77")
    manager.vault.all_captures.return_value = [cap]
    manager.submit_job("adoptable", cap, {})
    manager.poll_jobs()  # RUNNING

    manager.kill_all_running()

    assert adoptable.killed == []


def test_load_skips_bad_entry_keeps_good(tmp_path, monkeypatch):
    import json
    from wifit3.persist.config import Config
    monkeypatch.setattr(Config, "captures_dir", str(tmp_path))
    good = {"job_id": "g", "tool_name": "hashcat", "capture_path": "x.hc22000",
            "status": "RUNNING", "progress_msg": "..."}
    bad = {"job_id": "b", "status": "RUNNING"}   # missing required fields -> construction fails
    (tmp_path / "jobs.json").write_text(json.dumps({"g": good, "b": bad}), encoding="utf-8")

    mgr = JobManager(MagicMock())   # __init__ runs the real _load

    assert "g" in mgr.jobs and "b" not in mgr.jobs
