import json
import pathlib
import sys
import time
import pytest
from unittest.mock import MagicMock, patch
from wifit3.vault.manager import JobManager
from wifit3.models.jobs import JobAction, JobState, ToolCapability, ToolStatus, ToolResult
from wifit3.models import PersistedCapture

class DummyTool:
    def __init__(self, name="dummy", capabilities=ToolCapability.NONE):
        self.name = name
        self.label = name.title()
        self.capabilities = capabilities
        self.applied = []

    def can_crack(self, capture):
        return True

    def launch(self, job: JobState, capture):
        job.pid = 1234
        job.log_path = "dummy.log"

    def poll_status(self, job: JobState, assume_dead=False):
        return ToolResult(status=ToolStatus.RUNNING, value="Running...")

    def apply(self, action: JobAction, job: JobState):
        self.applied.append((action, job.pid))
        return True

@pytest.fixture
def manager(mocker):
    vault = MagicMock()
    # Mock save to avoid writing to disk
    mocker.patch("wifit3.vault.manager.JobManager._save")
    mocker.patch("wifit3.vault.manager.JobManager._load")

    mgr = JobManager(vault)
    mgr.tools["dummy"] = DummyTool()
    return mgr

def test_queued_jobs_share_one_capture_lookup_per_poll(manager):
    from wifit3.models.access_point import CaptureType
    captures = [
        PersistedCapture(type=CaptureType.HS, timestamp=0, path=f"capture-{index}.pcap",
                         bssid=f"00:11:22:33:44:{index:02x}")
        for index in range(3)
    ]
    manager.vault.all_captures.return_value = captures
    for capture in captures:
        manager.submit_job("dummy", capture, {})
    manager.vault.all_captures.reset_mock()

    manager.poll_jobs()

    manager.vault.all_captures.assert_called_once_with()
    assert all(job.status == ToolStatus.RUNNING for job in manager.jobs.values())


def test_job_lifecycle(manager):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="test.pcap", bssid="00:11:22:33:44:55")
    manager.vault.all_captures.return_value = [cap]
    
    job_id = manager.submit_job("dummy", cap, {"some": "config"})
    assert job_id in manager.jobs
    job = manager.jobs[job_id]
    assert job.status == ToolStatus.QUEUED
    assert job.display_name == "Dummy (test)"
    
    # First poll launches it
    manager.poll_jobs()
    assert job.status == ToolStatus.RUNNING
    assert job.pid == 1234
    
    # Second poll updates status
    with patch.object(manager.tools["dummy"], "poll_status") as mock_poll:
        mock_poll.return_value = ToolResult(status=ToolStatus.SUCCESS, value="Cracked! Key: 1234",
                                            percent=100.0, key="1234")
        manager.poll_jobs()
        
    assert job.status == ToolStatus.SUCCESS
    assert job.progress_msg == "Cracked! Key: 1234"
    assert (job.percent, job.key) == (100.0, "1234")


def _running_job(manager, actions=JobAction.STOP):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="test.pcap",
                           bssid="00:11:22:33:44:55")
    job_id = manager.submit_job("dummy", cap, {})
    job = manager.jobs[job_id]
    job.status = ToolStatus.RUNNING
    job.pid = 1234
    job.actions = actions
    return job_id, job


def test_stop_job_hands_the_stop_to_the_tool(manager):
    job_id, job = _running_job(manager)

    assert manager.stop_job(job_id) is True
    assert manager.tools["dummy"].applied == [(JobAction.STOP, 1234)]


def test_skip_wordlist_hands_the_skip_to_the_tool(manager):
    job_id, job = _running_job(manager, JobAction.STOP | JobAction.SKIP)

    assert manager.skip_wordlist(job_id) is True
    assert manager.tools["dummy"].applied == [(JobAction.SKIP, 1234)]


def test_an_action_the_job_does_not_offer_is_refused(manager):
    job_id, job = _running_job(manager, JobAction.STOP)

    assert manager.skip_wordlist(job_id) is False
    assert manager.tools["dummy"].applied == []


def test_actions_of_a_queued_job_are_clear_only(manager):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="test.pcap",
                           bssid="00:11:22:33:44:55")
    job_id = manager.submit_job("dummy", cap, {})

    assert manager.stop_job(job_id) is False
    assert manager.tools["dummy"].applied == []


