import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from wifit3.crack.passwords import DEFAULT_PASSWORDS_PATH, ssid_passwords, ssid_variants
from wifit3.persist.config import Config
from wifit3.vault.exhaustion import SSID_VARIANTS_IDENTITY, wordlist_identity
from wifit3.vault.tools.hashcat import (
    HashcatTool, _TERMINATE_WAIT_SECONDS, _read_log_tail, _split_args,
)
from wifit3.models import PersistedCapture
from wifit3.models.access_point import CaptureType
from wifit3.models.jobs import JobAction, JobState, ToolStatus


def _job(**fields) -> JobState:
    """A RUNNING hashcat job carrying only the fields a test cares about."""
    return JobState(job_id="hashcat-test", tool_name="hashcat",
                    capture_path=fields.pop("capture_path", ""),
                    status=ToolStatus.RUNNING, progress_msg="", **fields)


def _cap(tmp_path):
    return PersistedCapture(type=CaptureType.HS, timestamp=0,
                            path=str(tmp_path / "a.hc22000"), bssid="00:11:22:33:44:55")


def _queued_job(tmp_path, wordlists, args=None):
    """A job ready for launch: a real capture file and a hashcat_exe that exists."""
    (tmp_path / "a.hc22000").write_text("WPA*01*deadbeef\n")
    config = {"hashcat_exe": sys.executable, "wordlists": [str(w) for w in wordlists]}
    if args is not None:
        config["hashcat_args"] = args
    return _job(capture_path=str(tmp_path / "a.hc22000"), config=config)


def _fake_spawn(mocker, pid=4242, alive=True, stdin_path=""):
    """Stand in for hashcat_keypress.spawn: one fake process, and no stdin for keypresses
    unless a test asks for it, so by default a job can only be ended by being killed."""
    proc = mocker.Mock()
    proc.pid = pid
    proc.poll.return_value = None if alive else 1
    spawn = mocker.patch("wifit3.vault.tools.hashcat_keypress.spawn",
                         return_value=(proc, pid, stdin_path))
    spawn.proc = proc
    return spawn


def _fake_keys(mocker, delivered=True):
    """Stand in for hashcat_keypress.send; records every keypress sent to hashcat."""
    return mocker.patch("wifit3.vault.tools.hashcat_keypress.send", return_value=delivered)


def _live_tool(mocker, pid=4242) -> HashcatTool:
    """A tool holding a handle on a still-running process with that pid."""
    tool = HashcatTool()
    proc = mocker.Mock()
    proc.poll.return_value = None
    tool._procs[pid] = proc
    return tool


def _append(log_path, text):
    with open(log_path, "a", encoding="utf-8") as stream:
        stream.write(text)


# ----- status ---------------------------------------------------------------

def test_poll_status_reports_key_from_potfile(tmp_path):
    """A recovered key is read straight from the potfile (no `--show` subprocess), even for an
    unknown/hung pid, so a hashcat that hangs after cracking still resolves to SUCCESS."""
    cap = tmp_path / "ASUS.hc22000"
    cap.write_text("WPA*01*deadbeef*aabbccddeeff*001122334455*41535553***\n")
    (tmp_path / "ASUS.potfile").write_text(
        "13b42a8a0ec63a94b4d25ae450e5aa4b4d00ee971eb82a4d631ca62b79309510*41535553:0xdeadbeef\n"
    )
    res = HashcatTool().poll_status(_job(capture_path=str(cap), pid=999999))
    assert res.status == ToolStatus.SUCCESS
    assert res.key == "0xdeadbeef"


def test_poll_status_prefers_a_recovered_key_over_a_stop_request(tmp_path):
    """Stopping a job that already found the key still reports the key."""
    cap = tmp_path / "test.hc22000"
    cap.write_text("WPA*01*deadbeef\n")
    cap.with_suffix(".potfile").write_text("hash:recovered-key\n")

    res = HashcatTool().poll_status(
        _job(capture_path=str(cap), tool_state={"stopped": True}), assume_dead=True)

    assert res.status == ToolStatus.SUCCESS
    assert res.key == "recovered-key"


