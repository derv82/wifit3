import logging
import os
import shlex
import signal
import subprocess
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from wifit3.models import PersistedCapture, ToolCapability, ToolResult, ToolStatus
from wifit3.models.jobs import JobAction, JobState, ToolName
from wifit3.persist.config import Config
from wifit3.vault.tools import hashcat_keypress
from wifit3.vault.tools.cracker import CrackerTool, WordlistQueue, expand_wordlists


logger = logging.getLogger(__name__)

_STILL_ACTIVE = 259                             # GetExitCodeProcess: process has not exited
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_LOG_TAIL_BYTES = 256 * 1024
_EXIT_WAIT_SECONDS = 5.0
_TERMINATE_WAIT_SECONDS = 0.5                   # no SIGTERM handler in hashcat: it exits at once
_QUIT_GRACE_SECONDS = 10.0                      # how long 'q' has to work before we kill instead
_STATUS_NAMES = {2: "Autotuning", 3: "Running", 4: "Paused"}   # hashcat --status-json codes


def _read_log_tail(path, limit: int = _LOG_TAIL_BYTES, start: int = 0) -> list[str]:
    """Up to ``limit`` bytes of whole lines from the end of the log."""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            begin = max(start, size - limit)
            if begin > start:
                f.seek(begin - 1)
                starts_midline = f.read(1) != b"\n"
            else:
                starts_midline = False
            f.seek(begin)
            data = f.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if starts_midline and lines:
        lines = lines[1:]
    return lines


def _split_args(raw: str) -> list[str]:
    # posix=False to retain Windows backslashes. Strip unnecessary quotes.
    return [token.strip('"') for token in shlex.split(raw, posix=False)]


@dataclass
class HashcatQueue(WordlistQueue):
    log_offset: int = 0         # bytes already in the job's log when the current process started
    stdin_path: str = ""            # hashcat's stdin; see hashcat_keypress.send
    quit_sent_at: float = 0.0       # time.time() of the 'q' keypress


