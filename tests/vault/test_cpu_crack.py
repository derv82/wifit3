import threading
from pathlib import Path

import pytest

from wifit3.crack import wpa_psk
from wifit3.crack.passwords import SSID_PASSWORDS_IDENTITY, ssid_variants
from wifit3.dot11 import wpa
from wifit3.models import PersistedCapture, ToolResult, ToolStatus
from wifit3.models.access_point import CaptureType
from wifit3.models.jobs import JobAction, JobState
from wifit3.vault.exhaustion import capture_record_ids, exhausted_wordlists, wordlist_identity
from wifit3.vault.tools.cpu_crack import CpuCrackTool

# hashcat wiki example_hashes, mode 22000 (password "hashcat!", essid "hashcat-essid").
_HASHCAT_PMKID_LINE = (
    "WPA*01*4d4fe7aac3a2cecab195321ceb99a7d0*fc690c158264*f4747f87f9f4"
    "*686173686361742d6573736964***")
_UNCRACKABLE_PMKID_LINE = (
    "WPA*01*00000000000000000000000000000000*fc690c158264*f4747f87f9f4"
    "*686173686361742d6573736964***")
_JOIN_SECONDS = 10.0


@pytest.fixture
def tool() -> CpuCrackTool:
    return CpuCrackTool()


def _write(path: Path, lines: list[str]) -> str:
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return str(path)


def _launch(tool: CpuCrackTool, tmp_path: Path, hashlines: list[str],
            wordlists: list[list[str]], job_id: str = "job",
            extra_passwords: list[str] | None = None,
            rerun_exhausted: bool = False) -> JobState:
    capture_path = _write(tmp_path / f"{job_id}.hc22000", hashlines)
    paths = [_write(tmp_path / f"{job_id}_words{i}.txt", words) for i, words in enumerate(wordlists)]
    config: dict = {"wordlists": paths, "rerun_exhausted": rerun_exhausted}
    if extra_passwords is not None:
        config["extra_passwords"] = extra_passwords
    job = JobState(job_id=job_id, tool_name="cpu_crack", capture_path=capture_path,
                   status=ToolStatus.RUNNING, progress_msg="", config=config)
    tool.launch(job, PersistedCapture(type=CaptureType.PMKID, timestamp=0, path=capture_path,
                                      bssid="fc:69:0c:15:82:64"))
    return job


def _join(tool: CpuCrackTool, job: JobState) -> None:
    thread = tool._runs[job.job_id].thread
    thread.join(timeout=_JOIN_SECONDS)
    assert not thread.is_alive()


def _poll_wordlist(tool: CpuCrackTool, job: JobState) -> ToolResult:
    _join(tool, job)
    return tool.poll_status(job)


def _poll_finished(tool: CpuCrackTool, job: JobState) -> ToolResult:
    result = _poll_wordlist(tool, job)
    while result.status is ToolStatus.RUNNING:
        result = _poll_wordlist(tool, job)
    return result


def _gate_pmk(mocker) -> tuple[threading.Event, threading.Event]:
    """Patch ``wpa.derive_pmk`` so the worker blocks inside its first PMK until released."""
    entered, release = threading.Event(), threading.Event()
    real_pmk = wpa.derive_pmk

    def gated(psk: str, ssid: bytes) -> bytes:
        entered.set()
        release.wait(_JOIN_SECONDS)
        return real_pmk(psk, ssid)

    mocker.patch("wifit3.dot11.wpa.derive_pmk", side_effect=gated)
    return entered, release


def test_cpu_crack_can_crack_only_hc22000(tool):
    assert tool.can_crack(PersistedCapture(type=CaptureType.PMKID, timestamp=0, path="a.hc22000",
                                           bssid="fc:69:0c:15:82:64"))
    assert not tool.can_crack(PersistedCapture(type=CaptureType.HS, timestamp=0, path="a.pcap",
                                               bssid="fc:69:0c:15:82:64"))


def test_cpu_crack_finds_the_hashcat_example_key(tool, tmp_path):
    job = _launch(tool, tmp_path, [_HASHCAT_PMKID_LINE], [["wrongpass1", "hashcat!"]])

    result = _poll_finished(tool, job)

    assert result.status == ToolStatus.SUCCESS
    assert result.value == "Cracked! Key: hashcat!"
    assert (result.key, result.percent) == ("hashcat!", 100.0)


def test_cpu_crack_reports_not_found_once_the_wordlist_is_exhausted(tool, tmp_path):
    job = _launch(tool, tmp_path, [_HASHCAT_PMKID_LINE], [["wrongpass1", "wrongpass2"]])

    result = _poll_finished(tool, job)

    assert result.status == ToolStatus.FAILURE
    assert result.value == "job_words0.txt"
    assert result.percent == 100.0