def test_poll_status_running_uses_popen_handle(tmp_path):
    """Liveness comes from the tracked Popen handle, never os.kill(pid, 0)."""
    tool = HashcatTool()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    tool._procs[proc.pid] = proc
    try:
        log = tmp_path / "j.log"
        log.write_text('{ "session": "hashcat", "status": 3, "progress": [1, 4] }\n')
        res = tool.poll_status(_job(log_path=str(log), pid=proc.pid,
                                    capture_path=str(tmp_path / "x.hc22000")))
        assert res.status == ToolStatus.RUNNING
        assert "25.00%" in res.value
    finally:
        proc.kill()


def test_poll_status_names_the_running_wordlist_and_its_place_in_the_queue(tmp_path, mocker):
    log = tmp_path / "j.log"
    log.write_text('{ "status": 3, "progress": [1, 4] }\n')
    job = _job(log_path=str(log), capture_path=str(tmp_path / "x.hc22000"), pid=4242,
               tool_state={"wordlists": ["/dicts/a.txt", "/dicts/b.txt", "/dicts/c.txt"],
                           "index": 1})

    res = _live_tool(mocker).poll_status(job)

    assert res.value == "Running (25.00%) · b.txt (2/3)"
    assert res.actions == JobAction.STOP | JobAction.SKIP


def test_poll_status_offers_no_skip_on_the_last_wordlist(tmp_path, mocker):
    log = tmp_path / "j.log"
    log.write_text('{ "status": 3, "progress": [1, 4] }\n')
    job = _job(log_path=str(log), capture_path=str(tmp_path / "x.hc22000"), pid=4242,
               tool_state={"wordlists": ["/dicts/a.txt", "/dicts/b.txt"], "index": 1})

    assert _live_tool(mocker).poll_status(job).actions == JobAction.STOP


def test_poll_status_omits_the_counter_for_a_single_wordlist(tmp_path, mocker):
    log = tmp_path / "j.log"
    log.write_text('{ "status": 3, "progress": [1, 4] }\n')
    job = _job(log_path=str(log), capture_path=str(tmp_path / "x.hc22000"), pid=4242,
               tool_state={"wordlists": ["/dicts/rockyou.txt"]})

    assert _live_tool(mocker).poll_status(job).value == "Running (25.00%) · rockyou.txt"


def test_poll_status_surfaces_hashcat_error(tmp_path):
    """A dead job that neither cracked nor exhausted reports hashcat's own last line."""
    log = tmp_path / "j.log"
    log.write_text(
        "hashcat (v7.1.2) starting\n\n"
        "Already an instance C:\\tools\\hashcat.exe running on pid 88952\n\n"
        "Started: Sun Sep 20\nStopped: Sun Sep 20\n"
    )
    res = HashcatTool().poll_status(_job(log_path=str(log)), assume_dead=True)
    assert res.status == ToolStatus.ERROR
    assert "Already an instance" in res.value


def test_poll_status_reports_stopped_once_a_stopped_job_is_gone(tmp_path):
    log = tmp_path / "j.log"
    log.write_text("Started: Sun Sep 20\nStopped: Sun Sep 20\n")

    res = HashcatTool().poll_status(
        _job(log_path=str(log), tool_state={"stopped": True}), assume_dead=True)

    assert res.status == ToolStatus.STOPPED
    assert res.value == "Stopped by user"
    assert res.actions == JobAction.NONE


def test_hashcats_own_stopped_line_without_a_user_stop_remains_error(tmp_path):
    log = tmp_path / "j.log"
    log.write_text("Started: Sun Sep 20\nStopped: Sun Sep 20\n")

    res = HashcatTool().poll_status(_job(log_path=str(log)), assume_dead=True)

    assert res.status == ToolStatus.ERROR


