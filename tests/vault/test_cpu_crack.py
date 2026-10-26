import threading
from pathlib import Path

import pytest

from wifit3.crack import wpa_psk
from wifit3.crack.hc22000_format import pmkid_hashline
from wifit3.crack.passwords import ssid_passwords, ssid_variants
from wifit3.dot11 import wpa
from wifit3.dot11.eapol import eapol_key
from wifit3.dot11.wpa import KCK_LEN, derive_ptk_sha256, eapol_mic_aes_cmac
from wifit3.models import Handshake, PersistedCapture, ToolResult, ToolStatus
from wifit3.models.access_point import CaptureType
from wifit3.models.jobs import JobAction, JobState
from wifit3.persist.config import Config
from wifit3.vault.exhaustion import SSID_VARIANTS_IDENTITY, capture_exhaustion, wordlist_identity
from wifit3.vault.tools.cpu_crack import CpuCrackTool

# hashcat wiki example_hashes, mode 22000 (password "hashcat!", essid "hashcat-essid").
_HASHCAT_PMKID_LINE = (
    "WPA*01*4d4fe7aac3a2cecab195321ceb99a7d0*fc690c158264*f4747f87f9f4"
    "*686173686361742d6573736964***")
_UNCRACKABLE_PMKID_LINE = (
    "WPA*01*00000000000000000000000000000000*fc690c158264*f4747f87f9f4"
    "*686173686361742d6573736964***")
# ESSID "ab": too short for any numbered SSID variant, so it has only a few dozen.
_UNCRACKABLE_SHORT_ESSID_LINE = (
    "WPA*01*00000000000000000000000000000000*fc690c158264*f4747f87f9f4*6162***")
_JOIN_SECONDS = 10.0


@pytest.fixture
def tool() -> CpuCrackTool:
    return CpuCrackTool()


def _write(path: Path, lines: list[str]) -> str:
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return str(path)


def _launch(tool: CpuCrackTool, tmp_path: Path, hashlines: list[str],
            wordlists: list[list[str]], job_id: str = "job",
            extra_passwords: list[str] | None = None, ap_ssid_passwords: bool = False) -> JobState:
    capture_path = _write(tmp_path / f"{job_id}.hc22000", hashlines)
    paths = [_write(tmp_path / f"{job_id}_words{i}.txt", words) for i, words in enumerate(wordlists)]
    config: dict = {"wordlists": paths, "ap_ssid_passwords": ap_ssid_passwords}
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


def _poll_finished(tool: CpuCrackTool, job: JobState) -> ToolResult:
    _join(tool, job)
    return tool.poll_status(job)


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


_FT_COMMON = "*020000000001*020000000002*66742d70736b2d74657374"
_FT_IDENTIFIERS = "*a1b2*72306b682e6578616d706c652e74657374*020000000001"
_FT_SNONCE = "404142434445464748494a4b4c4d4e4f505152535455565758595a5b5c5d5e5f"
_FT_ANONCE_DRIFT = "101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e30"
_FT_EAPOL_PREFIX = "020300fb02010b00100000000000000001"
_FT_EAPOL_SUFFIX = (
    "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "009c30260100000fac040100000fac040100000fac0400000100a296df8e01721dbac1685bc39d5b78ad3603a1b201376d"
    "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000000000000000000000000106020000000001031172306b68"
    "2e6578616d706c652e74657374")
_FT_EAPOL_PAIR3_WITH_ANONCE_DRIFT = (
    f"WPA*04*8d98ed12e5f7c4525f5628d4619618df{_FT_COMMON}"
    f"*{_FT_SNONCE}*{_FT_EAPOL_PREFIX}{_FT_ANONCE_DRIFT}{_FT_EAPOL_SUFFIX}"
    f"*83{_FT_IDENTIFIERS}")


