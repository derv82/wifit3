import logging
import os
import subprocess
import json
from pathlib import Path
from typing import Dict, Any

from wifit3.models import PersistedCapture, ToolCapability, ToolResult, ToolStatus
from wifit3.vault.tools.base import VaultTool


logger = logging.getLogger(__name__)


class HashcatTool(VaultTool):
    name = "hashcat"
    description = "Advanced password recovery (WPA/WPA2/PMKID)"
    capabilities = ToolCapability.KILLABLE

    def __init__(self) -> None:
        self._procs: Dict[int, subprocess.Popen] = {}

    def can_crack(self, capture: PersistedCapture) -> bool:
        return capture.path.endswith(".hc22000")

    def launch(self, capture: PersistedCapture, config: Dict[str, Any]) -> Dict[str, Any]:
        hashcat_exe = config.get("hashcat_exe")
        wordlist = config.get("wordlist")
        if not hashcat_exe:
            logger.error(f"Missing hashcat {hashcat_exe} or wordlist {wordlist} in config")
            raise ValueError("Missing hashcat_exe or wordlist in config")

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
                stdout=f,
                stderr=subprocess.STDOUT,
                creationflags=creationflags
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

        # Liveness via our own Popen handle, never os.kill(pid, 0): on Windows the latter fires a
        # console Ctrl+C that can wedge both hashcat and this app's terminal.
        if not assume_dead and self._is_running(pid):
            return ToolResult(status=ToolStatus.RUNNING, value=progress_msg)

        # Exited without a key: exhausted is a clean miss; anything else surfaces hashcat's message.
        if "Exhausted" in progress_msg:
            return ToolResult(status=ToolStatus.FAILURE, value="Exhausted: passphrase not in wordlist")
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

    def _is_running(self, pid) -> bool:
        proc = self._procs.get(pid)
        return proc is not None and proc.poll() is None

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
        # No handle we own (e.g. a job from a previous session). os.kill with SIGTERM maps to
        # TerminateProcess on Windows (safe: not a console Ctrl+C event).
        try:
            import signal
            os.kill(pid, signal.SIGTERM)
        except OSError:
            logger.warning(f"Could not terminate hashcat pid {pid}", exc_info=True)