def test_poll_status_exhausted_reports_the_only_wordlist_by_name(tmp_path):
    log = tmp_path / "j.log"
    log.write_text('{ "session": "hashcat", "status": 5, "progress": [3, 3] }\n')
    wordlist = str(tmp_path / "rockyou.txt")   # native path so Path(...).name works on any OS
    res = HashcatTool().poll_status(
        _job(log_path=str(log), tool_state={"wordlists": [wordlist]}), assume_dead=True)
    assert res.status == ToolStatus.FAILURE
    assert res.value == "rockyou.txt"


def test_poll_status_exhausted_counts_a_queue_of_wordlists(tmp_path):
    log = tmp_path / "j.log"
    log.write_text('{ "status": 5, "progress": [3, 3] }\n')
    job = _job(log_path=str(log), tool_state={"wordlists": ["a.txt", "b.txt", "c.txt"], "index": 2})

    res = HashcatTool().poll_status(job, assume_dead=True)

    assert res.status == ToolStatus.FAILURE
    assert res.value == "3 wordlists"


# ----- the wordlist queue ---------------------------------------------------

def test_launch_runs_hashcat_against_the_first_wordlist_only(tmp_path, mocker):
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text("one\n")
    second.write_text("two\n")
    spawn = _fake_spawn(mocker)
    job = _queued_job(tmp_path, [first, second])

    HashcatTool().launch(job, _cap(tmp_path))

    command = spawn.call_args.args[0]
    assert command[-2:] == [str((tmp_path / "a.hc22000").resolve()), str(first.resolve())]
    assert job.pid == 4242
    assert job.tool_state["wordlists"] == [str(first.resolve()), str(second.resolve())]
    assert job.tool_state["index"] == 0
    assert "shell" not in spawn.call_args.kwargs


def test_launch_records_where_to_send_keypresses_to_the_running_hashcat(tmp_path, mocker):
    wordlist = tmp_path / "a.txt"
    wordlist.write_text("one\n")
    spawn = _fake_spawn(mocker, stdin_path="/tmp/a.hashcat.log.keys")
    job = _queued_job(tmp_path, [wordlist])

    HashcatTool().launch(job, _cap(tmp_path))

    assert spawn.call_args.kwargs["log_path"] == job.log_path
    assert job.tool_state["stdin_path"] == "/tmp/a.hashcat.log.keys"


def test_poll_status_advances_to_the_next_wordlist_when_one_is_exhausted(tmp_path, mocker):
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text("one\n")
    second.write_text("two\n")
    spawn = _fake_spawn(mocker)
    tool = HashcatTool()
    job = _queued_job(tmp_path, [first, second])
    tool.launch(job, _cap(tmp_path))
    _append(job.log_path, '{ "status": 5, "progress": [3, 3] }\n')
    spawn.proc.poll.return_value = 1        # the first hashcat has exited

    res = tool.poll_status(job)

    assert res.status == ToolStatus.RUNNING
    assert res.value == "Starting... · b.txt (2/2)"
    assert res.actions == JobAction.STOP
    assert job.tool_state["index"] == 1
    assert spawn.call_count == 2
    assert spawn.call_args.args[0][-1] == str(second.resolve())


def test_poll_status_does_not_re_read_the_previous_wordlists_exhausted_line(tmp_path, mocker):
    """Each run's output is read from its own offset, so an advance cannot cascade."""
    wordlists = [tmp_path / "a.txt", tmp_path / "b.txt", tmp_path / "c.txt"]
    for wordlist in wordlists:
        wordlist.write_text("one\n")
    spawn = _fake_spawn(mocker)
    tool = HashcatTool()
    job = _queued_job(tmp_path, wordlists)
    tool.launch(job, _cap(tmp_path))
    _append(job.log_path, '{ "status": 5, "progress": [3, 3] }\n')
    spawn.proc.poll.return_value = 1
    tool.poll_status(job)                           # advances to b.txt
    spawn.proc.poll.return_value = None     # b.txt is running, has logged nothing yet

    res = tool.poll_status(job)

    assert res.status == ToolStatus.RUNNING
    assert job.tool_state["index"] == 1
    assert spawn.call_count == 2