def test_cpu_crack_corrects_a_key_version_3_anonce(tool, tmp_path):
    ssid = b"kv3-net"
    pmk = wpa.derive_pmk("kv3-secret", ssid)
    mac_ap, mac_sta = bytes.fromhex("112233445566"), bytes.fromhex("aabbccddeeff")
    snonce = bytes.fromhex("5b" * 32)
    true = bytes.fromhex("a0" * 28) + (1000).to_bytes(4, "big")
    stored = bytes.fromhex("a0" * 28) + (1001).to_bytes(4, "big")
    frame = eapol_key(key_info=0x010B, key_len=0, replay=1, nonce=snonce)
    mic = eapol_mic_aes_cmac(derive_ptk_sha256(pmk, mac_ap, mac_sta, true, snonce, KCK_LEN), frame)
    line = (f"WPA*02*{mic.hex()}*{mac_ap.hex()}*{mac_sta.hex()}*{ssid.hex()}"
            f"*{stored.hex()}*{frame.hex()}*80")

    job = _launch(tool, tmp_path, [line], [["nope", "kv3-secret"]])

    assert _poll_finished(tool, job).key == "kv3-secret"


def test_cpu_crack_finds_a_stored_psk_sha256_pmkid(tool, tmp_path):
    ssid = "sha256-net"
    pmk = wpa.derive_pmk("sha256-secret", ssid.encode())
    hs = Handshake(bssid="11:22:33:44:55:66", client_mac="aa:bb:cc:dd:ee:ff",
                   pmkid=wpa_psk.sha256_pmkid(pmk, bytes.fromhex("112233445566"),
                                              bytes.fromhex("aabbccddeeff")))
    hs.pmkid_akm = 6
    line = pmkid_hashline(ssid, hs)
    assert line is not None and line.endswith("***03")

    job = _launch(tool, tmp_path, [line], [["nope", "sha256-secret"]])

    assert _poll_finished(tool, job).key == "sha256-secret"


def test_cpu_crack_corrects_an_ft_psk_message_three_anonce(tool, tmp_path):
    job = _launch(tool, tmp_path, [_FT_EAPOL_PAIR3_WITH_ANONCE_DRIFT],
                  [["correct horse battery!", "correct horse battery"]])

    result = _poll_finished(tool, job)

    assert (result.status, result.key) == (ToolStatus.SUCCESS, "correct horse battery")


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


def test_cpu_crack_moves_on_to_the_next_wordlist(tool, tmp_path):
    job = _launch(tool, tmp_path, [_HASHCAT_PMKID_LINE], [["wrongpass1"], ["hashcat!"]])

    moved_on = _poll_finished(tool, job)
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

    job = _launch(tool, tmp_path, [line], [["wrongpass1"]], ap_ssid_passwords=True)
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
    expected = ssid_passwords([probed_ssid])
    assert tried == expected[:expected.index(psk) + 1]


def test_cpu_crack_counts_and_dedupes_the_ssid_passwords_in_the_total(tool, tmp_path, mocker):
    entered, release = _gate_pmk(mocker)
    job = _launch(tool, tmp_path, [_UNCRACKABLE_SHORT_ESSID_LINE], [["CafeGuest", "wrongpass1"]],
                  extra_passwords=["CafeGuest", "CafeGuest"], ap_ssid_passwords=True)
    assert entered.wait(_JOIN_SECONDS)

    run = tool._runs[job.job_id]
    tried = list(tool._passwords(run))
    assert tried == list(dict.fromkeys(ssid_variants("ab") + ssid_passwords(["CafeGuest"])))
    assert run.passwords_total == len(tried)
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
    assert running.actions == JobAction.STOP | JobAction.PAUSE
    assert (running.percent, running.detail) == (0.0, "job_words0.txt")

    assert tool.apply(JobAction.PAUSE, job) is True
    release.set()
    paused = tool.poll_status(job)
    assert paused.value.startswith("Paused (")
    assert paused.percent is not None
    assert paused.actions == JobAction.STOP | JobAction.RESUME
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


