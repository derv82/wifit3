import json
import pathlib
import sys
import time
import pytest
from concurrent.futures import Future
from unittest.mock import MagicMock, patch
from wifit3.vault.manager import JobManager
from wifit3.models.jobs import JobAction, JobState, ToolCapability, ToolStatus, ToolResult
from wifit3.models import PersistedCapture
from wifit3.crack import wpa_psk
from wifit3.dot11.wpa import derive_pmk

class DummyTool:
    def __init__(self, name="dummy", capabilities=ToolCapability.NONE):
        self.name = name
        self.label = name.title()
        self.capabilities = capabilities
        self.applied = []

    def can_crack(self, lines):
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
        manager.submit_job("dummy", capture, {}, [])
    manager.vault.all_captures.reset_mock()

    manager.poll_jobs()

    manager.vault.all_captures.assert_called_once_with()
    assert all(job.status == ToolStatus.RUNNING for job in manager.jobs.values())


def test_job_lifecycle(manager):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="test.pcap", bssid="00:11:22:33:44:55")
    manager.vault.all_captures.return_value = [cap]
    
    job_id = manager.submit_job("dummy", cap, {"some": "config"}, [])
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
    job_id = manager.submit_job("dummy", cap, {}, [])
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
    job_id = manager.submit_job("dummy", cap, {}, [])

    assert manager.stop_job(job_id) is False
    assert manager.tools["dummy"].applied == []


def test_poll_offers_the_tools_actions_while_running_and_clear_once_finished(manager):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="test.pcap",
                           bssid="00:11:22:33:44:55")
    manager.vault.all_captures.return_value = [cap]
    job_id = manager.submit_job("dummy", cap, {}, [])
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
    job_id = manager.submit_job("dummy", cap, {}, [])
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


def test_poll_notifies_the_ap_only_when_the_job_moves_to_another_wordlist_or_ends(manager):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="test.pcap",
                           bssid="00:11:22:33:44:55")
    manager.vault.all_captures.return_value = [cap]
    manager.submit_job("dummy", cap, {}, [])
    manager.poll_jobs()
    results = iter([ToolResult(status=ToolStatus.RUNNING, value="1%", detail="a.txt (1/2)"),
                    ToolResult(status=ToolStatus.RUNNING, value="2%", detail="a.txt (1/2)"),
                    ToolResult(status=ToolStatus.RUNNING, value="Starting...", detail="b.txt (2/2)"),
                    ToolResult(status=ToolStatus.FAILURE, value="2 wordlists", detail="b.txt (2/2)")])

    with patch.object(manager.tools["dummy"], "poll_status", side_effect=lambda job, assume_dead=False: next(results)):
        for _ in range(4):
            manager.poll_jobs()

    assert manager.vault.notify_changed.call_args_list == [((cap.bssid,),)] * 3


def test_crack_success_persists_key(manager):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="test.pcap",
                           bssid="00:11:22:33:44:55", ssid="TestNet")
    manager.vault.all_captures.return_value = [cap]

    job_id = manager.submit_job("dummy", cap, {}, [])
    manager.poll_jobs()  # QUEUED -> RUNNING

    with patch.object(manager.tools["dummy"], "poll_status") as mock_poll:
        mock_poll.return_value = ToolResult(status=ToolStatus.SUCCESS, value="Cracked!",
                                            key="hunter2")
        manager.poll_jobs()

    assert manager.jobs[job_id].status == ToolStatus.SUCCESS
    manager.vault.save_wpa_psk.assert_called_once_with("TestNet", "00:11:22:33:44:55", "hunter2")
    manager.vault.propagate_verified_psk.assert_called_once_with(
        "00:11:22:33:44:55", "TestNet", "hunter2")


def test_a_failed_propagation_to_related_aps_is_logged_and_the_job_still_succeeds(manager, caplog):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="test.pcap",
                           bssid="00:11:22:33:44:55", ssid="TestNet")
    propagation: Future = Future()
    manager.vault.propagate_verified_psk.return_value = propagation
    manager.vault.all_captures.return_value = [cap]
    job_id = manager.submit_job("dummy", cap, {}, [])
    manager.poll_jobs()
    with patch.object(manager.tools["dummy"], "poll_status") as mock_poll:
        mock_poll.return_value = ToolResult(status=ToolStatus.SUCCESS, value="Cracked!", key="hunter2")
        manager.poll_jobs()                 # returns before the propagation ends

    propagation.set_exception(OSError("disk full"))

    assert manager.jobs[job_id].status == ToolStatus.SUCCESS
    assert "Failed to pass a recovered key on to related APs" in caplog.text