def test_apply_skip_abandons_the_running_wordlist_for_the_next(tmp_path, mocker):
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text("one\n")
    second.write_text("two\n")
    spawn = _fake_spawn(mocker)
    tool = HashcatTool()
    job = _queued_job(tmp_path, [first, second])
    tool.launch(job, _cap(tmp_path))

    assert tool.apply(JobAction.SKIP, job) is True

    spawn.proc.kill.assert_called_once_with()
    assert job.tool_state["index"] == 1
    assert spawn.call_args.args[0][-1] == str(second.resolve())


_SHORT_ESSID_LINE = "WPA*01*00000000000000000000000000000000*020000000001*020000000002*6162***"


def _sources_job(tmp_path, wordlists, **sources) -> JobState:
    (tmp_path / "a.hc22000").write_text(f"{_SHORT_ESSID_LINE}\n")
    return _job(capture_path=str(tmp_path / "a.hc22000"),
                config={"hashcat_exe": sys.executable, "wordlists": [str(w) for w in wordlists],
                        **sources})


def test_launch_runs_the_default_passwords_then_the_ssid_passwords_first(tmp_path, mocker):
    wordlist = tmp_path / "rockyou.txt"
    wordlist.write_text("one\n")
    spawn = _fake_spawn(mocker)
    tool = HashcatTool()
    job = _sources_job(tmp_path, [wordlist], default_passwords=True, ap_ssid_passwords=True,
                       probed_ssid_passwords=True, extra_passwords=["CafeGuest"])

    tool.launch(job, _cap(tmp_path))

    ssid_passwords_path = Path(job.tool_state["ssid_passwords_path"])
    assert ssid_passwords_path.parent == Path(tempfile.gettempdir())
    assert ssid_passwords_path.name.startswith("wifit3-")
    assert job.tool_state["wordlists"] == [str(DEFAULT_PASSWORDS_PATH.resolve()),
                                           str(ssid_passwords_path),
                                           str(wordlist.resolve())]
    assert ssid_passwords_path.read_text(encoding="utf-8").splitlines() == list(dict.fromkeys(
        ssid_variants("ab") + ssid_passwords(["CafeGuest"])))
    assert spawn.call_args.args[0][-1] == str(DEFAULT_PASSWORDS_PATH.resolve())
    assert job.tool_state["identities"] == [
        wordlist_identity(DEFAULT_PASSWORDS_PATH), SSID_VARIANTS_IDENTITY, wordlist_identity(wordlist)]
    assert tool.poll_status(job).detail == "default_passwords.txt (1/3)"

    job.tool_state["index"] = 1
    assert tool.poll_status(job).detail == "SSID passwords (2/3)"

    job.tool_state["index"] = 2
    _append(job.log_path, '{ "status": 5, "progress": [3, 3] }\n')
    spawn.proc.poll.return_value = 1
    res = tool.poll_status(job)
    assert (res.status, res.value) == (ToolStatus.FAILURE, "2 wordlists")
    assert not ssid_passwords_path.exists()


def test_launch_with_only_the_ssid_passwords_and_a_failed_spawn_removes_them(tmp_path, mocker):
    mocker.patch("wifit3.vault.tools.hashcat_keypress.spawn", side_effect=OSError("no hashcat"))
    mkstemp = mocker.spy(tempfile, "mkstemp")
    job = _sources_job(tmp_path, [], ap_ssid_passwords=True)

    with pytest.raises(OSError):
        HashcatTool().launch(job, _cap(tmp_path))

    assert not Path(mkstemp.spy_return[1]).exists()


def test_auto_crack_config_needs_a_path_and_a_wordlist_or_password_source():
    Config.hashcat.path = "/usr/bin/hashcat"
    assert HashcatTool().auto_crack_config() == {
        "hashcat_exe": "/usr/bin/hashcat", "wordlists": [], "hashcat_args": "",
        "default_passwords": True, "ap_ssid_passwords": True, "probed_ssid_passwords": True}
    Config.hashcat.default_passwords = False
    Config.hashcat.ap_ssid_passwords = False
    Config.hashcat.probed_ssid_passwords = False
    assert HashcatTool().auto_crack_config() is None