def test_poll_offers_the_tools_actions_while_running_and_clear_once_finished(manager):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="test.pcap",
                           bssid="00:11:22:33:44:55")
    manager.vault.all_captures.return_value = [cap]
    job_id = manager.submit_job("dummy", cap, {})
    job = manager.jobs[job_id]
    tool = manager.tools["dummy"]

    with patch.object(tool, "poll_status") as mock_poll:
        mock_poll.return_value = ToolResult(status=ToolStatus.RUNNING, value="Running (1%)",
                                            actions=JobAction.STOP | JobAction.SKIP)
        manager.poll_jobs()        # launches
        manager.poll_jobs()        # polls
        assert job.actions == JobAction.STOP | JobAction.SKIP

        mock_poll.return_value = ToolResult(status=ToolStatus.FAILURE, value="rockyou.txt")
        manager.poll_jobs()

    assert job.status == ToolStatus.FAILURE
    assert job.actions == JobAction.CLEAR


def test_poll_persists_a_tool_that_moved_the_job_on_without_changing_status(manager):
    """The tool advancing its own queue (new pid, new tool_state) must reach jobs.json."""
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="test.pcap",
                           bssid="00:11:22:33:44:55")
    manager.vault.all_captures.return_value = [cap]
    job_id = manager.submit_job("dummy", cap, {})
    job = manager.jobs[job_id]
    manager.poll_jobs()
    job.progress_msg = "Running..."

    def _advance(polled, assume_dead=False):
        polled.pid = 5678
        polled.tool_state["index"] = 1
        return ToolResult(status=ToolStatus.RUNNING, value="Running...", actions=JobAction.STOP)

    with patch.object(manager.tools["dummy"], "poll_status", side_effect=_advance):
        manager._save.reset_mock()
        manager.poll_jobs()

    assert job.pid == 5678
    assert manager._save.called


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
                                            key="hunter2")
        manager.poll_jobs()

    assert manager.jobs[job_id].status == ToolStatus.SUCCESS
    manager.vault.save_wpa_psk.assert_called_once_with("TestNet", "00:11:22:33:44:55", "hunter2")


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
                                            key="hunter2")
        manager.poll_jobs()

    assert manager.jobs[job_id].status == ToolStatus.SUCCESS
    manager.vault.save_wpa_psk.assert_called_once_with("Cafe WiFi", "00:11:22:33:44:55", "hunter2")


def test_reconcile_resolves_dead_running_job(manager):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="test.pcap", bssid="00:11:22:33:44:55")
    manager.vault.all_captures.return_value = [cap]
    manager.tools["dummy"].capabilities = ToolCapability.ADOPTABLE

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
    manager.tools["dummy"].capabilities = ToolCapability.ADOPTABLE

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
    manager.tools["dummy"].capabilities = ToolCapability.ADOPTABLE
    job_id = manager.submit_job("dummy", cap, {})
    manager.poll_jobs()  # -> RUNNING

    with patch.object(manager.tools["dummy"], "poll_status", side_effect=RuntimeError("boom")):
        manager.reconcile_on_startup()  # must not raise

    assert manager.jobs[job_id].status == ToolStatus.ERROR


def test_reconcile_stops_a_running_job_of_a_tool_that_cannot_be_adopted(manager):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="test.pcap", bssid="00:11:22:33:44:55")
    manager.vault.all_captures.return_value = [cap]
    job_id = manager.submit_job("dummy", cap, {})
    manager.poll_jobs()  # -> RUNNING

    with patch.object(manager.tools["dummy"], "poll_status") as mock_poll:
        manager.reconcile_on_startup()

    assert manager.jobs[job_id].status == ToolStatus.STOPPED
    mock_poll.assert_not_called()


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

    assert killable.applied == [(JobAction.STOP, 1234)]
    assert headless.applied == []


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

    assert adoptable.applied == []


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


def test_a_finished_job_loaded_from_disk_can_still_be_cleared(tmp_path, mocker):
    """jobs.json has no actions column: they are derived, so a restart must not strand a job."""
    mocker.patch("wifit3.vault.manager.JobManager._save")
    mocker.patch("wifit3.persist.config.Config.captures_dir", str(tmp_path))
    (tmp_path / "jobs.json").write_text(json.dumps({"j1": {
        "job_id": "j1", "tool_name": "hashcat", "capture_path": "a.hc22000",
        "status": "FAILURE", "progress_msg": "3 wordlists",
    }}), encoding="utf-8")

    manager = JobManager(MagicMock())

    assert manager.jobs["j1"].actions == JobAction.CLEAR


def test_poll_copies_the_tools_progress_onto_the_job(manager):
    job_id, job = _running_job(manager)

    with patch.object(manager.tools["dummy"], "poll_status") as mock_poll:
        mock_poll.return_value = ToolResult(status=ToolStatus.RUNNING, value="Running (42.00%)",
                                            percent=42.0, detail="rockyou.txt (2/3)")
        manager.poll_jobs()

    assert (job.percent, job.detail, job.key) == (42.0, "rockyou.txt (2/3)", None)


