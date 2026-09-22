import subprocess
import sys

from wifit3.vault.tools.hashcat import HashcatTool
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
    res = HashcatTool().poll_status(
        {"log_path": str(log), "pid": None, "config": {"wordlist": r"D:\lists\rockyou.txt"}},
        assume_dead=True)
    assert res.status == ToolStatus.FAILURE
    assert res.value == "rockyou.txt"


def test_parse_progress_computes_percent(tmp_path):
    log = tmp_path / "j.log"
    log.write_text('{ "session": "hashcat", "status": 3, "progress": [1, 4] }\n')
    assert "25.00%" in HashcatTool().parse_progress(str(log))