def test_auto_crack_config_uses_the_saved_cpu_crack_wordlists():
    Config.cpu_crack.wordlists = ["/dicts/small.txt"]
    Config.hashcat.wordlists = ["/dicts/rockyou.txt"]
    Config.cpu_crack.default_passwords = False
    assert CpuCrackTool().auto_crack_config() == {
        "wordlists": ["/dicts/small.txt"], "default_passwords": False,
        "ap_ssid_passwords": True, "probed_ssid_passwords": True}


def test_auto_crack_config_is_none_without_a_wordlist_or_password_source():
    Config.cpu_crack.wordlists = []
    Config.cpu_crack.default_passwords = False
    Config.cpu_crack.ap_ssid_passwords = False
    Config.cpu_crack.probed_ssid_passwords = False
    assert CpuCrackTool().auto_crack_config() is None


def _launch_with(tool: CpuCrackTool, tmp_path: Path, hashlines: list[str], **config) -> JobState:
    capture_path = _write(tmp_path / "sources.hc22000", hashlines)
    job = JobState(job_id="sources", tool_name="cpu_crack", capture_path=capture_path,
                   status=ToolStatus.RUNNING, progress_msg="", config={"wordlists": [], **config})
    tool.launch(job, PersistedCapture(type=CaptureType.PMKID, timestamp=0, path=capture_path,
                                      bssid="fc:69:0c:15:82:64"))
    return job


def test_a_job_without_wordlist_files_cracks_from_the_ssid_passwords(tool, tmp_path):
    job = _launch_with(tool, tmp_path, [_HASHCAT_PMKID_LINE], extra_passwords=["hashcat!"],
                       ap_ssid_passwords=False)
    result = _poll_finished(tool, job)
    assert result.status == ToolStatus.SUCCESS and result.key == "hashcat!"


def test_a_job_without_wordlist_files_ends_not_found_on_the_ssid_passwords(tool, tmp_path):
    job = _launch_with(tool, tmp_path, [_UNCRACKABLE_SHORT_ESSID_LINE], ap_ssid_passwords=True)
    result = _poll_finished(tool, job)
    assert result.status == ToolStatus.FAILURE and result.value == "SSID passwords"


@pytest.mark.parametrize("ticked", [True, False])
def test_ap_ssid_passwords_box_adds_the_ap_ssid_variants(tool, tmp_path, ticked):
    wordlist = _write(tmp_path / "words.txt", ["unrelated"])
    job = _launch_with(tool, tmp_path, [_UNCRACKABLE_SHORT_ESSID_LINE], wordlists=[wordlist],
                       ap_ssid_passwords=ticked)
    _join(tool, job)
    variants = set(ssid_variants("ab"))
    assert bool(variants & set(tool._runs[job.job_id].ssid_passwords)) is ticked


@pytest.mark.parametrize("ticked", [True, False])
def test_default_passwords_box_runs_the_default_list_before_the_wordlists(tool, tmp_path, mocker,
                                                                         ticked):
    default = tmp_path / "default_passwords.txt"
    default.write_text("hashcat!\n", encoding="utf-8")
    mocker.patch("wifit3.vault.tools.cpu_crack.DEFAULT_PASSWORDS_PATH", default)
    wordlist = _write(tmp_path / "words.txt", ["unrelated"])
    job = _launch_with(tool, tmp_path, [_HASHCAT_PMKID_LINE], wordlists=[wordlist],
                       default_passwords=ticked, ap_ssid_passwords=False)
    result = _poll_finished(tool, job)
    assert (result.status == ToolStatus.SUCCESS) is ticked


def test_a_job_with_no_password_source_refuses_to_launch(tool, tmp_path):
    with pytest.raises(ValueError, match="No passwords to try"):
        _launch_with(tool, tmp_path, [_HASHCAT_PMKID_LINE], default_passwords=False,
                     ap_ssid_passwords=False)


