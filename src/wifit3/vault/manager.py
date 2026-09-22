import logging
import json
import time
from pathlib import Path
from typing import Dict, List, Optional

from wifit3.models import PersistedCapture
from wifit3.models.jobs import JobState, ToolStatus, ToolResult
from wifit3.vault.tools.base import VaultTool
from wifit3.vault.tools.hashcat import HashcatTool
from wifit3.persist.common import parse_hc22000
from wifit3.persist.config import Config


logger = logging.getLogger(__name__)


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
        jobs_file = self.get_jobs_file()
        if not jobs_file.exists():
            return
        try:
            data = json.loads(jobs_file.read_text(encoding="utf-8"))
            for k, v in data.items():
                if 'status' in v:
                    v['status'] = ToolStatus(v['status'])
                self.jobs[k] = JobState(**v)
        except Exception:
            logger.exception(f"Error while loading {jobs_file}")
            self.jobs = {}  # If corrupted, reset

    def _save(self) -> None:
        jobs_file = self.get_jobs_file()
        data = {}
        for k, v in self.jobs.items():
            d = v.__dict__.copy()
            d['status'] = d['status'].value
            data[k] = d
        logger.info(f"Saving {len(data.keys())} jobs to {jobs_file}")
        jobs_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def reconcile_on_startup(self) -> None:
        """Resolve jobs left RUNNING by a previous session. Their process is no longer ours
        to track (it may have died, and its PID may since have been reused), so derive the
        final status from the tool's own log/output rather than from liveness."""
        changed = False
        for job_id, job in self.jobs.items():
            if job.status != ToolStatus.RUNNING:
                continue
            tool = self.tools.get(job.tool_name)
            if tool and job.log_path:
                tracking = {
                    'pid': job.pid,
                    'log_path': job.log_path,
                    'api_id': job.api_id,
                    'capture_path': job.capture_path,
                    'config': job.config or {},
                }
                logger.info(f"Reconciling {job.tool_name} job {job_id} from its log")
                res = tool.poll_status(tracking, assume_dead=True)
                job.status = res.status
                job.progress_msg = res.value or "Process ended while wifit3 was closed."
                self._persist_cracked_key(job, res)
            else:
                job.status = ToolStatus.ERROR
                job.progress_msg = "Process ended while wifit3 was closed."
            changed = True

        if changed:
            self._save()

    def submit_job(self, tool_name: str, capture: PersistedCapture, config: dict) -> str:
        """Submit a job. It will be marked QUEUED and launched on the next poll if slots are free."""
        job_id = f"{tool_name}_{Path(capture.path).name}_{int(time.time())}"
        
        display_name = f"{tool_name} ({capture.ssid or Path(capture.path).stem})"
        
        job = JobState(
            job_id=job_id,
            tool_name=tool_name,
            display_name=display_name,
            capture_path=capture.path,
            status=ToolStatus.QUEUED,
            progress_msg="Waiting in queue...",
            config=config
        )
        self.jobs[job_id] = job
        logger.info(f"Submitted job {job}")
        self._save()
        return job_id

    def poll_jobs(self) -> None:
        """Called periodically by a UI timer."""
        changed = False
        
        running_hashcat = sum(1 for j in self.jobs.values() if j.status == ToolStatus.RUNNING and j.tool_name == "hashcat")
        
        for job_id, job in list(self.jobs.items()):
            tool = self.tools.get(job.tool_name)
            if not tool:
                logger.warning(f"Unable to find tool for job ID {job_id}: '{job.tool_name}' not found")
                continue

            if job.status == ToolStatus.QUEUED:
                if job.tool_name == "hashcat" and running_hashcat >= 1:
                    logger.warning(f"Hashcat is already running; job ID {job_id} remains queued")
                    continue # Wait for slot
                    
                # Slot available, launch it
                try:
                    # Retrieve the actual capture object from the vault
                    cap = next((c for c in self.vault.all_captures() if c.path == job.capture_path), None)
                    if not cap:
                        logger.error(f"Unable to find capture for job ID {job_id}, path {job.capture_path} not found")
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
                        running_hashcat += 1
                except Exception as exc:
                    logger.exception(f"Failed to launch '{job.tool_name}' for job ID {job_id}")
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
                        logger.info(f"Changed status for job ID {job_id}: {res}")
                        job.status = res.status
                        if res.value is not None:
                            job.progress_msg = res.value
                        self._persist_cracked_key(job, res)
                        changed = True
                except Exception as exc:
                    logger.exception(f"Error while polling status for job ID {job_id}")
                    job.status = ToolStatus.ERROR
                    job.progress_msg = f"Error polling status: {exc}"
                    changed = True

        if changed:
            self._save()

    def _persist_cracked_key(self, job: JobState, res: ToolResult) -> None:
        """A SUCCESS result carrying a recovered key is written beside its capture as a
        vault artifact, then folded into the in-memory index."""
        if res.status != ToolStatus.SUCCESS or not res.result_data or "key" not in res.result_data:
            return
        cap = next((c for c in self.vault.all_captures() if c.path == job.capture_path), None)
        if not cap:
            return
        ssid = self._essid_from_capture(job.capture_path) or cap.ssid or "Unknown"
        ssid_safe = "".join(c if c.isalnum() else "_" for c in (cap.ssid or "Unknown"))
        bssid_safe = cap.bssid.replace(":", "-").lower() if cap.bssid else "00-00-00-00-00-00"
        out_name = f"{ssid_safe}_{bssid_safe}_{int(time.time())}_wpa_psk.txt"
        out_path = Path(Config.captures_dir) / out_name
        out_path.write_text(f"SSID: {ssid}\nBSSID: {cap.bssid}\nPSK: {res.result_data['key']}\n")
        self.vault.refresh()

    def _essid_from_capture(self, capture_path: str) -> Optional[str]:
        """The AP's real SSID decoded from the capture's hashline (lossless), unlike the
        sanitized name recovered from the filename."""
        try:
            text = Path(capture_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        for line in text.splitlines():
            parsed = parse_hc22000(line)
            if parsed and parsed.essid:
                try:
                    return bytes.fromhex(parsed.essid).decode("utf-8", errors="replace")
                except ValueError:
                    return None
        return None

    def clear_job(self, job_id: str) -> None:
        """Drop a finished job from the list and persist."""
        if self.jobs.pop(job_id, None) is not None:
            self._save()

    def kill_all_running(self) -> None:
        """Kill every RUNNING job's process. Called on shutdown so tools are not orphaned:
        an orphaned hashcat holds its single-instance lock and blocks all future launches."""
        for job in self.jobs.values():
            if job.status != ToolStatus.RUNNING:
                continue
            tool = self.tools.get(job.tool_name)
            if tool is None:
                continue
            try:
                tool.kill({'pid': job.pid, 'log_path': job.log_path, 'api_id': job.api_id})
            except Exception:
                logger.exception(f"Failed to kill job {job.job_id} on shutdown")

    def get_active_jobs(self) -> List[JobState]:
        # Return all jobs, sorted by most recently added.
        # This allows the UI to display finished jobs until the user clears them.
        return list(reversed(self.jobs.values()))
