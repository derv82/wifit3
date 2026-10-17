import logging
import json
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

from wifit3.models import PersistedCapture
from wifit3.models.jobs import JobAction, JobState, ToolCapability, ToolStatus, ToolResult
from wifit3.vault.tools.base import VaultTool
from wifit3.vault.tools.cpu_crack import CpuCrackTool
from wifit3.vault.tools.hashcat import HashcatTool
from wifit3.crack.hc22000_format import parse_hc22000
from wifit3.persist.config import Config


logger = logging.getLogger(__name__)


def _set_actions(job: JobState, offered: JobAction) -> None:
    clearable = JobAction.NONE if job.status == ToolStatus.RUNNING else JobAction.CLEAR
    job.actions = offered | clearable


def _copy_progress(job: JobState, res: ToolResult) -> None:
    job.percent = res.percent
    job.detail = res.detail
    job.key = res.key


class JobManager:
    def __init__(self, vault):
        self.vault = vault
        self.jobs: Dict[str, JobState] = {}
        self.tools: Dict[str, VaultTool] = {}
        self.register_tool(HashcatTool())
        self.register_tool(CpuCrackTool())
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
        except Exception:
            logger.exception(f"Error while loading {jobs_file}")
            self.jobs = {}  # Corrupt file: reset
            return
        for k, v in data.items():
            try:
                self.jobs[k] = JobState.from_dict(v)
                _set_actions(self.jobs[k], JobAction.NONE)
            except Exception:
                logger.exception(f"Skipping unreadable job entry {k!r} in {jobs_file}")

    def _save(self) -> None:
        jobs_file = self.get_jobs_file()
        data = {k: v.to_dict() for k, v in self.jobs.items()}
        logger.debug(f"Saving {len(data.keys())} jobs to {jobs_file}")
        jobs_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def reconcile_on_startup(self) -> None:
        """Resolve jobs left in a ``RUNNING`` state by a previous session."""
        changed = False
        for job_id, job in self.jobs.items():
            if job.status != ToolStatus.RUNNING:
                continue
            try:
                tool = self.tools.get(job.tool_name)
                if tool and ToolCapability.ADOPTABLE not in tool.capabilities:
                    job.status = ToolStatus.STOPPED
                    job.progress_msg = "Stopped when wifit3 closed."
                    changed = True
                    continue
                if not (tool and job.log_path):
                    job.status = ToolStatus.ERROR
                    job.progress_msg = "Process ended while wifit3 was closed."
                    changed = True
                    continue
                res = tool.poll_status(job)
                if res.status == ToolStatus.RUNNING:
                    logger.info(f"Adopted still-running {job.tool_name} job {job_id}")
                else:
                    logger.info(f"Reconciled {job.tool_name} job {job_id}: {res.status.value}")
                if res.status != job.status or (res.value and res.value != job.progress_msg):
                    job.status = res.status
                    if res.status == ToolStatus.RUNNING:
                        job.progress_msg = res.value or job.progress_msg
                    else:
                        job.progress_msg = res.value or "Process ended while wifit3 was closed."
                    _copy_progress(job, res)
                    self._persist_cracked_key(job, res)
                    changed = True
                _set_actions(job, res.actions)
            except Exception as exc:
                logger.exception(f"Failed to reconcile job {job_id}")
                job.status = ToolStatus.ERROR
                job.progress_msg = f"Reconcile failed: {exc}"
                changed = True

        if changed:
            self._save()

    def submit_job(self, tool_name: str, capture: PersistedCapture, config: dict,
                   automatic: bool = False) -> str:
        """Submit a job. It will be marked QUEUED and launched on the next poll if slots are free."""
        job_id = f"{tool_name}_{Path(capture.path).name}_{int(time.time())}"
        
        display_name = f"{self.tools[tool_name].label} ({capture.ssid or Path(capture.path).stem})"
        
        job = JobState(
            job_id=job_id,
            tool_name=tool_name,
            display_name=display_name,
            capture_path=capture.path,
            status=ToolStatus.QUEUED,
            progress_msg="Waiting in queue...",
            config=config,
            automatic=automatic,
        )
        self.jobs[job_id] = job
        logger.info(f"Submitted job {job}")
        self._save()
        return job_id

    def poll_jobs(self) -> None:
        """Called periodically by a UI timer."""
        changed = False
        captures_by_path = None

        def capture_for(path):
            nonlocal captures_by_path
            if captures_by_path is None:
                captures_by_path = {capture.path: capture for capture in self.vault.all_captures()}
            return captures_by_path.get(path)

        running_by_tool: Counter = Counter(
            j.tool_name for j in self.jobs.values() if j.status == ToolStatus.RUNNING)

        for job_id, job in list(self.jobs.items()):
            _set_actions(job, JobAction.NONE)       # a running job's own actions come from its poll
            tool = self.tools.get(job.tool_name)
            if not tool:
                logger.warning(f"Unable to find tool for job ID {job_id}: '{job.tool_name}' not found")
                continue

            if job.status == ToolStatus.QUEUED:
                if (ToolCapability.SINGLETON in tool.capabilities
                        and running_by_tool[job.tool_name] >= 1):
                    logger.warning(f"{job.tool_name} is already running; job ID {job_id} remains queued")
                    continue # Wait for slot

                # Slot available, launch it
                try:
                    cap = capture_for(job.capture_path)
                    if not cap:
                        logger.error(f"Unable to find capture for job ID {job_id}, path {job.capture_path} not found")
                        job.status = ToolStatus.ERROR
                        job.progress_msg = "Capture file deleted or missing."
                        changed = True
                        continue

                    tool.launch(job, cap)
                    job.status = ToolStatus.RUNNING
                    job.progress_msg = "Starting..."
                    _set_actions(job, JobAction.STOP)
                    changed = True
                    running_by_tool[job.tool_name] += 1
                except Exception as exc:
                    logger.exception(f"Failed to launch '{job.tool_name}' for job ID {job_id}")
                    job.status = ToolStatus.ERROR
                    job.progress_msg = f"Failed to launch: {exc}"
                    changed = True

            elif job.status == ToolStatus.RUNNING:
                try:
                    tracked = job.to_dict()
                    res = tool.poll_status(job)
                    if res.status != job.status or res.value != job.progress_msg:
                        logger.info(f"Changed status for job ID {job_id}: {res}")
                        job.status = res.status
                        if res.value is not None:
                            job.progress_msg = res.value
                        _copy_progress(job, res)
                        self._persist_cracked_key(job, res, capture_for)
                        changed = True
                    elif job.to_dict() != tracked:
                        changed = True      # the tool moved the job on (its next wordlist)
                    _set_actions(job, res.actions)
                except Exception as exc:
                    logger.exception(f"Error while polling status for job ID {job_id}")
                    job.status = ToolStatus.ERROR
                    job.progress_msg = f"Error polling status: {exc}"
                    changed = True

        if changed:
            self._save()

    def _persist_cracked_key(self, job: JobState, res: ToolResult, capture_for=None) -> None:
        """A SUCCESS result carrying a recovered key is written beside its capture as a
        vault artifact, then folded into the in-memory index."""
        if res.status != ToolStatus.SUCCESS or res.key is None:
            return
        cap = (capture_for(job.capture_path) if capture_for is not None else
               next((c for c in self.vault.all_captures() if c.path == job.capture_path), None))
        if not cap:
            return
        ssid = self._essid_from_capture(job.capture_path) or cap.ssid
        try:
            self.vault.save_wpa_psk(ssid, cap.bssid, res.key)
        except OSError:
            logger.exception(f"Failed to save the recovered key for {job.capture_path}")

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

    def stop_job(self, job_id: str) -> bool:
        """Stop a running job; it reports STOPPED once its process is gone."""
        return self._run_action(job_id, JobAction.STOP)

    def skip_wordlist(self, job_id: str) -> bool:
        """Move a running job on to the next item of its queue, abandoning the current one."""
        return self._run_action(job_id, JobAction.SKIP)

    def pause_job(self, job_id: str) -> bool:
        return self._run_action(job_id, JobAction.PAUSE)

    def resume_job(self, job_id: str) -> bool:
        return self._run_action(job_id, JobAction.RESUME)

    def _run_action(self, job_id: str, action: JobAction) -> bool:
        """Hand one of the actions the last poll offered to the owning tool."""
        job = self.jobs.get(job_id)
        if job is None or action not in job.actions:
            return False
        tool = self.tools.get(job.tool_name)
        if tool is None or not tool.apply(action, job):
            return False
        self._save()
        return True

    def clear_job(self, job_id: str) -> None:
        """Drop a finished job from the list and persist."""
        if self.jobs.pop(job_id, None) is not None:
            self._save()

    def kill_all_running(self) -> None:
        """Shutdown: leave ADOPTABLE tools running (re-attached next launch), kill other KILLABLE
        ones so they aren't orphaned."""
        killed = False
        for job in self.jobs.values():
            if job.status != ToolStatus.RUNNING:
                continue
            tool = self.tools.get(job.tool_name)
            if tool is None:
                continue
            caps = tool.capabilities
            if ToolCapability.ADOPTABLE in caps or ToolCapability.KILLABLE not in caps:
                continue
            try:
                killed = tool.apply(JobAction.STOP, job) or killed
            except Exception:
                logger.exception(f"Failed to kill job {job.job_id} on shutdown")
        if killed:
            self._save()

    def get_active_jobs(self) -> List[JobState]:
        # Return all jobs, sorted by most recently added.
        # This allows the UI to display finished jobs until the user clears them.
        return list(reversed(self.jobs.values()))
