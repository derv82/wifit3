import os
import json
import time
from pathlib import Path
from typing import Dict, List

from wifit3.models import PersistedCapture
from wifit3.models.jobs import JobState, ToolStatus
from wifit3.vault.tools.base import VaultTool
from wifit3.vault.tools.hashcat import HashcatTool
from wifit3.persist.config import Config


class JobManager:
    def __init__(self, vault):
        self.vault = vault
        self.jobs: Dict[str, JobState] = {}
        self.tools: Dict[str, VaultTool] = {}
        self.register_tool(HashcatTool())
        self._load()

    def register_tool(self, tool: VaultTool) -> None:
        self.tools[tool.name] = tool

    def get_jobs_file(self) -> Path:
        return Path(Config.captures_dir) / "jobs.json"

    def _load(self) -> None:
        jf = self.get_jobs_file()
        if not jf.exists():
            return
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
            for k, v in data.items():
                if 'status' in v:
                    v['status'] = ToolStatus(v['status'])
                self.jobs[k] = JobState(**v)
        except Exception:
            # If corrupted, reset
            self.jobs = {}
            
    def _save(self) -> None:
        jf = self.get_jobs_file()
        data = {}
        for k, v in self.jobs.items():
            d = v.__dict__.copy()
            d['status'] = d['status'].value
            data[k] = d
        jf.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def reconcile_on_startup(self) -> None:
        """Called once at startup to check if RUNNING jobs are actually dead."""
        changed = False
        for job_id, job in self.jobs.items():
            if job.status == ToolStatus.RUNNING:
                is_dead = True
                if job.pid is not None:
                    try:
                        os.kill(job.pid, 0)
                        is_dead = False
                    except OSError:
                        pass # Ignore permission errors etc, assume dead
                
                if is_dead:
                    # Tool died while we were closed. Try to parse its log for success/error.
                    tool = self.tools.get(job.tool_name)
                    if tool and job.log_path:
                        tracking = {
                            'pid': job.pid,
                            'log_path': job.log_path,
                            'api_id': job.api_id,
                            'capture_path': job.capture_path,
                            'config': job.config or {}
                        }
                        res = tool.poll_status(tracking)
                        job.status = res.status
                        job.progress_msg = res.value or "Process died unexpectedly."
                    else:
                        job.status = ToolStatus.ERROR
                        job.progress_msg = "Process died while Wifit was closed."
                    changed = True

        if changed:
            self._save()

    def submit_job(self, tool_name: str, capture: PersistedCapture, config: dict) -> str:
        """Submit a job. It will be marked QUEUED and launched on the next poll if slots are free."""
        job_id = f"{tool_name}_{Path(capture.path).name}_{int(time.time())}"
        job = JobState(
            job_id=job_id,
            tool_name=tool_name,
            capture_path=capture.path,
            status=ToolStatus.QUEUED,
            progress_msg="Waiting in queue...",
            config=config
        )
        self.jobs[job_id] = job
        self._save()
        return job_id

    def poll_jobs(self) -> None:
        """Called periodically by a UI timer."""
        changed = False
        
        running_local = sum(1 for j in self.jobs.values() if j.status == ToolStatus.RUNNING and j.tool_name == "hashcat")
        
        for job_id, job in list(self.jobs.items()):
            tool = self.tools.get(job.tool_name)
            if not tool:
                continue

            if job.status == ToolStatus.QUEUED:
                if job.tool_name == "hashcat" and running_local >= 1:
                    continue # Wait for slot
                    
                # Slot available, launch it
                try:
                    # Retrieve the actual capture object from the vault
                    cap = next((c for c in self.vault.all_captures() if c.path == job.capture_path), None)
                    if not cap:
                        job.status = ToolStatus.ERROR
                        job.progress_msg = "Capture file deleted or missing."
                        changed = True
                        continue

                    tracking = tool.launch(cap, job.config or {})
                    job.status = ToolStatus.RUNNING
                    job.progress_msg = "Starting..."
                    job.pid = tracking.get('pid')
                    job.log_path = tracking.get('log_path')
                    job.api_id = tracking.get('api_id')
                    changed = True
                    if job.tool_name == "hashcat":
                        running_local += 1
                except Exception as exc:
                    job.status = ToolStatus.ERROR
                    job.progress_msg = f"Failed to launch: {exc}"
                    changed = True

            elif job.status == ToolStatus.RUNNING:
                tracking = {
                    'pid': job.pid,
                    'log_path': job.log_path,
                    'api_id': job.api_id,
                    'capture_path': job.capture_path,
                    'config': job.config or {}
                }
                try:
                    res = tool.poll_status(tracking)
                    if res.status != job.status or res.value != job.progress_msg:
                        job.status = res.status
                        if res.value is not None:
                            job.progress_msg = res.value
                        changed = True
                except Exception as exc:
                    job.status = ToolStatus.ERROR
                    job.progress_msg = f"Error polling status: {exc}"
                    changed = True

        if changed:
            self._save()

    def get_active_jobs(self) -> List[JobState]:
        return [j for j in self.jobs.values() if j.status in (ToolStatus.QUEUED, ToolStatus.RUNNING)]
