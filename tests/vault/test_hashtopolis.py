import base64
import io
import json
import time
import urllib.error
from unittest.mock import MagicMock

import pytest

from wifit3.models import CaptureType, JobAction, JobState, PersistedCapture, ToolStatus
from wifit3.persist.config import Config
from wifit3.vault.manager import JobManager
from wifit3.vault.tools import hashtopolis as ht
from wifit3.vault.tools import hashtopolis_api as api_module
from wifit3.vault.tools.hashtopolis_api import (HashtopolisApi, HashtopolisError,
                                                HashtopolisUnreachable, ServerFile, TaskProgress,
                                                user_api_endpoint)

_ESSID = "test".encode().hex()
_PMKID = f"WPA*01*{'11' * 16}*020000000a01*020000000b02*{_ESSID}***"
_EAPOL = f"WPA*02*{'22' * 16}*020000000a01*020000000b02*{_ESSID}*{'33' * 32}*{'44' * 60}*00"
_FT_PMKID = f"WPA*03*{'55' * 16}*020000000a01*020000000b02*{_ESSID}****a1b2*{'66' * 6}*{'77' * 6}"


class FakeApi:
    """Scripted server: ``progress`` and ``cracked`` are what the next poll sees."""

    def __init__(self, url: str = "", access_key: str = "") -> None:
        self.uploaded: list[str] = []
        self.created_tasks = 0
        self.archived: list[int] = []
        self.progress = TaskProgress(keyspace=0, searched=0, is_complete=False)
        self.cracked_keys_found: list[str] = []
        self.poll_error: Exception | None = None
        self.setup_error: Exception | None = None

    def newest_hashcat_version_id(self) -> int:
        if self.setup_error:
            raise self.setup_error
        return 3

    def create_hashlist(self, name, hashlines, access_group_id) -> int:
        self.uploaded = hashlines
        return 40

    def create_task(self, name, hashlist_id, wordlist, cracker_version_id) -> int:
        self.created_tasks += 1
        return 50

    def cracked_keys(self, hashlist_id) -> list[str]:
        if self.poll_error:
            raise self.poll_error
        return self.cracked_keys_found

    def task_progress(self, task_id) -> TaskProgress:
        return self.progress

    def archive_task(self, task_id) -> None:
        self.archived.append(task_id)