def _capture_exhaustion(job: JobState) -> set[str]:
    return set(capture_exhaustion(PersistedCapture(type=CaptureType.PMKID, timestamp=0,
                                                   path=job.capture_path,
                                                   bssid="fc:69:0c:15:82:64")))


def test_each_password_source_is_its_own_queue_entry_recorded_once_exhausted(tool, tmp_path,
                                                                             mocker):
    default = Path(_write(tmp_path / "default_passwords.txt", ["defaultpass", "CafeGuest"]))
    mocker.patch("wifit3.vault.tools.cpu_crack.DEFAULT_PASSWORDS_PATH", default)
    wordlist = _write(tmp_path / "words.txt", ["unrelated", "CafeGuest"])
    job = _launch_with(tool, tmp_path, [_UNCRACKABLE_SHORT_ESSID_LINE], wordlists=[wordlist],
                       default_passwords=True, ap_ssid_passwords=True,
                       extra_passwords=["CafeGuest"])

    _join(tool, job)
    assert list(tool._passwords(tool._runs[job.job_id])) == ["defaultpass", "CafeGuest"]
    ssid_entry = tool.poll_status(job)
    assert (ssid_entry.status, ssid_entry.detail) == (ToolStatus.RUNNING, "SSID passwords (2/3)")
    assert _capture_exhaustion(job) == {wordlist_identity(default)}

    _join(tool, job)
    assert list(tool._passwords(tool._runs[job.job_id])) == list(dict.fromkeys(
        ssid_variants("ab") + ssid_passwords(["CafeGuest"])))
    wordlist_entry = tool.poll_status(job)
    assert wordlist_entry.detail == "words.txt (3/3)"
    assert _capture_exhaustion(job) == {wordlist_identity(default), SSID_VARIANTS_IDENTITY}

    _join(tool, job)
    assert list(tool._passwords(tool._runs[job.job_id])) == ["unrelated", "CafeGuest"]
    not_found = tool.poll_status(job)
    assert (not_found.status, not_found.value) == (ToolStatus.FAILURE, "2 wordlists")
    assert _capture_exhaustion(job) == {wordlist_identity(default), SSID_VARIANTS_IDENTITY,
                                        wordlist_identity(wordlist)}


def test_skip_moves_from_the_ssid_passwords_to_the_wordlist_without_recording_them(tool, tmp_path,
                                                                                  mocker):
    entered, release = _gate_pmk(mocker)
    wordlist = _write(tmp_path / "words.txt", ["unrelated"])
    job = _launch_with(tool, tmp_path, [_UNCRACKABLE_SHORT_ESSID_LINE], wordlists=[wordlist],
                       ap_ssid_passwords=True)
    assert entered.wait(_JOIN_SECONDS)
    running = tool.poll_status(job)
    assert running.detail == "SSID passwords (1/2)"
    assert running.actions == JobAction.STOP | JobAction.SKIP | JobAction.PAUSE

    assert tool.apply(JobAction.SKIP, job) is True
    release.set()
    skipped = _poll_finished(tool, job)
    assert (skipped.status, skipped.detail) == (ToolStatus.RUNNING, "words.txt (2/2)")
    assert _capture_exhaustion(job) == set()

    assert _poll_finished(tool, job).value == "words.txt"
    assert _capture_exhaustion(job) == {wordlist_identity(wordlist)}


def test_exhausted_probed_ssid_passwords_alone_record_nothing(tool, tmp_path):
    wordlist = _write(tmp_path / "words.txt", ["unrelated"])
    job = _launch_with(tool, tmp_path, [_UNCRACKABLE_SHORT_ESSID_LINE], wordlists=[wordlist],
                       ap_ssid_passwords=False, extra_passwords=["CafeGuest"])

    assert _poll_finished(tool, job).detail == "words.txt (2/2)"
    assert _capture_exhaustion(job) == set()
    assert _poll_finished(tool, job).status == ToolStatus.FAILURE
    assert _capture_exhaustion(job) == {wordlist_identity(wordlist)}