def _pmkid_line(psk: str, ssid: str) -> str:
    ap, sta = bytes.fromhex("001122334455"), bytes.fromhex("aabbccddeeff")
    pmkid = wpa_psk.pmkid(derive_pmk(psk, ssid.encode()), ap, sta)
    return f"WPA*01*{pmkid.hex()}*{ap.hex()}*{sta.hex()}*{ssid.encode().hex()}***"


def test_crack_success_saves_the_key_under_the_ssid_of_the_line_it_cracks(manager):
    """The lossless hashline ESSID of the line the key cracks, not the first line's."""
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.PMKID, timestamp=0, path="Cafe_WiFi_00-11-22-33-44-55.hc22000",
                           bssid="00:11:22:33:44:55", ssid="Cafe_WiFi")
    manager.vault.all_captures.return_value = [cap]
    hashlines = [_pmkid_line("newpass1", "Cafe WiFi"), _pmkid_line("oldpass1", "Old Cafe")]

    job_id = manager.submit_job("dummy", cap, {}, hashlines)
    manager.poll_jobs()
    with patch.object(manager.tools["dummy"], "poll_status") as mock_poll:
        mock_poll.return_value = ToolResult(status=ToolStatus.SUCCESS, value="Cracked!",
                                            key="oldpass1")
        manager.poll_jobs()

    assert manager.jobs[job_id].status == ToolStatus.SUCCESS
    manager.vault.save_wpa_psk.assert_called_once_with("Old Cafe", "00:11:22:33:44:55", "oldpass1")


def _crack(manager, cap: PersistedCapture, hashlines: list[str], key: str) -> None:
    manager.vault.all_captures.return_value = [cap]
    manager.submit_job("dummy", cap, {}, hashlines)
    manager.poll_jobs()
    with patch.object(manager.tools["dummy"], "poll_status") as mock_poll:
        mock_poll.return_value = ToolResult(status=ToolStatus.SUCCESS, value="Cracked!", key=key)
        manager.poll_jobs()


def test_a_key_that_cracks_lines_of_two_ssids_is_saved_under_both(manager):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.PMKID, timestamp=0, path="Cafe_WiFi_00-11-22-33-44-55.hc22000",
                           bssid="00:11:22:33:44:55", ssid="Cafe_WiFi")

    _crack(manager, cap, [_pmkid_line("samepass", "Cafe WiFi"), _pmkid_line("samepass", "Old Cafe")],
           "samepass")

    assert [c.args for c in manager.vault.save_wpa_psk.call_args_list] == [
        ("Cafe WiFi", "00:11:22:33:44:55", "samepass"), ("Old Cafe", "00:11:22:33:44:55", "samepass")]
    assert [c.args for c in manager.vault.propagate_verified_psk.call_args_list] == [
        ("00:11:22:33:44:55", "Cafe WiFi", "samepass"), ("00:11:22:33:44:55", "Old Cafe", "samepass")]


def test_a_key_no_line_verifies_is_saved_under_the_first_lines_ssid_not_the_filename_one(manager):
    """hashcat's nonce-error-correction cracks lines a plain PMKID/MIC check rejects."""
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.PMKID, timestamp=0, path="Cafe_WiFi_00-11-22-33-44-55.hc22000",
                           bssid="00:11:22:33:44:55", ssid="Cafe_WiFi")

    _crack(manager, cap, [_pmkid_line("newpass1", "Cafe WiFi")], "not-the-psk")

    manager.vault.save_wpa_psk.assert_called_once_with("Cafe WiFi", "00:11:22:33:44:55", "not-the-psk")


def test_a_job_whose_capture_is_gone_still_launches_and_saves_its_key_from_its_own_hashlines(manager):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.PMKID, timestamp=0, path="Cafe_WiFi_00-11-22-33-44-55.hc22000",
                           bssid="00:11:22:33:44:55", ssid="Cafe_WiFi")
    job_id = manager.submit_job("dummy", cap, {}, [_pmkid_line("newpass1", "Cafe WiFi")])
    manager.vault.all_captures.return_value = []

    manager.poll_jobs()
    assert manager.jobs[job_id].status == ToolStatus.RUNNING
    with patch.object(manager.tools["dummy"], "poll_status") as mock_poll:
        mock_poll.return_value = ToolResult(status=ToolStatus.SUCCESS, value="Cracked!", key="newpass1")
        manager.poll_jobs()

    manager.vault.save_wpa_psk.assert_called_once_with("Cafe WiFi", "00:11:22:33:44:55", "newpass1")


