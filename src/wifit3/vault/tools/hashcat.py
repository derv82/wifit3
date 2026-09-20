import os
import subprocess
import json
from pathlib import Path
from typing import Dict, Any

from wifit3.models import PersistedCapture, ToolCapability, ToolResult, ToolStatus
from wifit3.vault.tools.base import VaultTool


class HashcatTool(VaultTool):
    name = "hashcat"
    description = "Advanced password recovery (WPA/WPA2/PMKID)"
    capabilities = ToolCapability.KILLABLE

    def can_crack(self, capture: PersistedCapture) -> bool:
        return capture.path.endswith(".hc22000")

    def launch(self, capture: PersistedCapture, config: Dict[str, Any]) -> Dict[str, Any]:
        hashcat_exe = config.get("hashcat_exe")
        wordlist = config.get("wordlist")
        if not hashcat_exe or not wordlist:
            raise ValueError("Missing hashcat_exe or wordlist in config")

        hashcat_dir = str(Path(hashcat_exe).parent)
        potfile = str(Path(capture.path).with_suffix('.potfile'))
        log_path = str(Path(capture.path).with_suffix('.hashcat.log'))
        
        # Clear old potfile if it exists so we start fresh for this job
        if os.path.exists(potfile):
            try:
                os.remove(potfile)
            except OSError:
                pass

        cmd = [
            hashcat_exe,
            "-m", "22000",
            capture.path,
            wordlist,
            "--potfile-path", potfile,
            "--status",
            "--status-json",
            "--status-timer", "1",
        ]
        
        creationflags = 0
        if os.name == 'nt':
            creationflags = subprocess.CREATE_NO_WINDOW

        with open(log_path, 'w') as f:
            proc = subprocess.Popen(
                cmd,
                cwd=hashcat_dir,
                stdout=f,
                stderr=subprocess.STDOUT,
                creationflags=creationflags
            )

        return {
            "pid": proc.pid,
            "log_path": log_path,
            "potfile": potfile,
            "hashcat_exe": hashcat_exe,
            "hashcat_dir": hashcat_dir,
            "capture_path": capture.path
        }

    def poll_status(self, tracking_data: Dict[str, Any]) -> ToolResult:
        log_path = tracking_data.get("log_path")
        progress_msg = "Running..."
        
        # 1. Read the latest status line from the log file
        if log_path and os.path.exists(log_path):
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
                                pass
            except Exception:
                pass

        # 2. Check if process is still running
        pid = tracking_data.get("pid")
        is_running = False
        if pid:
            try:
                # os.kill with 0 does not kill the process, it just checks for existence/permissions
                os.kill(pid, 0)
                is_running = True
            except OSError:
                is_running = False

        if is_running:
            return ToolResult(status=ToolStatus.RUNNING, value=progress_msg)
        
        # 3. Process is dead. Verify output via hashcat --show
        config = tracking_data.get("config", {})
        hashcat_exe = config.get("hashcat_exe")
        hashcat_dir = str(Path(hashcat_exe).parent) if hashcat_exe else None
        capture_path = tracking_data.get("capture_path")
        potfile = str(Path(capture_path).with_suffix('.potfile')) if capture_path else None

        if hashcat_exe and capture_path and potfile and os.path.exists(potfile):
            cmd = [
                hashcat_exe,
                "-m", "22000",
                capture_path,
                "--potfile-path", potfile,
                "--show"
            ]
            try:
                creationflags = 0
                if os.name == 'nt':
                    creationflags = subprocess.CREATE_NO_WINDOW
                    
                output = subprocess.check_output(cmd, cwd=hashcat_dir, creationflags=creationflags, text=True, errors='replace')
                cracked = [line for line in output.splitlines() if line.strip()]
                if cracked:
                    # e.g. d6c9b3a9b8c448b4ab57d81c71884aac:1cb72c380a80:02cb0e3c3073:ASUS:0xdeadbeef
                    parts = cracked[0].split(':')
                    pwd = parts[-1] if parts else "<unknown>"
                    return ToolResult(status=ToolStatus.SUCCESS, value=f"Cracked! Key: {pwd}")
            except Exception:
                pass
                
        # If process exited and no cracked output was found, it failed or exhausted
        if "Cracked" in progress_msg:
            return ToolResult(status=ToolStatus.SUCCESS, value=progress_msg)
        elif "Exhausted" in progress_msg:
            return ToolResult(status=ToolStatus.ERROR, value="Wordlist exhausted")
            
        return ToolResult(status=ToolStatus.ERROR, value="Failed or crashed")

    def kill(self, tracking_data: Dict[str, Any]) -> None:
        pid = tracking_data.get("pid")
        if pid:
            try:
                import signal
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
