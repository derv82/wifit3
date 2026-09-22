import subprocess
import sys

import pytest

from wifit3.vault.tools.hashcat import HashcatTool
from wifit3.models import PersistedCapture
from wifit3.models.access_point import CaptureType
from wifit3.models.jobs import ToolStatus


def test_poll_status_reports_key_from_potfile(tmp_path):
    """A recovered key is read straight from the potfile (no `--show` subprocess), even for an
    unknown/hung pid, so a hashcat that hangs after cracking still resolves to SUCCESS."""
    cap = tmp_path / "ASUS.hc22000"
    cap.write_text("WPA*01*deadbeef*aabbccddeeff*001122334455*41535553***\n")
    (tmp_path / "ASUS.potfile").write_text(
        "13b42a8a0ec63a94b4d25ae450e5aa4b4d00ee971eb82a4d631ca62b79309510*41535553:0xdeadbeef\n"
    )
    res = HashcatTool().poll_status({"capture_path": str(cap), "pid": 999999})
    assert res.status == ToolStatus.SUCCESS
    assert res.result_data == {"key": "0xdeadbeef"}


def test_poll_status_running_uses_popen_handle(tmp_path):
    """Liveness comes from the tracked Popen handle, never os.kill(pid, 0)."""
    tool = HashcatTool()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    tool._procs[proc.pid] = proc
    try:
        log = tmp_path / "j.log"
        log.write_text('{ "session": "hashcat", "status": 3, "progress": [1, 4] }\n')
        res = tool.poll_status({"log_path": str(log), "pid": proc.pid,
                                "capture_path": str(tmp_path / "x.hc22000")})
        assert res.status == ToolStatus.RUNNING
        assert "25.00%" in res.value
    finally:
        proc.kill()


def test_poll_status_surfaces_hashcat_error(tmp_path):
    """A dead job that neither cracked nor exhausted reports hashcat's own last line."""
    log = tmp_path / "j.log"
    log.write_text(
        "hashcat (v7.1.2) starting\n\n"
        "Already an instance C:\\tools\\hashcat.exe running on pid 88952\n\n"
        "Started: Sun Sep 20\nStopped: Sun Sep 20\n"
    )
    res = HashcatTool().poll_status({"log_path": str(log), "pid": None, "config": {}}, assume_dead=True)
    assert res.status == ToolStatus.ERROR
    assert "Already an instance" in res.value


def test_poll_status_exhausted_reports_wordlist(tmp_path):
    log = tmp_path / "j.log"
    log.write_text('{ "session": "hashcat", "status": 5, "progress": [3, 3] }\n')
    wordlist = str(tmp_path / "rockyou.txt")   # native path so Path(...).name works on any OS
    res = HashcatTool().poll_status(
        {"log_path": str(log), "pid": None, "config": {"wordlist": wordlist}},
        assume_dead=True)
    assert res.status == ToolStatus.FAILURE
    assert res.value == "rockyou.txt"


def test_parse_progress_computes_percent(tmp_path):
    log = tmp_path / "j.log"
    log.write_text('{ "session": "hashcat", "status": 3, "progress": [1, 4] }\n')
    assert "25.00%" in HashcatTool().parse_progress(str(log))


def _cap(tmp_path):
    return PersistedCapture(type=CaptureType.HS, timestamp=0,
                            path=str(tmp_path / "a.hc22000"), bssid="00:11:22:33:44:55")


def test_launch_requires_wordlist(tmp_path):
    exe = tmp_path / "hashcat.exe"
    exe.write_text("")
    with pytest.raises(ValueError):
        HashcatTool().launch(_cap(tmp_path), {"hashcat_exe": str(exe)})  # no wordlist


def test_launch_wordlist_must_exist(tmp_path):
    exe = tmp_path / "hashcat.exe"
    exe.write_text("")
    with pytest.raises(ValueError, match="Wordlist not found"):
        HashcatTool().launch(_cap(tmp_path),
                             {"hashcat_exe": str(exe), "wordlist": str(tmp_path / "missing.txt")})


def test_poll_status_reaps_procs_on_exhausted(tmp_path):
    """A finished-but-not-cracked job drops its process-tracking entry (no leak)."""
    tool = HashcatTool()
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    tool._procs[proc.pid] = proc
    log = tmp_path / "j.log"
    log.write_text('{ "session": "hashcat", "status": 5, "progress": [3, 3] }\n')
    res = tool.poll_status({"log_path": str(log), "pid": proc.pid,
                            "capture_path": str(tmp_path / "x.hc22000"),
                            "config": {"wordlist": "rockyou.txt"}})
    assert res.status == ToolStatus.FAILURE
    assert proc.pid not in tool._procs


def test_is_running_true_for_live_pid_without_handle():
    """Adopted across a restart (no Popen handle): a live PID is detected via the OS."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    try:
        assert HashcatTool()._is_running(proc.pid) is True
    finally:
        proc.kill()


def test_is_running_false_for_dead_pid_without_handle():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert HashcatTool()._is_running(proc.pid) is False


@pytest.mark.skipif(sys.platform == "darwin", reason="no /proc; image identity unavailable on macOS")
def test_is_running_rejects_reused_pid_of_another_program():
    """A live PID whose image is not the configured hashcat exe is not our crack (PID reuse).
    Works via QueryFullProcessImageNameW on Windows and /proc/<pid>/exe on Linux."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    try:
        # The live process is python, not hashcat -> the identity guard rejects it.
        assert HashcatTool()._is_running(proc.pid, exe="hashcat.exe") is False
    finally:
        proc.kill()


def test_poll_status_adopts_running_via_live_pid(tmp_path):
    """No handle we own, but a live PID -> the adopted process is still RUNNING."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    try:
        log = tmp_path / "j.log"
        log.write_text('{ "session": "hashcat", "status": 3, "progress": [1, 4] }\n')
        res = HashcatTool().poll_status({"log_path": str(log), "pid": proc.pid,
                                         "capture_path": str(tmp_path / "x.hc22000")})
        assert res.status == ToolStatus.RUNNING
    finally:
        proc.kill()
