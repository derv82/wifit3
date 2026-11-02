"""Starts hashcat with keypress hooks."""
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CONSOLE_INPUT = "CONIN$"        # hashcat's stdin on Windows: a console input buffer
_CONSOLE_MODULE = "wifit3.vault.tools.hashcat_console"
_CONSOLE_TIMEOUT_SECONDS = 30.0


def spawn(cmd: list[str], *, cwd: str, log_path: str) -> tuple[Optional[subprocess.Popen], int, str]:
    """Start hashcat with its output appended to ``log_path``. Returns its Popen (None on Windows,
    where ``hashcat_console`` is its parent), its pid, and the ``stdin_path`` for ``send``."""
    if os.name == "nt":
        return _spawn_with_console_input(cmd, cwd, log_path)
    return _spawn_with_named_pipe(cmd, cwd, log_path)


def send(key: str, *, pid: Optional[int], stdin_path: str) -> bool:
    """Send one keypress to a running hashcat; False when it could not be delivered."""
    if not pid or not stdin_path:
        return False
    if stdin_path == CONSOLE_INPUT:
        return _write_console_input(pid, key)
    return _write_named_pipe(Path(stdin_path), key)


def _spawn_with_named_pipe(cmd, cwd, log_path):
    pipe = Path(log_path + ".keys")
    try:
        pipe.unlink(missing_ok=True)
        os.mkfifo(pipe, 0o600)
        stdin = os.open(pipe, os.O_RDWR)  # Reader & Writer
    except OSError:
        logger.warning(f"hashcat will take no keypresses: could not create {pipe}", exc_info=True)
        proc = _popen(cmd, cwd, log_path, subprocess.DEVNULL)
        return proc, proc.pid, ""
    try:
        proc = _popen(cmd, cwd, log_path, stdin)
    finally:
        os.close(stdin)
    return proc, proc.pid, str(pipe)


def _popen(cmd, cwd, log_path, stdin) -> subprocess.Popen:
    with open(log_path, "ab") as log:
        return subprocess.Popen(cmd, cwd=cwd, stdin=stdin, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)


def _spawn_with_console_input(cmd, cwd, log_path):
    done = _run_console_module(["launch", cwd, log_path, *cmd])
    pid = done.stdout.strip() if done is not None and done.returncode == 0 else ""
    if pid.isdigit():
        return None, int(pid), CONSOLE_INPUT
    logger.error("hashcat will take no keypresses: running it without a console input buffer")
    proc = _popen(cmd, cwd, log_path, subprocess.DEVNULL)
    return proc, proc.pid, ""


def _run_console_module(args: list[str]) -> Optional[subprocess.CompletedProcess]:
    try:
        done = subprocess.run([sys.executable, "-m", _CONSOLE_MODULE, *args],
                              capture_output=True, text=True,
                              creationflags=subprocess.CREATE_NO_WINDOW,
                              timeout=_CONSOLE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        logger.warning(f"{_CONSOLE_MODULE} {args[0]} did not run", exc_info=True)
        return None
    if done.returncode:
        logger.warning(f"{_CONSOLE_MODULE} {args[0]} exited {done.returncode}: "
                       f"{done.stderr.strip()}")
    return done


def _write_console_input(pid: int, key: str) -> bool:
    done = _run_console_module(["keypress", str(pid), key])
    return done is not None and done.returncode == 0


def _write_named_pipe(pipe: Path, key: str) -> bool:
    try:
        stdin = os.open(pipe, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        logger.warning(f"Could not send {key!r} to hashcat: nothing is reading {pipe}")
        return False
    try:
        os.write(stdin, (key + "\n").encode())
    except OSError:
        logger.warning(f"Could not send {key!r} to hashcat through {pipe}", exc_info=True)
        return False
    finally:
        os.close(stdin)
    return True