@pytest.fixture
def server(monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(ht, "HashtopolisApi", lambda url, access_key: fake)
    monkeypatch.setattr(ht, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(Config.hashtopolis, "url", "http://server:8080")
    monkeypatch.setattr(Config.hashtopolis, "access_key", "key")
    monkeypatch.setattr(Config.hashtopolis, "wordlist_id", None)
    monkeypatch.setattr(Config.hashtopolis, "wordlist_name", None)
    monkeypatch.setattr(Config.hashtopolis, "access_group_id", None)
    return fake


def _capture(tmp_path, *lines: str) -> PersistedCapture:
    path = tmp_path / "test.hc22000"
    path.write_text("\n".join(lines) + "\n")
    return PersistedCapture(type=CaptureType.HS, timestamp=0, path=str(path),
                            bssid="aa:bb:cc:dd:ee:ff", ssid="test")


def _job(capture: PersistedCapture) -> JobState:
    return JobState(job_id="job", tool_name="hashtopolis", capture_path=capture.path,
                    status=ToolStatus.RUNNING, progress_msg="",
                    config={"wordlist_id": 1, "wordlist_name": "rockyou.txt",
                            "access_group_id": 1})


def _poll_until(tool, job, predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = tool.poll_status(job)
        if predicate(result):
            return result
        time.sleep(0.01)
    raise AssertionError(f"last result: {result}")


def test_cracked_hash_succeeds_with_its_key(server, tmp_path):
    tool, capture = ht.HashtopolisTool(), _capture(tmp_path, _PMKID)
    job = _job(capture)
    tool.launch(job, capture)
    server.cracked_keys_found = ["secret-psk"]

    result = _poll_until(tool, job, lambda r: r.status != ToolStatus.RUNNING)

    assert (result.status, result.key) == (ToolStatus.SUCCESS, "secret-psk")
    assert (job.api_id, job.tool_state["hashlist_id"]) == ("50", 40)


def test_exhausted_task_fails_naming_the_wordlist(server, tmp_path):
    tool, capture = ht.HashtopolisTool(), _capture(tmp_path, _PMKID)
    job = _job(capture)
    tool.launch(job, capture)
    server.progress = TaskProgress(keyspace=5, searched=5, is_complete=True)

    result = _poll_until(tool, job, lambda r: r.status != ToolStatus.RUNNING)

    assert (result.status, result.value) == (ToolStatus.FAILURE, "rockyou.txt")


def test_task_no_agent_took_waits_then_reports_progress(server, tmp_path):
    tool, capture = ht.HashtopolisTool(), _capture(tmp_path, _PMKID)
    job = _job(capture)
    tool.launch(job, capture)

    assert _poll_until(tool, job, lambda r: r.value == "Waiting for an agent").status \
        == ToolStatus.RUNNING
    server.progress = TaskProgress(keyspace=200, searched=50, is_complete=False)
    result = _poll_until(tool, job, lambda r: r.percent is not None)

    assert (result.value, result.percent, result.actions) == (
        "Running (25.00%)", 25.0, JobAction.STOP)


def test_server_error_message_ends_the_job(server, tmp_path):
    tool, capture = ht.HashtopolisTool(), _capture(tmp_path, _PMKID)
    job = _job(capture)
    server.setup_error = HashtopolisError("Expired access key!")
    tool.launch(job, capture)

    result = _poll_until(tool, job, lambda r: r.status != ToolStatus.RUNNING)

    assert (result.status, result.value) == (ToolStatus.ERROR, "Expired access key!")


def test_unreachable_server_while_polling_keeps_the_job_running(server, tmp_path):
    tool, capture = ht.HashtopolisTool(), _capture(tmp_path, _PMKID)
    job = _job(capture)
    server.poll_error = HashtopolisUnreachable("Cannot reach http://server:8080/api/user.php")
    tool.launch(job, capture)

    result = _poll_until(tool, job, lambda r: r.value.startswith("Cannot reach"))
    assert result.status == ToolStatus.RUNNING

    server.poll_error = None
    server.cracked_keys_found = ["secret-psk"]
    assert _poll_until(tool, job, lambda r: r.status != ToolStatus.RUNNING).key == "secret-psk"


def test_stop_archives_the_task(server, tmp_path):
    tool, capture = ht.HashtopolisTool(), _capture(tmp_path, _PMKID)
    job = _job(capture)
    tool.launch(job, capture)
    _poll_until(tool, job, lambda r: r.value == "Waiting for an agent")

    assert tool.apply(JobAction.STOP, job)
    result = _poll_until(tool, job, lambda r: r.status != ToolStatus.RUNNING)

    assert result.status == ToolStatus.STOPPED
    assert server.archived == [50]


def test_relaunched_app_polls_the_existing_task(server, tmp_path):
    capture = _capture(tmp_path, _PMKID)
    job = _job(capture)
    job.api_id = "50"
    job.tool_state.update(hashlist_id=40, server="http://server:8080/api/user.php")
    server.cracked_keys_found = ["secret-psk"]

    result = _poll_until(ht.HashtopolisTool(), job, lambda r: r.status != ToolStatus.RUNNING)

    assert result.key == "secret-psk"
    assert server.created_tasks == 0


def test_job_on_another_server_is_not_adopted(server, tmp_path, monkeypatch):
    capture = _capture(tmp_path, _PMKID)
    job = _job(capture)
    job.api_id = "50"
    job.tool_state.update(hashlist_id=40, server=user_api_endpoint("http://old-server:8080"))

    result = ht.HashtopolisTool().poll_status(job)

    assert (result.status, result.value) == (
        ToolStatus.ERROR, "Hashtopolis URL changed since this job started on "
                          "http://old-server:8080/api/user.php")


def test_launch_records_the_server_but_not_the_access_key(server, tmp_path, monkeypatch):
    monkeypatch.setattr(Config.hashtopolis, "access_key", "SECRET-ACCESS-KEY")
    tool, capture = ht.HashtopolisTool(), _capture(tmp_path, _PMKID)
    job = _job(capture)
    tool.launch(job, capture)
    _poll_until(tool, job, lambda r: r.value == "Waiting for an agent")

    assert job.tool_state["server"] == "http://server:8080/api/user.php"
    assert "SECRET-ACCESS-KEY" not in json.dumps(job.to_dict())


def test_job_without_a_task_cannot_be_adopted(server, tmp_path):
    job = _job(_capture(tmp_path, _PMKID))

    assert ht.HashtopolisTool().poll_status(job).status == ToolStatus.ERROR


def test_manager_adopts_a_running_hashtopolis_job(server, tmp_path, mocker):
    mocker.patch("wifit3.vault.manager.JobManager._save")
    mocker.patch("wifit3.vault.manager.JobManager._load")
    manager = JobManager(MagicMock())
    job = _job(_capture(tmp_path, _PMKID))
    job.api_id = "50"
    job.tool_state.update(hashlist_id=40, server="http://server:8080/api/user.php")
    manager.jobs[job.job_id] = job

    manager.reconcile_on_startup()

    assert job.status == ToolStatus.RUNNING


def test_only_wpa01_and_wpa02_lines_are_uploaded(server, tmp_path):
    tool, capture = ht.HashtopolisTool(), _capture(tmp_path, _PMKID, _FT_PMKID, _EAPOL, _PMKID)
    job = _job(capture)
    tool.launch(job, capture)
    _poll_until(tool, job, lambda r: r.value == "Waiting for an agent")

    assert server.uploaded == [_PMKID, _EAPOL]


def test_ft_only_capture_is_not_crackable(server, tmp_path):
    assert not ht.HashtopolisTool().can_crack(_capture(tmp_path, _FT_PMKID))
    assert ht.HashtopolisTool().can_crack(_capture(tmp_path, _EAPOL))


def test_auto_crack_config_needs_server_wordlist_and_group(server, monkeypatch):
    assert ht.HashtopolisTool().auto_crack_config() is None
    monkeypatch.setattr(Config.hashtopolis, "wordlist_id", 1)
    monkeypatch.setattr(Config.hashtopolis, "wordlist_name", "rockyou.txt")
    monkeypatch.setattr(Config.hashtopolis, "access_group_id", 2)

    assert ht.HashtopolisTool().auto_crack_config() == {
        "wordlist_id": 1, "wordlist_name": "rockyou.txt", "access_group_id": 2}


# ----- User API client ------------------------------------------------------

def _answer(monkeypatch, *responses):
    """Each urlopen call returns the next response; returns the request bodies sent."""
    sent = []
    queue = list(responses)

    def urlopen(request, timeout):
        sent.append(json.loads(request.data))
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(response if isinstance(response, bytes) else json.dumps(response).encode())

    monkeypatch.setattr(api_module.urllib.request, "urlopen", urlopen)
    return sent


def test_server_error_raises_its_message(monkeypatch):
    _answer(monkeypatch, {"response": "ERROR", "message": "Expired access key!"})

    with pytest.raises(HashtopolisError, match="Expired access key!"):
        HashtopolisApi("http://server:8080", "key").call("task", "getTask", taskId=1)


def test_connection_failure_and_non_json_are_unreachable(monkeypatch):
    _answer(monkeypatch, urllib.error.URLError("refused"), b"<html>legacy UI</html>")
    api = HashtopolisApi("http://server:8080", "key")

    with pytest.raises(HashtopolisUnreachable, match="refused"):
        api.check_access()
    with pytest.raises(HashtopolisUnreachable, match="not a Hashtopolis User API"):
        api.check_access()


def test_create_hashlist_sends_base64_hashlines_and_every_required_key(monkeypatch):
    sent = _answer(monkeypatch, {"response": "OK", "hashlistId": 7})

    assert HashtopolisApi("server:8080", "key").create_hashlist("n", [_PMKID], 1) == 7
    assert sent[0]["data"] == base64.b64encode(f"{_PMKID}\n".encode()).decode()
    assert {k: sent[0][k] for k in ("hashtypeId", "brainFeatures", "format", "accessKey")} == {
        "hashtypeId": 22000, "brainFeatures": 0, "format": 0, "accessKey": "key"}


def test_create_task_runs_the_wordlist_on_the_hashlist(monkeypatch):
    sent = _answer(monkeypatch, {"response": "OK", "taskId": 9})

    assert HashtopolisApi("server", "key").create_task(
        "n", 7, ServerFile(2, "rockyou.txt"), 3) == 9
    assert (sent[0]["attackCmd"], sent[0]["files"], sent[0]["crackerVersionId"]) == (
        "#HL# rockyou.txt", [2], 3)
    assert sent[0]["priority"] >= 1


def test_wordlists_are_files_of_type_zero(monkeypatch):
    _answer(monkeypatch, {"response": "OK", "files": [
        {"fileId": 1, "fileType": 0, "filename": "rockyou.txt"},
        {"fileId": 2, "fileType": 1, "filename": "best64.rule"}]})

    assert HashtopolisApi("server", "key").wordlists() == [ServerFile(1, "rockyou.txt")]


def test_newest_hashcat_version_is_the_highest_version_id(monkeypatch):
    _answer(monkeypatch,
            {"response": "OK", "crackers": [{"crackerTypeId": 1, "crackerTypeName": "hashcat"}]},
            {"response": "OK", "crackerVersions": [{"versionId": 1}, {"versionId": 4}]})

    assert HashtopolisApi("server", "key").newest_hashcat_version_id() == 4


@pytest.mark.parametrize("url", ["server:8080", "http://server:8080/",
                                 "http://server:8080/api/user.php"])
def test_user_api_endpoint(url):
    assert user_api_endpoint(url) == "http://server:8080/api/user.php"


@pytest.mark.parametrize("error", [ValueError("nonnumeric port: '8080x'"),
                                   OverflowError("port must be 0-65535.")])
def test_invalid_url_is_a_hashtopolis_error(monkeypatch, error):
    _answer(monkeypatch, error)

    with pytest.raises(HashtopolisError, match="Invalid server URL"):
        HashtopolisApi("http://server:8080x", "key").check_access()


@pytest.mark.parametrize("reply", [
    {"response": "OK", "files": [{"fileType": 0, "filename": "rockyou.txt"}]},
    {"response": "OK", "files": 5},
    {"response": "OK", "files": [{"fileId": "x", "fileType": 0, "filename": "a"}]},
])
def test_malformed_reply_is_a_hashtopolis_error(monkeypatch, reply):
    _answer(monkeypatch, reply)

    with pytest.raises(HashtopolisError, match="Unexpected reply to file/listFiles"):
        HashtopolisApi("server", "key").wordlists()


def test_cracked_keys_are_the_plains(monkeypatch):
    _answer(monkeypatch, {"response": "OK", "cracked": [
        {"hash": _PMKID, "plain": "secret-psk", "crackpos": 1}]})

    assert HashtopolisApi("server", "key").cracked_keys(40) == ["secret-psk"]