def test_a_job_with_neither_capture_nor_hashlines_errors(manager):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.PMKID, timestamp=0, path="gone.hc22000", bssid="00:11:22:33:44:55")
    job_id = manager.submit_job("dummy", cap, {}, [])
    manager.vault.all_captures.return_value = []

    manager.poll_jobs()

    assert manager.jobs[job_id].status == ToolStatus.ERROR


def test_reconcile_recovers_the_key_of_a_hashcat_job_from_an_older_jobs_json(tmp_path):
    """Its hashcat was started on the capture, with the potfile beside it."""
    from wifit3.models.access_point import CaptureType
    capture = tmp_path / "ASUS_00-11-22-33-44-55.hc22000"
    capture.write_text(f"{_pmkid_line('hunter22', 'ASUS')}\n", encoding="utf-8")
    capture.with_suffix(".potfile").write_text("hash*41535553:hunter22\n", encoding="utf-8")
    log = tmp_path / "ASUS_00-11-22-33-44-55.hashcat.log"
    log.write_text("", encoding="utf-8")
    (tmp_path / "jobs.json").write_text(json.dumps({"j1": {
        "job_id": "j1", "tool_name": "hashcat", "capture_path": str(capture), "status": "RUNNING",
        "progress_msg": "Running", "pid": 999999, "log_path": str(log),
        "config": {"hashcat_exe": sys.executable}, "tool_state": {"wordlists": ["a.txt"]},
    }}), encoding="utf-8")
    vault = MagicMock()
    vault.all_captures.return_value = [PersistedCapture(
        type=CaptureType.PMKID, timestamp=0, path=str(capture), bssid="00:11:22:33:44:55", ssid="ASUS")]
    manager = JobManager(vault)

    manager.reconcile_on_startup()

    assert manager.jobs["j1"].status == ToolStatus.SUCCESS
    vault.save_wpa_psk.assert_called_once_with("ASUS", "00:11:22:33:44:55", "hunter22")


def test_reconcile_resolves_dead_running_job(manager):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="test.pcap", bssid="00:11:22:33:44:55")
    manager.vault.all_captures.return_value = [cap]
    manager.tools["dummy"].capabilities = ToolCapability.ADOPTABLE

    job_id = manager.submit_job("dummy", cap, {}, [])
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

    job_id = manager.submit_job("dummy", cap, {}, [])
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
    job_id = manager.submit_job("dummy", cap, {}, [])
    manager.poll_jobs()  # -> RUNNING

    with patch.object(manager.tools["dummy"], "poll_status", side_effect=RuntimeError("boom")):
        manager.reconcile_on_startup()  # must not raise

    assert manager.jobs[job_id].status == ToolStatus.ERROR


def test_reconcile_stops_a_running_job_of_a_tool_that_cannot_be_adopted(manager):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="test.pcap", bssid="00:11:22:33:44:55")
    manager.vault.all_captures.return_value = [cap]
    job_id = manager.submit_job("dummy", cap, {}, [])
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

    id1 = manager.submit_job("dummy", cap1, {}, [])
    id2 = manager.submit_job("dummy", cap2, {}, [])
    manager.poll_jobs()

    assert manager.jobs[id1].status == ToolStatus.RUNNING
    assert manager.jobs[id2].status == ToolStatus.QUEUED


def test_non_singleton_tool_allows_concurrent_jobs(manager):
    from wifit3.models.access_point import CaptureType
    cap1 = PersistedCapture(type=CaptureType.HS, timestamp=0, path="a.pcap", bssid="00:11:22:33:44:55")
    cap2 = PersistedCapture(type=CaptureType.HS, timestamp=0, path="b.pcap", bssid="00:11:22:33:44:66")
    manager.vault.all_captures.return_value = [cap1, cap2]

    id1 = manager.submit_job("dummy", cap1, {}, [])
    id2 = manager.submit_job("dummy", cap2, {}, [])
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
    manager.submit_job("killable", capk, {}, [])
    manager.submit_job("headless", caph, {}, [])
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
    manager.submit_job("adoptable", cap, {}, [])
    manager.poll_jobs()  # RUNNING

    manager.kill_all_running()

    assert adoptable.applied == []


