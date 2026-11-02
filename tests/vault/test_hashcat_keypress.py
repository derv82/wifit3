import os
import subprocess
from pathlib import Path

import pytest

from wifit3.vault.tools import hashcat_keypress

posix_only = pytest.mark.skipif(os.name == "nt", reason="named pipes for stdin are POSIX")
windows_only = pytest.mark.skipif(os.name != "nt", reason="console input is Windows")


def test_send_is_refused_without_a_pid_or_a_stdin_path():
    assert hashcat_keypress.send("q", pid=None, stdin_path="/dev/null") is False
    assert hashcat_keypress.send("q", pid=42, stdin_path="") is False


@posix_only
def test_a_keypress_reaches_whoever_is_reading_the_pipe(tmp_path):
    pipe = tmp_path / "keys"
    os.mkfifo(pipe)
    hashcat = os.open(pipe, os.O_RDWR)          # as hashcat holds it: reader and writer at once
    try:
        assert hashcat_keypress.send("p", pid=1, stdin_path=str(pipe)) is True
        assert os.read(hashcat, 8) == b"p\n"
    finally:
        os.close(hashcat)


@posix_only
def test_a_keypress_is_refused_when_nothing_is_reading_the_pipe(tmp_path):
    """A hashcat that has exited leaves its pipe behind; opening it for writing is how we find out."""
    pipe = tmp_path / "keys"
    os.mkfifo(pipe)
    assert hashcat_keypress.send("p", pid=1, stdin_path=str(pipe)) is False


@posix_only
def test_spawn_gives_hashcat_a_named_pipe_beside_its_log(tmp_path, mocker):
    popen = mocker.patch("wifit3.vault.tools.hashcat_keypress.subprocess.Popen")
    popen.return_value.pid = 99
    log = tmp_path / "job.log"

    proc, pid, stdin_path = hashcat_keypress.spawn(["/bin/true"], cwd=str(tmp_path),
                                                   log_path=str(log))

    assert (proc, pid) == (popen.return_value, 99)
    assert stdin_path == str(log) + ".keys"
    assert Path(stdin_path).is_fifo()
    assert popen.call_args.kwargs["stdin"] >= 0          # a real fd, not DEVNULL
    assert popen.call_args.kwargs["start_new_session"] is True


@posix_only
def test_spawn_without_a_named_pipe_still_runs_hashcat(tmp_path, mocker):
    """A filesystem that cannot hold a fifo costs the user the pause button, not the job."""
    popen = mocker.patch("wifit3.vault.tools.hashcat_keypress.subprocess.Popen")
    popen.return_value.pid = 99
    mocker.patch("wifit3.vault.tools.hashcat_keypress.os.mkfifo", side_effect=OSError(1, "nope"))

    proc, pid, stdin_path = hashcat_keypress.spawn(
        ["/bin/true"], cwd=str(tmp_path), log_path=str(tmp_path / "job.log"))

    assert (pid, stdin_path) == (99, "")
    assert popen.call_args.kwargs["stdin"] is subprocess.DEVNULL


@windows_only
def test_spawn_returns_the_pid_hashcat_console_printed(tmp_path, mocker):
    run = mocker.patch("wifit3.vault.tools.hashcat_keypress.subprocess.run",
                       return_value=subprocess.CompletedProcess([], 0, stdout="777\n", stderr=""))
    log = tmp_path / "job.log"

    proc, pid, stdin_path = hashcat_keypress.spawn(["hashcat.exe", "-m", "22000"],
                                                   cwd=str(tmp_path), log_path=str(log))

    assert (proc, pid, stdin_path) == (None, 777, hashcat_keypress.CONSOLE_INPUT)
    assert run.call_args.args[0][1:] == [
        "-m", "wifit3.vault.tools.hashcat_console", "launch", str(tmp_path), str(log),
        "hashcat.exe", "-m", "22000",
    ]


@windows_only
def test_spawn_without_a_console_input_buffer_still_runs_hashcat(tmp_path, mocker):
    """hashcat_console failing costs the user the pause button, not the job."""
    mocker.patch("wifit3.vault.tools.hashcat_keypress.subprocess.run",
                 return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="boom"))
    popen = mocker.patch("wifit3.vault.tools.hashcat_keypress.subprocess.Popen")
    popen.return_value.pid = 99

    proc, pid, stdin_path = hashcat_keypress.spawn(
        ["hashcat.exe"], cwd=str(tmp_path), log_path=str(tmp_path / "job.log"))

    assert (proc, pid, stdin_path) == (popen.return_value, 99, "")
    assert popen.call_args.kwargs["stdin"] is subprocess.DEVNULL


@windows_only
def test_send_passes_the_pid_and_the_key_to_hashcat_console(mocker):
    run = mocker.patch("wifit3.vault.tools.hashcat_keypress.subprocess.run",
                       return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))

    assert hashcat_keypress.send("q", pid=4242, stdin_path=hashcat_keypress.CONSOLE_INPUT) is True
    assert run.call_args.args[0][1:] == [
        "-m", "wifit3.vault.tools.hashcat_console", "keypress", "4242", "q"]

    run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="no such console")
    assert hashcat_keypress.send("q", pid=4242, stdin_path=hashcat_keypress.CONSOLE_INPUT) is False