def _pidfile_job(tmp_path, exe: Path, wordlist: Path) -> JobState:
    (tmp_path / "a.hc22000").write_text("WPA*01*deadbeef\n")
    exe.write_text("")
    wordlist.write_text("one\n")
    return _job(capture_path=str(tmp_path / "a.hc22000"),
                config={"hashcat_exe": str(exe), "wordlists": [str(wordlist)]})


def test_launch_removes_a_pidfile_left_behind_by_a_killed_run(tmp_path, mocker):
    """hashcat refuses to start while the pid in its session pidfile is alive, and the run we
    killed never got to remove that file."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    pidfile = tmp_path / "hashcat.pid"
    pidfile.write_bytes(dead.pid.to_bytes(4, "little"))
    _fake_spawn(mocker)
    job = _pidfile_job(tmp_path, tmp_path / "hashcat.exe", tmp_path / "a.txt")

    HashcatTool().launch(job, _cap(tmp_path))

    assert not pidfile.exists()


def test_launch_keeps_the_pidfile_of_a_hashcat_that_is_really_running(tmp_path, mocker):
    """The pidfile is hashcat's guard against two instances sharing a potfile; only a dead pid
    makes it stale."""
    running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        pidfile = tmp_path / "hashcat.pid"
        pidfile.write_bytes(running.pid.to_bytes(4, "little"))
        _fake_spawn(mocker)
        # named for the live process above, so the pid's image matches the configured exe
        job = _pidfile_job(tmp_path, tmp_path / (Path(sys.executable).stem + ".exe"),
                           tmp_path / "a.txt")

        HashcatTool().launch(job, _cap(tmp_path))

        assert pidfile.exists()
    finally:
        running.kill()
        running.wait(timeout=5)


def test_skip_starts_the_next_wordlist_only_once_the_old_process_is_gone(tmp_path, mocker):
    """hashcat refuses to start while another instance of the same executable is alive, so the
    skipped process has to be reaped before the next one is launched."""
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text("one\n")
    second.write_text("two\n")
    spawn = _fake_spawn(mocker)
    reaped_before_launch = []

    def record(*args, **kwargs):
        reaped_before_launch.append(spawn.proc.wait.called)
        return spawn.proc, spawn.proc.pid, ""
    spawn.side_effect = record
    tool = HashcatTool()
    job = _queued_job(tmp_path, [first, second])
    tool.launch(job, _cap(tmp_path))

    assert tool.apply(JobAction.SKIP, job) is True

    assert reaped_before_launch == [False, True]
    assert spawn.call_args.args[0][-1] == str(second.resolve())


def test_apply_skip_is_refused_on_the_last_wordlist(tmp_path, mocker):
    wordlist = tmp_path / "a.txt"
    wordlist.write_text("one\n")
    spawn = _fake_spawn(mocker)
    tool = HashcatTool()
    job = _queued_job(tmp_path, [wordlist])
    tool.launch(job, _cap(tmp_path))

    assert tool.apply(JobAction.SKIP, job) is False
    assert spawn.call_count == 1
    spawn.proc.kill.assert_not_called()


def test_apply_stop_kills_the_process_and_marks_the_job_stopped(tmp_path, mocker):
    wordlist = tmp_path / "a.txt"
    wordlist.write_text("one\n")
    spawn = _fake_spawn(mocker)
    tool = HashcatTool()
    job = _queued_job(tmp_path, [wordlist])
    tool.launch(job, _cap(tmp_path))

    assert tool.apply(JobAction.STOP, job) is True

    spawn.proc.kill.assert_called_once_with()
    assert job.tool_state["stopped"] is True
    assert tool.poll_status(job, assume_dead=True).status == ToolStatus.STOPPED


# ----- keys -----------------------------------------------------------------

def _reachable_job(tmp_path, mocker, wordlists):
    """A launched job whose hashcat accepts keypresses, plus the spawn and send stand-ins."""
    spawn = _fake_spawn(mocker, stdin_path="/keys")
    keys = _fake_keys(mocker)
    tool = HashcatTool()
    job = _queued_job(tmp_path, wordlists)
    tool.launch(job, _cap(tmp_path))
    return tool, job, spawn, keys


def test_stop_asks_hashcat_to_quit_instead_of_killing_it(tmp_path, mocker):
    """A clean exit flushes hashcat's files and removes the pidfile that would block the
    next run."""
    wordlist = tmp_path / "a.txt"
    wordlist.write_text("one\n")
    tool, job, spawn, keys = _reachable_job(tmp_path, mocker, [wordlist])

    assert tool.apply(JobAction.STOP, job) is True

    assert keys.call_args.args[0] == "q"
    assert keys.call_args.kwargs == {"pid": 4242, "stdin_path": "/keys"}
    spawn.proc.kill.assert_not_called()
    assert job.tool_state["stopped"] is True


def test_stop_kills_hashcat_when_it_takes_no_keypresses(tmp_path, mocker):
    wordlist = tmp_path / "a.txt"
    wordlist.write_text("one\n")
    spawn = _fake_spawn(mocker)
    tool = HashcatTool()
    job = _queued_job(tmp_path, [wordlist])
    tool.launch(job, _cap(tmp_path))

    assert tool.apply(JobAction.STOP, job) is True

    spawn.proc.kill.assert_called_once_with()


def test_skip_waits_for_the_quit_before_starting_the_next_wordlist(tmp_path, mocker):
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text("one\n")
    second.write_text("two\n")
    tool, job, spawn, keys = _reachable_job(tmp_path, mocker, [first, second])

    assert tool.apply(JobAction.SKIP, job) is True

    assert keys.call_args.args[0] == "q"
    assert spawn.call_count == 1                        # the next one has not been launched
    assert job.tool_state["index"] == 0
    spawn.proc.kill.assert_not_called()

    spawn.proc.poll.return_value = 1                    # the quit took effect
    res = tool.poll_status(job)

    assert res.status == ToolStatus.RUNNING
    assert job.tool_state["index"] == 1
    assert spawn.call_args.args[0][-1] == str(second.resolve())


def test_pause_and_resume_send_a_keypress_to_hashcat(tmp_path, mocker):
    wordlist = tmp_path / "a.txt"
    wordlist.write_text("one\n")
    tool, job, _spawn, keys = _reachable_job(tmp_path, mocker, [wordlist])

    assert tool.apply(JobAction.PAUSE, job) is True
    assert keys.call_args.args[0] == "p"

    assert tool.apply(JobAction.RESUME, job) is True
    assert keys.call_args.args[0] == "r"


def test_pause_is_refused_when_hashcat_takes_no_keypresses(tmp_path, mocker):
    wordlist = tmp_path / "a.txt"
    wordlist.write_text("one\n")
    _fake_spawn(mocker)
    tool = HashcatTool()
    job = _queued_job(tmp_path, [wordlist])
    tool.launch(job, _cap(tmp_path))

    assert tool.apply(JobAction.PAUSE, job) is False


def test_a_running_job_offers_pause_and_a_paused_one_offers_resume(tmp_path, mocker):
    wordlist = tmp_path / "a.txt"
    wordlist.write_text("one\n")
    tool, job, _spawn, _keys = _reachable_job(tmp_path, mocker, [wordlist])

    _append(job.log_path, '{ "status": 3, "progress": [1, 4] }\n')
    running = tool.poll_status(job)
    assert running.actions == JobAction.STOP | JobAction.PAUSE
    assert (running.percent, running.detail) == (25.0, "a.txt")

    _append(job.log_path, '{ "status": 4, "progress": [1, 4] }\n')
    assert tool.poll_status(job).actions == JobAction.STOP | JobAction.RESUME


def test_a_job_that_takes_no_keypresses_offers_neither(tmp_path, mocker):
    wordlist = tmp_path / "a.txt"
    wordlist.write_text("one\n")
    _fake_spawn(mocker)
    tool = HashcatTool()
    job = _queued_job(tmp_path, [wordlist])
    tool.launch(job, _cap(tmp_path))

    assert tool.poll_status(job).actions == JobAction.STOP


def test_a_job_on_its_way_out_offers_nothing_but_stop(tmp_path, mocker):
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text("one\n")
    second.write_text("two\n")
    tool, job, _spawn, _keys = _reachable_job(tmp_path, mocker, [first, second])

    tool.apply(JobAction.SKIP, job)

    assert tool.poll_status(job).actions == JobAction.STOP


def test_an_ignored_quit_key_escalates_to_a_kill(tmp_path, mocker):
    """hashcat reads no keys until start-up finishes. An early 'q' reaches nothing, and Stop
    must still end the job."""
    wordlist = tmp_path / "a.txt"
    wordlist.write_text("one\n")
    tool, job, spawn, _keys = _reachable_job(tmp_path, mocker, [wordlist])
    tool.apply(JobAction.STOP, job)

    tool.poll_status(job)
    spawn.proc.kill.assert_not_called()

    job.tool_state["quit_sent_at"] = time.time() - 11
    tool.poll_status(job)

    spawn.proc.kill.assert_called_once_with()


def test_launch_clears_a_potfile_from_an_earlier_job(tmp_path, mocker):
    wordlist = tmp_path / "a.txt"
    wordlist.write_text("one\n")
    _fake_spawn(mocker)
    job = _queued_job(tmp_path, [wordlist])
    potfile = tmp_path / "a.potfile"
    potfile.write_text("hash:stale-key\n")

    HashcatTool().launch(job, _cap(tmp_path))

    assert not potfile.exists()


def test_launch_truncates_the_log_so_one_job_owns_one_log(tmp_path, mocker):
    wordlist = tmp_path / "a.txt"
    wordlist.write_text("one\n")
    _fake_spawn(mocker)
    job = _queued_job(tmp_path, [wordlist])
    log = tmp_path / "a.hashcat.log"
    log.write_text("output from a previous job\n")

    HashcatTool().launch(job, _cap(tmp_path))

    assert "previous job" not in Path(job.log_path).read_text(encoding="utf-8")


def test_launch_requires_wordlists(tmp_path):
    exe = tmp_path / "hashcat.exe"
    exe.write_text("")
    with pytest.raises(ValueError):
        HashcatTool().launch(_job(config={"hashcat_exe": str(exe)}), _cap(tmp_path))


def test_launch_wordlist_must_exist(tmp_path):
    exe = tmp_path / "hashcat.exe"
    exe.write_text("")
    with pytest.raises(ValueError, match="Wordlist not found"):
        HashcatTool().launch(
            _job(config={"hashcat_exe": str(exe), "wordlists": [str(tmp_path / "missing.txt")]}),
            _cap(tmp_path))


def test_launch_puts_user_args_before_the_capture_and_wordlist(tmp_path, mocker, caplog):
    """hashcat's usage is [options] hashfile dictionary, so a user option must land ahead of
    both positionals."""
    wordlist = tmp_path / "rockyou.txt"
    wordlist.write_text("hunter2\n")
    _fake_spawn(mocker)
    tool = HashcatTool()
    job = _queued_job(tmp_path, [wordlist], args="-w 3")
    with caplog.at_level(logging.INFO):
        tool.launch(job, _cap(tmp_path))
    launched = next(r.message for r in caplog.records if r.message.startswith("Launching "))
    assert launched.index("-w 3") < launched.index("a.hc22000")


# ----- log reading ----------------------------------------------------------

def test_read_log_tail_returns_all_lines_from_small_file(tmp_path):
    log = tmp_path / "j.log"
    log.write_text("first\nsecond\n")

    assert _read_log_tail(log, limit=1024) == ["first", "second"]


def test_read_log_tail_discards_only_partial_first_line(tmp_path):
    log = tmp_path / "j.log"
    # Bytes, not text: a CRLF translation on Windows would shift the tail offsets.
    log.write_bytes(b"discarded line\nkept one\nkept two\n")

    assert _read_log_tail(log, limit=18) == ["kept one", "kept two"]
    assert _read_log_tail(log, limit=17) == ["kept two"]


def test_read_log_tail_never_reads_before_the_start_offset(tmp_path):
    log = tmp_path / "j.log"
    log.write_bytes(b"earlier run\nthis run\n")

    assert _read_log_tail(log, start=12) == ["this run"]


def test_parse_progress_computes_percent(tmp_path):
    lines = ['{ "session": "hashcat", "status": 3, "progress": [1, 4] }']
    assert HashcatTool()._parse_progress(lines) == ("Running (25.00%)", 25.0)


def test_parse_progress_uses_the_newest_status_line(tmp_path):
    lines = ['{"status":3,"progress":[1,4]}', "padding", '{"status":3,"progress":[3,4]}']
    assert HashcatTool()._parse_progress(lines) == ("Running (75.00%)", 75.0)


def test_parse_progress_is_none_without_a_status_line():
    assert HashcatTool()._parse_progress(["hashcat (v7.1.2) starting"]) is None


# ----- process identity -----------------------------------------------------

def test_kill_force_terminates_owned_hashcat(mocker):
    proc = mocker.Mock()
    proc.poll.return_value = None
    tool = HashcatTool()
    tool._procs[123] = proc

    tool._kill(_job(pid=123))

    proc.kill.assert_called_once_with()
    assert 123 not in tool._procs


def _exhausted_job(tmp_path) -> JobState:
    """A job whose log says hashcat ran out of candidates."""
    log = tmp_path / "j.log"
    log.write_text('{ "session": "hashcat", "status": 5, "progress": [3, 3] }\n')
    return _job(log_path=str(log), pid=777, capture_path=str(tmp_path / "x.hc22000"),
                tool_state={"wordlists": ["rockyou.txt"]})


def test_poll_status_reaps_procs_on_exhausted(tmp_path):
    """A finished-but-not-cracked job drops its process-tracking entry (no leak)."""
    tool = HashcatTool()
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    tool._procs[proc.pid] = proc
    log = tmp_path / "j.log"
    log.write_text('{ "session": "hashcat", "status": 5, "progress": [3, 3] }\n')
    res = tool.poll_status(_job(log_path=str(log), pid=proc.pid,
                                capture_path=str(tmp_path / "x.hc22000"),
                                tool_state={"wordlists": ["rockyou.txt"]}))
    assert res.status == ToolStatus.FAILURE
    assert proc.pid not in tool._procs


def test_a_finished_hashcat_is_waited_on_instead_of_left_as_a_zombie(tmp_path, mocker):
    proc = mocker.Mock()
    proc.poll.return_value = None                # still there when the job's last poll arrives
    tool = HashcatTool()
    tool._procs[777] = proc

    tool.poll_status(_exhausted_job(tmp_path), assume_dead=True)

    proc.terminate.assert_called_once_with()
    proc.wait.assert_called_once()
    assert 777 not in tool._procs


def test_a_hashcat_that_will_not_go_is_reaped_by_a_later_poll(tmp_path, mocker):
    """hashcat sometimes hangs while exiting. Waiting for it would freeze the UI, and dropping
    the Popen would leave the process defunct for good."""
    proc = mocker.Mock()
    proc.poll.return_value = None
    proc.wait.side_effect = subprocess.TimeoutExpired("hashcat", _TERMINATE_WAIT_SECONDS)
    tool = HashcatTool()
    tool._procs[777] = proc
    job = _exhausted_job(tmp_path)

    tool.poll_status(job, assume_dead=True)
    assert 777 in tool._procs

    proc.poll.return_value = 1
    tool.poll_status(job, assume_dead=True)
    assert 777 not in tool._procs


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
        res = HashcatTool().poll_status(_job(log_path=str(log), pid=proc.pid,
                                             capture_path=str(tmp_path / "x.hc22000")))
        assert res.status == ToolStatus.RUNNING
    finally:
        proc.kill()


def test_split_args_is_empty_for_blank_input():
    assert _split_args("") == []


def test_split_args_keeps_a_quoted_windows_path_intact():
    assert _split_args(r'-w 3 --session "D:\my runs\a"') == ["-w", "3", "--session",
                                                             r"D:\my runs\a"]