class HashcatTool(CrackerTool[HashcatQueue]):
    name = ToolName.HASHCAT
    label = "Hashcat"
    description = "Advanced password recovery (WPA/WPA2/PMKID)"
    # ADOPTABLE: hashcat is left running when wifit3 exits and re-attached on the next launch, so a
    # long crack survives closing the app. KILLABLE is the user's explicit Stop, not shutdown.
    capabilities = ToolCapability.KILLABLE | ToolCapability.SINGLETON | ToolCapability.ADOPTABLE
    queue_type = HashcatQueue

    def __init__(self) -> None:
        self._procs: Dict[int, subprocess.Popen] = {}

    def auto_crack_config(self) -> Optional[dict[str, Any]]:
        if not (Config.hashcat.path and Config.hashcat.wordlists):
            return None
        return {"hashcat_exe": Config.hashcat.path, "wordlists": list(Config.hashcat.wordlists),
                "hashcat_args": Config.hashcat.args or ""}

    def launch(self, job: JobState, capture: PersistedCapture) -> None:
        hashcat_exe = job.config.get("hashcat_exe")
        requested = job.config.get("wordlists") or []
        if not hashcat_exe or not requested:
            logger.error(f"Missing hashcat_exe {hashcat_exe!r} or wordlists {requested!r} in config")
            raise ValueError("Missing hashcat_exe or wordlists in config")
        if not Path(hashcat_exe).is_file():
            raise ValueError(f"hashcat executable not found: {hashcat_exe}")
        wordlists = expand_wordlists(requested)
        if not wordlists:
            raise ValueError(f"Wordlist not found: {', '.join(requested)}")

        potfile = self._potfile(job.capture_path)
        if potfile and os.path.exists(potfile):
            try:
                logger.info(f"Removing old potfile {potfile}")
                os.remove(potfile)
            except OSError:
                logger.warning(f"Could not remove old potfile {potfile}", exc_info=True)

        job.log_path = str(Path(job.capture_path).with_suffix('.hashcat.log').resolve())
        Path(job.log_path).write_bytes(b"")      # one log per job, every wordlist appended to it
        self._start_wordlist(job, HashcatQueue(wordlists=wordlists))

    def _start_wordlist(self, job: JobState, queue: HashcatQueue) -> None:
        """Run hashcat against the queue's current wordlist, appending to the job's log."""
        hashcat_exe = str(job.config.get("hashcat_exe"))
        self._drop_dead_pidfile(hashcat_exe)
        cmd = [
            hashcat_exe,
            "-m", "22000",
            "--potfile-path", str(self._potfile(job.capture_path)),
            "--status",
            "--status-json",
            "--status-timer", "1",
            *_split_args(job.config.get("hashcat_args") or ""),
            str(Path(job.capture_path).resolve()),
            str(queue.current),
        ]

        with open(job.log_path, 'ab') as f:
            f.write(f"\n=== wordlist: {queue.current} ===\n".encode("utf-8"))
            f.flush()
            queue.log_offset = f.tell()
        logger.info(f"Launching {' '.join(cmd)}, saving output to {job.log_path}")
        proc, pid, stdin_path = hashcat_keypress.spawn(
            cmd, cwd=str(Path(hashcat_exe).parent), log_path=job.log_path)

        if proc is not None:
            self._procs[pid] = proc
        job.pid = pid
        queue.stdin_path = stdin_path
        queue.quit_sent_at = 0.0
        queue.write_to(job)
        logger.info(f"hashcat running on pid {pid} for {queue.label}")

    def _drop_dead_pidfile(self, hashcat_exe: str) -> None:
        """hashcat refuses to start while the pid in its session pidfile is alive, and a run we
        killed never gets to remove that file."""
        candidates = (Path(hashcat_exe).parent / "hashcat.pid",
                      Path.home() / ".hashcat" / "sessions" / "hashcat.pid",
                      Path.home() / ".local" / "share" / "hashcat" / "sessions" / "hashcat.pid")
        for pidfile in candidates:
            try:
                recorded = int.from_bytes(pidfile.read_bytes()[:4], "little")
            except OSError:
                continue
            if recorded and not self._is_running(recorded, hashcat_exe):
                try:
                    pidfile.unlink()
                except OSError:
                    logger.warning(f"Could not remove {pidfile}", exc_info=True)
                else:
                    logger.info(f"Removed {pidfile}: hashcat pid {recorded} is gone")

    def poll_status(self, job: JobState, assume_dead: bool = False) -> ToolResult:
        self._remove_exited_processes()
        queue = HashcatQueue.from_job(job)
        lines = _read_log_tail(job.log_path, start=queue.log_offset)
        progress_msg, percent = self._parse_progress(lines) or ("Running...", None)

        # The potfile is the source of truth for a recovered key. Check it FIRST, regardless of
        # liveness: hashcat sometimes hangs after printing its result, and blocking the poll until
        # the process exits froze the whole UI. If the key is there, end the process and succeed.
        key = self._key_from_potfile(self._potfile(job.capture_path))
        if key is not None:
            self._terminate(job.pid)
            return self._cracked(key)

        if not assume_dead and self._is_running(job.pid, job.config.get("hashcat_exe")):
            self._kill_if_quit_timed_out(job, queue)
            return self._running(progress_msg, percent, queue)

        self._terminate(job.pid)
        ended = self._wordlist_ended(job, queue, exhausted="Exhausted" in progress_msg)
        if ended is not None:
            return ended
        reason = self._last_log_message(lines)
        return ToolResult(status=ToolStatus.ERROR, value=reason or "hashcat exited without recovering the key")

    def _request_pause(self, job: JobState, queue: HashcatQueue) -> bool:
        return hashcat_keypress.send("p", pid=job.pid, stdin_path=queue.stdin_path)

    def _request_resume(self, job: JobState, queue: HashcatQueue) -> bool:
        return hashcat_keypress.send("r", pid=job.pid, stdin_path=queue.stdin_path)

    def _request_stop(self, job: JobState, queue: HashcatQueue) -> bool:
        if not hashcat_keypress.send("q", pid=job.pid, stdin_path=queue.stdin_path):
            return False
        queue.quit_sent_at = time.time()
        queue.write_to(job)
        logger.info(f"Sent 'q' to hashcat pid {job.pid}")
        return True

    def _kill_if_quit_timed_out(self, job: JobState, queue: HashcatQueue) -> None:
        if not queue.quit_sent_at or time.time() - queue.quit_sent_at < _QUIT_GRACE_SECONDS:
            return
        logger.warning(f"hashcat pid {job.pid} is still running {_QUIT_GRACE_SECONDS:.0f}s "
                       f"after 'q'; killing it")
        self._kill(job)
        queue.quit_sent_at = 0.0
        queue.write_to(job)

    def _running_actions(self, queue: HashcatQueue, progress_msg: str) -> JobAction:
        if queue.quit_sent_at:
            return JobAction.STOP
        actions = self._queue_actions(queue)
        if queue.stdin_path:
            actions |= self._pause_action(progress_msg)
        return actions

    @staticmethod
    def _potfile(capture_path: str) -> Optional[str]:
        return str(Path(capture_path).resolve().with_suffix('.potfile')) if capture_path else None

    def _key_from_potfile(self, potfile):
        """The recovered passphrase from a per-capture potfile, or None. A line is
        ``<hash>[*<essid>]:<password>``, so the passphrase is everything after the first colon."""
        if not potfile or not os.path.exists(potfile):
            return None
        try:
            lines = Path(potfile).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            line = line.strip()
            if ":" in line:
                return line.split(":", 1)[1]
        return None

    def _is_running(self, pid, exe: Optional[str] = None) -> bool:
        """Whether the job's process is alive: via our Popen handle if we launched it, else via the
        OS, and (when ``exe`` is given) only if the live PID is still that program."""
        proc = self._procs.get(pid)
        if proc is not None:
            return proc.poll() is None
        if not self._pid_alive(pid):
            return False
        if exe:
            running_exe = self._pid_exe_path(pid)
            # stem-in-name (not exact) so a symlinked/versioned hashcat -> hashcat.bin still matches
            stem = Path(exe).stem.lower()
            if running_exe and stem and stem not in Path(running_exe).name.lower():
                return False
        return True

    def _pid_alive(self, pid) -> bool:
        """True if a process with this PID currently exists. Avoids os.kill(pid, 0) on Windows,
        where signal 0 routes to TerminateProcess."""
        if not pid:
            return False
        if os.name != "nt":
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True                       # exists, owned by another user
            return True
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)

    def _pid_exe_path(self, pid) -> Optional[str]:
        """Path to a running PID's executable (Windows API / Linux /proc), or None if unreadable."""
        if not pid:
            return None
        if os.name != "nt":
            try:
                return os.readlink(f"/proc/{int(pid)}/exe")   # Linux; OSError elsewhere/gone
            except OSError:
                return None
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
            return None
        finally:
            k32.CloseHandle(handle)

    def _remove_exited_processes(self) -> None:
        # poll() also releases an exited child: unpolled it stays defunct until wifit3 exits
        for pid, proc in list(self._procs.items()):
            if proc.poll() is not None:
                del self._procs[pid]

    def _terminate(self, pid) -> None:
        proc = self._procs.get(pid)
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=_TERMINATE_WAIT_SECONDS)
            except Exception:
                logger.warning(f"hashcat pid {pid} did not exit when terminated", exc_info=True)
                return                  # still tracked, so a later poll removes it
        del self._procs[pid]

    def _kill(self, job: JobState) -> None:
        """Kill the job's process and wait for it to exit: hashcat refuses to start while
        another instance of the same executable is running."""
        pid = job.pid
        if not pid:
            return
        proc = self._procs.get(pid)
        if proc is not None:
            if proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=_EXIT_WAIT_SECONDS)
                    logger.info(f"Killed hashcat pid {pid}")
                except Exception:
                    logger.exception(f"Failed to kill hashcat pid {pid}")
                    return                  # still tracked, so a later poll removes it
            del self._procs[pid]
            return
        if not self._is_running(pid, job.config.get("hashcat_exe")):
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            logger.warning(f"Could not terminate hashcat pid {pid}", exc_info=True)
            return
        deadline = time.monotonic() + _EXIT_WAIT_SECONDS
        while self._pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)

    def _last_log_message(self, lines: list[str]) -> Optional[str]:
        """hashcat's last human-readable line (skips JSON status, prompts, banners), so a
        failed job reports hashcat's own error rather than a generic message."""
        skip = ("{", "[s]tatus", "Started:", "Stopped:", "hashcat (", "=== wordlist")
        candidate = None
        for raw in lines:
            line = raw.strip()
            if line and not line.startswith(skip):
                candidate = line
        if candidate and len(candidate) > 200:
            candidate = candidate[:197] + "..."
        return candidate

    def _parse_progress(self, lines: list[str]) -> Optional[tuple[str, float]]:
        """hashcat's status and percent done from the newest ``--status-json`` line in the log."""
        for line in reversed(lines):
            line = line.strip()
            if line.startswith('{') and line.endswith('}'):
                try:
                    status_data = json.loads(line)
                    progress = status_data.get('progress', [0, 1])
                    percent = (progress[0] / max(progress[1], 1)) * 100
                    status_code = status_data.get('status', 0)
                    if status_code == 5:
                        progress_msg = "Exhausted"
                    elif status_code == 6:
                        progress_msg = "Cracked! (100%)"
                    else:
                        state = _STATUS_NAMES.get(status_code, f"Status {status_code}")
                        progress_msg = self._progress_msg(state, percent)
                    return progress_msg, percent
                except json.JSONDecodeError:
                    logger.warning(f"Could not parse hashcat status JSON: {line}", exc_info=True)
        return None