def test_load_skips_bad_entry_keeps_good(tmp_path):
    import json
    good = {"job_id": "g", "tool_name": "hashcat", "capture_path": "x.hc22000",
            "status": "RUNNING", "progress_msg": "..."}
    bad = {"job_id": "b", "status": "RUNNING"}   # missing required fields -> construction fails
    (tmp_path / "jobs.json").write_text(json.dumps({"g": good, "b": bad}), encoding="utf-8")

    mgr = JobManager(MagicMock())   # __init__ runs the real _load

    assert "g" in mgr.jobs and "b" not in mgr.jobs


def test_a_finished_job_loaded_from_disk_can_still_be_cleared(tmp_path, mocker):
    """jobs.json has no actions column: they are derived, so a restart must not strand a job."""
    mocker.patch("wifit3.vault.manager.JobManager._save")
    (tmp_path / "jobs.json").write_text(json.dumps({"j1": {
        "job_id": "j1", "tool_name": "hashcat", "capture_path": "a.hc22000",
        "status": "FAILURE", "progress_msg": "3 wordlists",
    }}), encoding="utf-8")

    manager = JobManager(MagicMock())

    assert manager.jobs["j1"].actions == JobAction.CLEAR


def test_clearing_the_last_job_deletes_jobs_json_and_loads_as_no_jobs(tmp_path, mocker):
    manager = JobManager(MagicMock())
    manager.jobs["j1"] = JobState(job_id="j1", tool_name="cpu_crack", capture_path="a.hc22000", hashlines_path="a.hc22000",
                                  status=ToolStatus.FAILURE, progress_msg="")
    manager._save()
    assert (tmp_path / "jobs.json").exists()

    manager.clear_job("j1")

    assert not (tmp_path / "jobs.json").exists()
    assert JobManager(MagicMock()).jobs == {}


def test_poll_copies_the_tools_progress_onto_the_job(manager):
    job_id, job = _running_job(manager)

    with patch.object(manager.tools["dummy"], "poll_status") as mock_poll:
        mock_poll.return_value = ToolResult(status=ToolStatus.RUNNING, value="Running (42.00%)",
                                            percent=42.0, detail="rockyou.txt (2/3)")
        manager.poll_jobs()

    assert (job.percent, job.detail, job.key) == (42.0, "rockyou.txt (2/3)", None)


def test_jobs_json_keeps_the_progress_fields(tmp_path, mocker):
    manager = JobManager(MagicMock())
    manager.jobs["j1"] = JobState(job_id="j1", tool_name="cpu_crack", capture_path="a.hc22000", hashlines_path="a.hc22000",
                                  status=ToolStatus.SUCCESS, progress_msg="Cracked! Key: k",
                                  percent=100.0, key="k")
    manager._save()

    loaded = JobManager(MagicMock()).jobs["j1"]

    assert (loaded.percent, loaded.detail, loaded.key) == (100.0, "", "k")


def test_submit_job_marks_an_auto_crack_job_and_jobs_json_keeps_it(tmp_path, mocker):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="a.hc22000", bssid="00:11:22:33:44:55")
    manager = JobManager(MagicMock())
    auto_crack_id = manager.submit_job("cpu_crack", cap, {"wordlists": ["w.txt"]}, [], auto_crack=True)
    manual_id = manager.submit_job("hashcat", cap, {}, [])

    loaded = JobManager(MagicMock()).jobs

    assert loaded[auto_crack_id].auto_crack is True
    assert loaded[manual_id].auto_crack is False


