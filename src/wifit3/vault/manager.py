import logging
import json
import os
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

from wifit3.models import PersistedCapture
from wifit3.models.jobs import JobState, ToolCapability, ToolStatus, ToolResult
from wifit3.vault.tools.base import ToolAlreadySubmitted, ToolTransientError, VaultTool
from wifit3.vault.tools.hashcat import HashcatTool
from wifit3.vault.tools.hashtopolis import HashtopolisTool
from wifit3.persist.common import parse_hc22000
from wifit3.persist.config import Config


logger = logging.getLogger(__name__)


_RETRY_BASE_SECONDS = 5.0
_RETRY_CAP_SECONDS = 60.0


class JobManager:
    def __init__(self, vault):
        self.vault = vault
        self.jobs: Dict[str, JobState] = {}
        self.tools: Dict[str, VaultTool] = {}
        self._seq = 0
        self._lock = threading.RLock()
        self._clock = time.monotonic
        self._retry_after: Dict[str, float] = {}
        self._retry_count: Dict[str, int] = {}
        self.register_tool(HashcatTool())
        self.register_tool(HashtopolisTool())
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
                if 'status' in v:
                    v['status'] = ToolStatus(v['status'])
                self.jobs[k] = JobState(**v)
            except Exception:
                logger.exception(f"Skipping unreadable job entry {k!r} in {jobs_file}")

    def _save(self) -> None:
        with self._lock:
            jobs_file = self.get_jobs_file()
            data = {}
            for k, v in self.jobs.items():
                d = v.__dict__.copy()
                d['status'] = d['status'].value
                data[k] = d
            logger.info(f"Saving {len(data.keys())} jobs to {jobs_file}")
            tmp = jobs_file.with_name(jobs_file.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, jobs_file)

    def _tracking_for(self, job: JobState) -> dict:
        tracking = dict(job.tracking or {})
        tracking.update({
            'pid': job.pid,
            'log_path': job.log_path,
            'api_id': job.api_id,
            'capture_path': job.capture_path,
            'config': job.config or {},
        })
        return tracking

    def reconcile_on_startup(self) -> None:
        """Resolve jobs left in a ``RUNNING`` state by a previous session."""
        changed = False
        for job_id, job in self.jobs.items():
            if job.status != ToolStatus.RUNNING:
                continue
            try:
                tool = self.tools.get(job.tool_name)
                # A synchronous upload persists its result in ``tracking`` before the next poll
                # confirms it; poll_status can resolve that from tracking even without a log file.
                if not (tool and (job.log_path or job.tracking)):
                    job.status = ToolStatus.ERROR
                    job.progress_msg = "Process ended while wifit3 was closed."
                    changed = True
                    continue
                res = tool.poll_status(self._tracking_for(job))
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
                    self._persist_cracked_key(job, res)
                    changed = True
            except Exception as exc:
                logger.exception(f"Failed to reconcile job {job_id}")
                job.status = ToolStatus.ERROR
                job.progress_msg = f"Reconcile failed: {exc}"
                changed = True

        # An auto-submitted upload that already succeeded in a past session is dropped here too,
        # not only on the live completion toast, so successful auto-jobs don't pile up on restart.
        for job_id in [jid for jid, j in self.jobs.items()
                       if j.status == ToolStatus.SUCCESS and (j.config or {}).get("auto_clear")]:
            del self.jobs[job_id]
            changed = True

        if changed:
            self._save()

    def submit_job(self, tool_name: str, capture: PersistedCapture, config: dict) -> str:
        """Submit a job. It will be marked QUEUED and launched on the next poll if slots are free."""
        display_name = f"{tool_name} ({capture.ssid or Path(capture.path).stem})"
        tool = self.tools.get(tool_name)
        prepare = getattr(tool, "prepare_submit_config", None)
        config = prepare(dict(config or {})) if prepare else dict(config or {})
        with self._lock:
            self._seq += 1
            job_id = f"{tool_name}_{Path(capture.path).name}_{int(time.time())}_{self._seq}"
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
            logger.info(f"Submitted job {job_id} ({tool_name})")
            self._save()
        return job_id

    def poll_jobs(self) -> None:
        """Called periodically by a UI timer."""
        with self._lock:
            running_by_tool: Counter = Counter(
                j.tool_name for j in self.jobs.values() if j.status == ToolStatus.RUNNING)
            queued = [(jid, j.tool_name, j.config, j.capture_path)
                      for jid, j in list(self.jobs.items()) if j.status == ToolStatus.QUEUED]
            running = [(jid, j.tool_name, self._tracking_for(j))
                       for jid, j in list(self.jobs.items()) if j.status == ToolStatus.RUNNING]
            captures = {c.path: c for c in self.vault.all_captures()}
        changed = False

        # Poll already-running jobs first so live progress (e.g. hashcat) refreshes every cycle,
        # ahead of any queued launch that may block on a slow or unreachable remote.
        for job_id, tool_name, tracking in running:
            tool = self.tools.get(tool_name)
            if tool is None:
                continue
            try:
                res = tool.poll_status(tracking)
            except Exception as exc:
                logger.exception(f"Error while polling status for job ID {job_id}")
                changed |= self._update(job_id, status=ToolStatus.ERROR,
                                        progress=f"Error polling status: {exc}")
                continue
            with self._lock:
                job = self.jobs.get(job_id)
                if job is None:
                    continue
                value_changed = res.value is not None and res.value != job.progress_msg
                if res.status != job.status or value_changed:
                    logger.info(f"Changed status for job ID {job_id}: {res.status.value}")
                    job.status = res.status
                    if value_changed:
                        job.progress_msg = res.value
                    changed = True
                if self._persist_cracked_key(job, res):
                    changed = True

        now = self._clock()
        for job_id, tool_name, config, capture_path in queued:
            tool = self.tools.get(tool_name)
            if tool is None:
                logger.warning(f"Unable to find tool for job ID {job_id}: '{tool_name}' not found")
                continue
            if ToolCapability.SINGLETON in tool.capabilities and running_by_tool[tool_name] >= 1:
                continue
            if self._retry_after.get(job_id, 0.0) > now:
                continue
            cap = captures.get(capture_path)
            if cap is None:
                logger.error(f"Capture for job ID {job_id} not found: {capture_path}")
                self._clear_retry(job_id)
                changed |= self._update(job_id, status=ToolStatus.ERROR,
                                        progress="Capture file deleted or missing.")
                continue
            try:
                tracking = tool.launch(cap, config or {})
            except ToolTransientError as exc:
                delay = self._defer_retry(job_id)
                logger.info(f"Deferring launch of job ID {job_id} for {delay:.0f}s: {exc}")
                changed |= self._update(job_id, progress=f"Waiting to submit: {exc}")
                continue
            except ToolAlreadySubmitted as exc:
                self._clear_retry(job_id)
                changed |= self._update(job_id, status=ToolStatus.SUCCESS, progress=str(exc))
                continue
            except Exception as exc:
                logger.exception(f"Failed to launch '{tool_name}' for job ID {job_id}")
                self._clear_retry(job_id)
                changed |= self._update(job_id, status=ToolStatus.ERROR,
                                        progress=f"Failed to launch: {exc}")
                continue
            self._clear_retry(job_id)
            with self._lock:
                job = self.jobs.get(job_id)
                if job is not None:
                    job.status = ToolStatus.RUNNING
                    job.progress_msg = "Starting..."
                    job.pid = tracking.get('pid')
                    job.log_path = tracking.get('log_path')
                    job.api_id = tracking.get('api_id')
                    job.tracking = tracking or None
                    changed = True
            running_by_tool[tool_name] += 1

        if changed:
            self._save()

    def _defer_retry(self, job_id: str) -> float:
        """Push a transiently-failed job's next attempt out with exponential backoff, so a dead
        server spaces retries instead of re-attempting every queued upload on every poll."""
        count = self._retry_count.get(job_id, 0) + 1
        self._retry_count[job_id] = count
        delay = min(_RETRY_CAP_SECONDS, _RETRY_BASE_SECONDS * (2 ** (count - 1)))
        self._retry_after[job_id] = self._clock() + delay
        return delay

    def _clear_retry(self, job_id: str) -> None:
        self._retry_after.pop(job_id, None)
        self._retry_count.pop(job_id, None)

    def _update(self, job_id: str, *, status: Optional[ToolStatus] = None,
                progress: Optional[str] = None) -> bool:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                return False
            changed = False
            if status is not None and job.status != status:
                job.status = status
                changed = True
            if progress is not None and job.progress_msg != progress:
                job.progress_msg = progress
                changed = True
            return changed

    def _persist_cracked_key(self, job: JobState, res: ToolResult) -> bool:
        """Write a SUCCESS result's recovered key beside its capture; a write failure is logged,
        never raised, so it cannot abort the poll cycle or lose the job's status update."""
        if res.status != ToolStatus.SUCCESS:
            return False
        key = (res.result_data or {}).get("key")
        if not key:
            return False
        try:
            cap = next((c for c in self.vault.all_captures() if c.path == job.capture_path), None)
            if cap is None or not cap.bssid:
                return False
            ssid = self._essid_from_capture(job.capture_path) or cap.ssid
            result = self.vault.save_wpa_psk(bssid=cap.bssid, ssid=ssid, psk=key)
            return bool(result and result.was_new)
        except Exception:
            logger.exception(f"Failed to persist recovered key for job {job.job_id}")
            return False

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
        with self._lock:
            self._clear_retry(job_id)
            if self.jobs.pop(job_id, None) is not None:
                self._save()

    def kill_job(self, job_id: str) -> None:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                return
            tool = self.tools.get(job.tool_name)
            if tool is None or ToolCapability.KILLABLE not in tool.capabilities:
                return
            tracking = {'pid': job.pid, 'log_path': job.log_path, 'api_id': job.api_id}
        try:
            tool.kill(tracking)
        except Exception:
            logger.exception(f"Failed to kill job {job_id}")

    def kill_all_running(self) -> None:
        """Shutdown: leave ADOPTABLE tools running (re-attached next launch), kill other KILLABLE
        ones so they aren't orphaned."""
        with self._lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            if job.status != ToolStatus.RUNNING:
                continue
            tool = self.tools.get(job.tool_name)
            if tool is None:
                continue
            caps = tool.capabilities
            if ToolCapability.ADOPTABLE in caps or ToolCapability.KILLABLE not in caps:
                continue
            try:
                tool.kill({'pid': job.pid, 'log_path': job.log_path, 'api_id': job.api_id})
            except Exception:
                logger.exception(f"Failed to kill job {job.job_id} on shutdown")

    def get_active_jobs(self) -> List[JobState]:
        # Return all jobs, sorted by most recently added.
        # This allows the UI to display finished jobs until the user clears them.
        with self._lock:
            return list(reversed(self.jobs.values()))