def test_jobs_json_keeps_the_progress_fields(tmp_path, mocker):
    mocker.patch("wifit3.persist.config.Config.captures_dir", str(tmp_path))
    manager = JobManager(MagicMock())
    manager.jobs["j1"] = JobState(job_id="j1", tool_name="cpu_crack", capture_path="a.hc22000",
                                  status=ToolStatus.SUCCESS, progress_msg="Cracked! Key: k",
                                  percent=100.0, key="k")
    manager._save()

    loaded = JobManager(MagicMock()).jobs["j1"]

    assert (loaded.percent, loaded.detail, loaded.key) == (100.0, "", "k")


def test_submit_job_marks_an_automatic_job_and_jobs_json_keeps_it(tmp_path, mocker):
    mocker.patch("wifit3.persist.config.Config.captures_dir", str(tmp_path))
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="a.hc22000", bssid="00:11:22:33:44:55")
    manager = JobManager(MagicMock())
    automatic_id = manager.submit_job("cpu_crack", cap, {"wordlists": ["w.txt"]}, automatic=True)
    manual_id = manager.submit_job("hashcat", cap, {})

    loaded = JobManager(MagicMock()).jobs

    assert loaded[automatic_id].automatic is True
    assert loaded[manual_id].automatic is False


def test_a_job_saved_before_the_progress_fields_existed_still_loads(tmp_path, mocker):
    mocker.patch("wifit3.vault.manager.JobManager._save")
    mocker.patch("wifit3.persist.config.Config.captures_dir", str(tmp_path))
    (tmp_path / "jobs.json").write_text(json.dumps({"j1": {
        "job_id": "j1", "tool_name": "hashcat", "capture_path": "a.hc22000",
        "status": "SUCCESS", "progress_msg": "Cracked! Key: k", "display_name": "hashcat (A)",
        "pid": None, "log_path": None, "api_id": None, "config": {}, "tool_state": {},
    }}), encoding="utf-8")

    job = JobManager(MagicMock()).jobs["j1"]

    assert (job.status, job.percent, job.detail, job.key) == (ToolStatus.SUCCESS, None, "", None)
    assert job.automatic is False


def _wait_for_exit(tool, pid, seconds=10.0):
    """The fake hashcat prints one line and exits; on Windows hashcat_console is its parent,
    so wifit3 has no Popen to wait on."""
    deadline = time.monotonic() + seconds
    while tool._is_running(pid) and time.monotonic() < deadline:
        time.sleep(0.01)


def _fake_hashcat(tmp_path):
    """A stand-in executable that prints one exhausted status line and exits."""
    status = '{"status": 5, "progress": [3, 3]}'
    if sys.platform == "win32":
        exe = tmp_path / "fake-hashcat.cmd"
        exe.write_text(f"@echo {status}\r\n@exit /b 1\r\n", encoding="utf-8")
        return exe
    exe = tmp_path / "fake-hashcat.sh"
    exe.write_text(f"#!/bin/sh\necho '{status}'\nexit 1\n", encoding="utf-8")
    exe.chmod(0o755)
    return exe


def test_a_queue_of_two_wordlists_runs_both_then_reports_not_found(manager, tmp_path):
    """The whole loop against real processes: launch, exhaust, advance, exhaust, NOT FOUND."""
    from wifit3.models.access_point import CaptureType
    from wifit3.vault.tools.hashcat import HashcatTool

    capture = tmp_path / "ap.hc22000"
    capture.write_text("WPA*01*deadbeef\n")
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text("one\n")
    second.write_text("two\n")
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path=str(capture),
                           bssid="00:11:22:33:44:55")
    manager.vault.all_captures.return_value = [cap]
    tool = HashcatTool()
    manager.register_tool(tool)
    job_id = manager.submit_job("hashcat", cap, {
        "hashcat_exe": str(_fake_hashcat(tmp_path)),
        "wordlists": [str(first), str(second)],
    })
    job = manager.jobs[job_id]

    manager.poll_jobs()                                 # launches against a.txt
    assert job.status == ToolStatus.RUNNING
    assert job.tool_state["index"] == 0
    first_pid = job.pid

    _wait_for_exit(tool, job.pid)
    manager.poll_jobs()                                 # a.txt exhausted -> b.txt
    assert job.status == ToolStatus.RUNNING
    assert job.tool_state["index"] == 1
    assert job.pid != first_pid
    assert job.actions == JobAction.STOP | JobAction.PAUSE   # nothing left to skip to

    _wait_for_exit(tool, job.pid)
    manager.poll_jobs()                                 # b.txt exhausted -> done

    assert job.status == ToolStatus.FAILURE
    assert job.progress_msg == "2 wordlists"
    assert job.actions == JobAction.CLEAR
    log = pathlib.Path(job.log_path).read_text(encoding="utf-8", errors="replace")
    assert log.count("=== wordlist:") == 2, "both runs logged to the job's one log"