def test_submit_job_writes_the_hashlines_in_the_jobs_dir_and_clear_removes_the_jobs_files(tmp_path):
    from wifit3.models.access_point import CaptureType
    capture = tmp_path / "a.hc22000"
    capture.write_text("WPA*02*a\n", encoding="utf-8")
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path=str(capture), bssid="00:11:22:33:44:55")
    manager = JobManager(MagicMock())
    job_id = manager.submit_job("cpu_crack", cap, {}, ["WPA*02*a", "WPA*01*b"])
    job = JobManager(MagicMock()).jobs[job_id]
    jobs_dir = tmp_path / "jobs"
    assert pathlib.Path(job.hashlines_path) == jobs_dir / f"{job_id}.hc22000"
    assert pathlib.Path(job.hashlines_path).read_text(encoding="utf-8") == "WPA*02*a\nWPA*01*b\n"
    assert job.capture_path == str(capture)
    (jobs_dir / f"{job_id}.potfile").write_text("x", encoding="utf-8")
    (jobs_dir / f"{job_id}.log").write_text("x", encoding="utf-8")
    (jobs_dir / f"{job_id}_2.hc22000").write_text("another job's", encoding="utf-8")

    manager.clear_job(job_id)

    assert sorted(p.name for p in jobs_dir.iterdir()) == [f"{job_id}_2.hc22000"]
    assert capture.exists()


def test_clearing_a_job_from_an_older_jobs_json_never_deletes_its_capture(tmp_path):
    """Such a job cracks the capture itself: its hashlines_path is the capture_path."""
    capture = tmp_path / "a.hc22000"
    capture.write_text("WPA*02*a\n", encoding="utf-8")
    (tmp_path / "a.potfile").write_text("x", encoding="utf-8")
    (tmp_path / "jobs.json").write_text(json.dumps({"j1": {
        "job_id": "j1", "tool_name": "hashcat", "capture_path": str(capture),
        "status": "FAILURE", "progress_msg": "",
    }}), encoding="utf-8")
    manager = JobManager(MagicMock())
    assert manager.jobs["j1"].hashlines_path == str(capture)

    manager.clear_job("j1")

    assert capture.exists() and (tmp_path / "a.potfile").exists()


def test_jobs_submitted_back_to_back_each_get_their_own_id_and_hashlines_file(manager):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="a.hc22000", bssid="00:11:22:33:44:55")
    lines = ["WPA*02*a", "WPA*02*b", "WPA*02*c"]

    job_ids = [manager.submit_job("dummy", cap, {}, [line]) for line in lines]

    assert len(set(job_ids)) == len(lines)
    assert [pathlib.Path(manager.jobs[job_id].hashlines_path).read_text(encoding="utf-8")
            for job_id in job_ids] == [f"{line}\n" for line in lines]


def test_clearing_a_job_whose_file_cannot_be_removed_still_drops_the_job(tmp_path):
    from wifit3.models.access_point import CaptureType
    cap = PersistedCapture(type=CaptureType.HS, timestamp=0, path="a.hc22000", bssid="00:11:22:33:44:55")
    manager = JobManager(MagicMock())
    job_id = manager.submit_job("cpu_crack", cap, {}, ["WPA*02*a"])
    (tmp_path / "jobs" / f"{job_id}.log").mkdir()       # unlink raises OSError on a directory

    manager.clear_job(job_id)

    assert job_id not in JobManager(MagicMock()).jobs
    assert not (tmp_path / "jobs" / f"{job_id}.hc22000").exists()


def test_a_job_saved_before_the_progress_fields_existed_still_loads(tmp_path, mocker):
    mocker.patch("wifit3.vault.manager.JobManager._save")
    (tmp_path / "jobs.json").write_text(json.dumps({"j1": {
        "job_id": "j1", "tool_name": "hashcat", "capture_path": "a.hc22000",
        "status": "SUCCESS", "progress_msg": "Cracked! Key: k", "display_name": "hashcat (A)",
        "pid": None, "log_path": None, "api_id": None, "config": {}, "tool_state": {},
    }}), encoding="utf-8")

    job = JobManager(MagicMock()).jobs["j1"]

    assert (job.status, job.percent, job.detail, job.key) == (ToolStatus.SUCCESS, None, "", None)
    assert job.auto_crack is False


def test_a_job_saved_with_the_older_automatic_key_loads_as_auto_crack(tmp_path, mocker):
    mocker.patch("wifit3.vault.manager.JobManager._save")
    (tmp_path / "jobs.json").write_text(json.dumps({"j1": {
        "job_id": "j1", "tool_name": "cpu_crack", "capture_path": "a.hc22000",
        "status": "SUCCESS", "progress_msg": "Cracked! Key: k", "automatic": True,
    }}), encoding="utf-8")

    assert JobManager(MagicMock()).jobs["j1"].auto_crack is True


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
    hashline = f"WPA*01*{'aa' * 16}*aabbccddeeff*001122334455*41535553***"
    capture.write_text(f"{hashline}\n")
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
    }, [hashline])
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