def test_natural_exhaustion_is_recorded_and_filtered_on_the_next_launch(tool, tmp_path):
    job = _launch(tool, tmp_path, [_UNCRACKABLE_PMKID_LINE], [["wrongpass1"]])
    wordlist = job.tool_state["wordlists"][1]
    assert _poll_finished(tool, job).status == ToolStatus.FAILURE

    identifiers = capture_record_ids(job.capture_path)
    assert exhausted_wordlists("fc:69:0c:15:82:64", identifiers) == {
        SSID_PASSWORDS_IDENTITY, wordlist_identity(wordlist),
    }
    with pytest.raises(ValueError, match="already exhausted"):
        tool.launch(job, PersistedCapture(type=CaptureType.PMKID, timestamp=0,
                                          path=job.capture_path, bssid="fc:69:0c:15:82:64"))


def test_rerun_exhausted_runs_without_writing_history(tool, tmp_path):
    job = _launch(tool, tmp_path, [_UNCRACKABLE_PMKID_LINE], [["wrongpass1"]],
                  rerun_exhausted=True)

    assert _poll_finished(tool, job).status == ToolStatus.FAILURE
    assert exhausted_wordlists("fc:69:0c:15:82:64",
                               capture_record_ids(job.capture_path)) == set()


def test_success_before_wordlist_end_does_not_record_exhaustion(tool, tmp_path):
    job = _launch(tool, tmp_path, [_HASHCAT_PMKID_LINE], [["hashcat!", "wrongpass1"]])

    assert _poll_finished(tool, job).status == ToolStatus.SUCCESS
    assert exhausted_wordlists("fc:69:0c:15:82:64",
                               capture_record_ids(job.capture_path)) == {
                                   SSID_PASSWORDS_IDENTITY,
                               }


def test_cpu_crack_skips_an_exhausted_ssid_password_version(tool, tmp_path, mocker):
    crack = mocker.patch.object(tool, "_psk_cracks", return_value=False)
    first = _launch(tool, tmp_path, [_UNCRACKABLE_PMKID_LINE], [["wrongpass1"]], job_id="first")
    assert _poll_finished(tool, first).status == ToolStatus.FAILURE
    crack.reset_mock()

    second = _launch(tool, tmp_path, [_UNCRACKABLE_PMKID_LINE], [["wrongpass2"]], job_id="second")
    assert _poll_finished(tool, second).status == ToolStatus.FAILURE

    assert [call.args[0] for call in crack.call_args_list] == ["wrongpass2"]


def test_cpu_crack_moves_on_to_the_next_wordlist(tool, tmp_path):
    job = _launch(tool, tmp_path, [_HASHCAT_PMKID_LINE], [["wrongpass1"], ["hashcat!"]])

    first_wordlist = _poll_wordlist(tool, job)
    assert first_wordlist.status == ToolStatus.RUNNING
    assert first_wordlist.value.endswith(" · job_words0.txt (1/2)")

    moved_on = _poll_wordlist(tool, job)
    assert moved_on.status == ToolStatus.RUNNING
    assert moved_on.value.endswith(" · job_words1.txt (2/2)")
    assert (moved_on.percent, moved_on.detail) == (None, "job_words1.txt (2/2)")

    assert _poll_finished(tool, job).status == ToolStatus.SUCCESS


def test_cpu_crack_tries_ssid_variants_before_the_wordlist(tool, tmp_path, mocker):
    ssid, psk = "MyHomeNet", "MyHomeNet123"
    mac_ap, mac_sta = bytes.fromhex("fc690c158264"), bytes.fromhex("f4747f87f9f4")
    pmkid = wpa_psk.pmkid(wpa.derive_pmk(psk, ssid.encode()), mac_ap, mac_sta)
    line = f"WPA*01*{pmkid.hex()}*{mac_ap.hex()}*{mac_sta.hex()}*{ssid.encode().hex()}***"
    pmk_spy = mocker.patch("wifit3.dot11.wpa.derive_pmk", wraps=wpa.derive_pmk)

    job = _launch(tool, tmp_path, [line], [["wrongpass1"]])
    result = _poll_finished(tool, job)

    assert result.value == f"Cracked! Key: {psk}"
    assert pmk_spy.call_args_list[0].args == (ssid, ssid.encode())
    assert "wrongpass1" not in [call.args[0] for call in pmk_spy.call_args_list]


