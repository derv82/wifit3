import logging
import os
import subprocess
import json
from pathlib import Path
from typing import Dict, Any, Optional

from wifit3.models import PersistedCapture, ToolCapability, ToolResult, ToolStatus
from wifit3.vault.tools.base import VaultTool


logger = logging.getLogger(__name__)

_STILL_ACTIVE = 259                             # GetExitCodeProcess: process has not exited
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class HashcatTool(VaultTool):
    name = "hashcat"
    description = "Advanced password recovery (WPA/WPA2/PMKID)"
    # ADOPTABLE: hashcat is left running when wifit3 exits and re-attached on the next launch, so a
    # long crack survives closing the app. KILLABLE is the user's explicit Kill, not shutdown.
    capabilities = ToolCapability.KILLABLE | ToolCapability.SINGLETON | ToolCapability.ADOPTABLE

    def __init__(self) -> None:
        self._procs: Dict[int, subprocess.Popen] = {}

    def can_crack(self, capture: PersistedCapture) -> bool:
        return capture.path.endswith(".hc22000")

    def launch(self, capture: PersistedCapture, config: Dict[str, Any]) -> Dict[str, Any]:
        hashcat_exe = config.get("hashcat_exe")
        wordlist = config.get("wordlist")
        if not hashcat_exe or not wordlist:
            logger.error(f"Missing hashcat_exe {hashcat_exe!r} or wordlist {wordlist!r} in config")
            raise ValueError("Missing hashcat_exe or wordlist in config")
        if not Path(hashcat_exe).is_file():
            raise ValueError(f"hashcat executable not found: {hashcat_exe}")
        if not Path(wordlist).is_file():
            raise ValueError(f"Wordlist not found: {wordlist}")

        hashcat_dir = str(Path(hashcat_exe).parent)
        abs_capture = str(Path(capture.path).resolve())
        abs_wordlist = str(Path(wordlist).resolve())
        potfile = str(Path(capture.path).with_suffix('.potfile').resolve())
        log_path = str(Path(capture.path).with_suffix('.hashcat.log').resolve())
        
        # Clear old potfile if it exists so we start fresh for this job
        if os.path.exists(potfile):
            try:
                logger.info(f"Removing old potfile {potfile}")
                os.remove(potfile)
            except OSError:
                logger.warning(f"Could not remove old potfile {potfile}", exc_info=True)

        cmd = [
            hashcat_exe,
            "-m", "22000",
            abs_capture,
            abs_wordlist,
            "--potfile-path", potfile,
            "--status",
            "--status-json",
            "--status-timer", "1",
        ]
        
        creationflags = 0
        if os.name == 'nt':
            creationflags = subprocess.CREATE_NO_WINDOW

        with open(log_path, 'w') as f:
            logger.info(f"Launching {' '.join(cmd)}, saving output to {log_path}")
            proc = subprocess.Popen(
                cmd,
                cwd=hashcat_dir,
                # Detach stdin: with a real console on stdin hashcat runs interactively (its
                # [s]/[p]/[q] prompt), reading the same keys Textual needs and freezing the UI.
                stdin=subprocess.DEVNULL,
                stdout=f,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                # setsid on POSIX so hashcat leaves wifit3's session and survives SIGHUP when the
                # terminal closes; no effect on Windows.
                start_new_session=True,
            )
        self._procs[proc.pid] = proc

        return {
            "pid": proc.pid,
            "log_path": log_path,
            "potfile": potfile,
            "hashcat_exe": hashcat_exe,
            "hashcat_dir": hashcat_dir,
            "capture_path": abs_capture
        }

    def poll_status(self, tracking_data: Dict[str, Any], assume_dead: bool = False) -> ToolResult:
        log_path = tracking_data.get("log_path")
        progress_msg = "Running..."
        if log_path and os.path.exists(log_path):
            progress_msg = self.parse_progress(log_path) or progress_msg

        pid = tracking_data.get("pid")
        capture_path = tracking_data.get("capture_path")
        potfile = str(Path(capture_path).resolve().with_suffix('.potfile')) if capture_path else None

        # The potfile is the source of truth for a recovered key. Check it FIRST, regardless of
        # liveness: hashcat sometimes hangs after printing its result, and blocking the poll until
        # the process exits froze the whole UI. If the key is there, reap the process and succeed.
        key = self._key_from_potfile(potfile)
        if key is not None:
            self._terminate(pid)
            return ToolResult(status=ToolStatus.SUCCESS, value=f"Cracked! Key: {key}", result_data={"key": key})

        exe = (tracking_data.get("config") or {}).get("hashcat_exe")
        if not assume_dead and self._is_running(pid, exe):
            return ToolResult(status=ToolStatus.RUNNING, value=progress_msg)

        self._terminate(pid)
        if "Exhausted" in progress_msg:
            wordlist = (tracking_data.get("config") or {}).get("wordlist")
            return ToolResult(status=ToolStatus.FAILURE, value=Path(wordlist).name if wordlist else "wordlist")
        reason = self._last_log_message(log_path)
        return ToolResult(status=ToolStatus.ERROR, value=reason or "hashcat exited without recovering the key")

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

    def _terminate(self, pid) -> None:
        proc = self._procs.pop(pid, None)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                logger.exception(f"Failed to terminate hashcat pid {pid}")

    def _last_log_message(self, log_path):
        """hashcat's last human-readable line (skips JSON status, prompts, banners), so a
        failed job reports hashcat's own error rather than a generic message."""
        if not log_path or not os.path.exists(log_path):
            return None
        skip = ("{", "[s]tatus", "Started:", "Stopped:", "hashcat (")
        candidate = None
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                for raw in f:
                    line = raw.strip()
                    if line and not line.startswith(skip):
                        candidate = line
        except OSError:
            return None
        if candidate and len(candidate) > 200:
            candidate = candidate[:197] + "..."
        return candidate

    def parse_progress(self, log_path):
        progress_msg = None
        try:
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
                for line in reversed(lines):
                    line = line.strip()
                    if line.startswith('{') and line.endswith('}'):
                        try:
                            status_data = json.loads(line)
                            progress = status_data.get('progress', [0, 1])
                            percent = (progress[0] / max(progress[1], 1)) * 100
                            status_code = status_data.get('status', 0)
                            if status_code == 2:
                                progress_msg = "Autotuning (%.2f%%)" % percent
                            elif status_code == 3:
                                progress_msg = "Running (%.2f%%)" % percent
                            elif status_code == 4:
                                progress_msg = "Paused (%.2f%%)" % percent
                            elif status_code == 5:
                                progress_msg = "Exhausted"
                            elif status_code == 6:
                                progress_msg = "Cracked! (100%)"
                            else:
                                progress_msg = f"Status {status_code} (%.2f%%)" % percent
                            break
                        except json.JSONDecodeError:
                            logger.warning(f"Could not parse hashcat status JSON: {line}", exc_info=True)
        except Exception:
            logger.exception(f"Error while parsing progress from {log_path}")
        return progress_msg

    def kill(self, tracking_data: Dict[str, Any]) -> None:
        pid = tracking_data.get("pid")
        if not pid:
            return
        proc = self._procs.pop(pid, None)
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                logger.exception(f"Failed to kill hashcat pid {pid}")
            return
        # A job adopted from a previous session (no handle we own). Only signal it if still alive
        # and still hashcat, so a reused PID is left alone. SIGTERM -> TerminateProcess on Windows.
        exe = (tracking_data.get("config") or {}).get("hashcat_exe")
        if not self._is_running(pid, exe):
            return
        try:
            import signal
            os.kill(pid, signal.SIGTERM)
        except OSError:
            logger.warning(f"Could not terminate hashcat pid {pid}", exc_info=True)