def test_cpu_crack_tries_probed_ssid_variants_before_the_wordlist(tool, tmp_path, mocker):
    probed_ssid, psk = "CafeGuest", "CafeGuest123"
    mac_ap, mac_sta = bytes.fromhex("fc690c158264"), bytes.fromhex("f4747f87f9f4")
    essid = b"hashcat-essid"
    pmkid = wpa_psk.pmkid(wpa.derive_pmk(psk, essid), mac_ap, mac_sta)
    line = f"WPA*01*{pmkid.hex()}*{mac_ap.hex()}*{mac_sta.hex()}*{essid.hex()}***"
    pmk_spy = mocker.patch("wifit3.dot11.wpa.derive_pmk", wraps=wpa.derive_pmk)

    job = _launch(tool, tmp_path, [line], [["wrongpass1"]], extra_passwords=[probed_ssid])
    result = _poll_finished(tool, job)

    assert result.value == f"Cracked! Key: {psk}"
    tried = [call.args[0] for call in pmk_spy.call_args_list]
    expected = ssid_variants("hashcat-essid") + ssid_variants(probed_ssid)
    assert tried == expected[:expected.index(psk) + 1]


def test_cpu_crack_counts_and_dedupes_extra_passwords_in_the_total(tool, tmp_path, mocker):
    mocker.patch.object(tool, "_prepend_generated_wordlist")
    entered, release = _gate_pmk(mocker)
    job = _launch(tool, tmp_path, [_UNCRACKABLE_PMKID_LINE], [["CafeGuest", "wrongpass1"]],
                  extra_passwords=["CafeGuest", "hashcat-essid"])
    assert entered.wait(_JOIN_SECONDS)

    run = tool._runs[job.job_id]
    assert run.passwords_total == len(list(tool._passwords(run)))
    assert list(tool._passwords(run)).count("CafeGuest") == 1
    assert list(tool._passwords(run))[-1] == "wrongpass1"
    release.set()
    _join(tool, job)


def test_cpu_crack_derives_one_pmk_per_password_and_essid(tool, tmp_path, mocker):
    pmk_spy = mocker.patch("wifit3.dot11.wpa.derive_pmk", wraps=wpa.derive_pmk)

    first = _launch(tool, tmp_path, [_HASHCAT_PMKID_LINE, _UNCRACKABLE_PMKID_LINE],
                    [["wrongpass1"]], job_id="first")
    assert _poll_finished(tool, first).status == ToolStatus.FAILURE
    assert pmk_spy.call_count == len({call.args for call in pmk_spy.call_args_list})


def test_cpu_crack_stop_ends_the_job_as_stopped(tool, tmp_path, mocker):
    entered, release = _gate_pmk(mocker)
    job = _launch(tool, tmp_path, [_HASHCAT_PMKID_LINE], [["wrongpass1", "hashcat!"]])
    assert entered.wait(_JOIN_SECONDS)

    assert tool.apply(JobAction.STOP, job) is True
    assert tool.poll_status(job).actions == JobAction.STOP
    release.set()

    result = _poll_finished(tool, job)
    assert result.status == ToolStatus.STOPPED


def test_cpu_crack_pause_holds_the_worker_until_resumed(tool, tmp_path, mocker):
    entered, release = _gate_pmk(mocker)
    job = _launch(tool, tmp_path, [_HASHCAT_PMKID_LINE], [["wrongpass1", "hashcat!"]])
    assert entered.wait(_JOIN_SECONDS)
    running = tool.poll_status(job)
    assert running.actions == JobAction.STOP | JobAction.SKIP | JobAction.PAUSE
    assert (running.percent, running.detail) == (0.0, "SSID passwords (0/1)")

    assert tool.apply(JobAction.PAUSE, job) is True
    release.set()
    paused = tool.poll_status(job)
    assert paused.value.startswith("Paused (")
    assert paused.percent is not None
    assert paused.actions == JobAction.STOP | JobAction.SKIP | JobAction.RESUME
    thread = tool._runs[job.job_id].thread
    thread.join(timeout=0.05)
    assert thread.is_alive()

    assert tool.apply(JobAction.RESUME, job) is True
    assert _poll_finished(tool, job).status == ToolStatus.SUCCESS


def test_cpu_crack_refuses_pause_once_the_worker_has_ended(tool, tmp_path):
    job = _launch(tool, tmp_path, [_HASHCAT_PMKID_LINE], [["wrongpass1"]])
    _join(tool, job)

    assert tool.apply(JobAction.PAUSE, job) is False


def test_cpu_crack_launch_rejects_a_capture_without_hashlines(tool, tmp_path):
    with pytest.raises(ValueError):
        _launch(tool, tmp_path, ["not a hashline"], [["wrongpass1"]])
