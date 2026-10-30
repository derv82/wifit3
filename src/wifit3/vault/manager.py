import logging
import json
import time
from collections import Counter
from concurrent.futures import Future
from pathlib import Path
from typing import Callable, Dict, List, Optional

from wifit3.models import CaptureType, PersistedCapture
from wifit3.models.jobs import JobAction, JobState, ToolCapability, ToolStatus, ToolResult
from wifit3.vault.tools.base import VaultTool, job_file, jobs_dir
from wifit3.vault.tools.cpu_crack import CpuCrackTool
from wifit3.vault.tools.hashcat import HashcatTool
from wifit3.vault.tools.hashtopolis import HashtopolisTool
from wifit3.crack import wpa_psk
from wifit3.crack.hc22000_format import read_hc22000
from wifit3.persist.config import Config


logger = logging.getLogger(__name__)

def _set_actions(job: JobState, offered: JobAction) -> None:
    clearable = JobAction.NONE if job.status == ToolStatus.RUNNING else JobAction.CLEAR
    job.actions = offered | clearable


def _copy_progress(job: JobState, res: ToolResult) -> None:
    job.percent = res.percent
    job.detail = res.detail
    job.key = res.key


def _log_propagation(propagation: Future[list[str]]) -> None:
    if (error := propagation.exception()) is not None:
        logger.error("Failed to pass a recovered key on to related APs", exc_info=error)
    elif matched := propagation.result():
        logger.info(f"Recovered key also verified for {len(matched)} related APs: {', '.join(matched)}")


class JobManager:
    def __init__(self, vault):
        self.vault = vault
        self.jobs: Dict[str, JobState] = {}
        self.tools: Dict[str, VaultTool] = {}
        self.register_tool(HashcatTool())
        self.register_tool(CpuCrackTool())
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
                self.jobs[k] = JobState.from_dict(v)
                _set_actions(self.jobs[k], JobAction.NONE)
            except Exception:
                logger.exception(f"Skipping unreadable job entry {k!r} in {jobs_file}")

    def _save(self) -> None:
        jobs_file = self.get_jobs_file()
        if not self.jobs:
            jobs_file.unlink(missing_ok=True)
            return
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
                if not (tool and (job.log_path or job.api_id)):
                    job.status = ToolStatus.ERROR
                    job.progress_msg = "Interrupted: wifit3 closed before the job started"
                    changed = True
                    continue
                detail_and_status = (job.detail, job.status)
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
                    self._persist_cracked_key(job, res, self._capture)
                    changed = True
                _set_actions(job, res.actions)
                self._notify_if_moved_on(job, detail_and_status, self._capture)
            except Exception as exc:
                logger.exception(f"Failed to reconcile job {job_id}")
                job.status = ToolStatus.ERROR
                job.progress_msg = f"Reconcile failed: {exc}"
                changed = True

        if changed:
            self._save()

    def submit_job(self, tool_name: str, capture: PersistedCapture, config: dict,
                   hashlines: List[str], auto_crack: bool = False) -> str:
        """Queue a job cracking ``hashlines`` of the AP's ``capture``; it launches on the next poll
        with a free slot."""
        display_name = f"{self.tools[tool_name].label} ({capture.ssid or Path(capture.path).stem})"
        job_id = self._new_job_id(tool_name, capture)
        hashlines_path = job_file(job_id, ".hc22000")
        hashlines_path.parent.mkdir(parents=True, exist_ok=True)
        hashlines_path.write_text("".join(f"{line}\n" for line in hashlines), encoding="utf-8",
                                  newline="\n")

        job = JobState(
            job_id=job_id,
            tool_name=tool_name,
            display_name=display_name,
            capture_path=capture.path,
            status=ToolStatus.QUEUED,
            progress_msg="Waiting in queue...",
            config=config,
            auto_crack=auto_crack,
            hashlines_path=str(hashlines_path),
        )
        self.jobs[job_id] = job
        logger.info(f"Submitted job {job}")
        self._save()
        return job_id

    def _new_job_id(self, tool_name: str, capture: PersistedCapture) -> str:
        """Unique among the jobs: a second submit in the same second gets a ``_2`` suffix."""
        base = f"{tool_name}_{Path(capture.path).stem}_{int(time.time())}"
        job_id, number = base, 1
        while job_id in self.jobs:
            number += 1
            job_id = f"{base}_{number}"
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
                    cap = self._job_capture(job, capture_for)
                    if not cap:
                        logger.error(f"No capture or hashlines for job ID {job_id}: {job.hashlines_path}")
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
                    detail_and_status = (job.detail, job.status)
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
                    self._notify_if_moved_on(job, detail_and_status, capture_for)
                except Exception as exc:
                    logger.exception(f"Error while polling status for job ID {job_id}")
                    job.status = ToolStatus.ERROR
                    job.progress_msg = f"Error polling status: {exc}"
                    changed = True

        if changed:
            self._save()

    def _notify_if_moved_on(self, job: JobState, detail_and_status: tuple[str, ToolStatus],
                            capture_for: Callable[[str], Optional[PersistedCapture]]) -> None:
        """A job that moved to its next wordlist or ended may have recorded the last one as run out,
        in its AP's .meta."""
        if (job.detail, job.status) != detail_and_status and (capture := self._job_capture(job, capture_for)):
            self.vault.notify_changed(capture.bssid)

    def _capture(self, path: str) -> Optional[PersistedCapture]:
        return next((c for c in self.vault.all_captures() if c.path == path), None)

    @staticmethod
    def _job_capture(job: JobState, capture_for: Callable[[str], Optional[PersistedCapture]]
                     ) -> Optional[PersistedCapture]:
        """The job's capture; once it's gone from the vault, one standing in for it, read from the
        job's own hashlines file."""
        capture = capture_for(job.capture_path)
        if capture is not None:
            return capture
        line = next((line for _, line in read_hc22000(job.hashlines_path)), None)
        if line is None:
            return None
        return PersistedCapture(type=CaptureType.PMKID if line.is_pmkid else CaptureType.HS, timestamp=0,
                                path=job.hashlines_path, bssid=line.bssid, ssid=line.ssid)

    def _persist_cracked_key(self, job: JobState, res: ToolResult,
                             capture_for: Callable[[str], Optional[PersistedCapture]]) -> None:
        """Save a SUCCESS result's key under each SSID whose line it cracks, and pass it on to
        related APs (in the background)."""
        if res.status != ToolStatus.SUCCESS or res.key is None:
            return
        cap = self._job_capture(job, capture_for)
        if not cap:
            logger.warning(f"Recovered key for job {job.job_id} not saved: no capture or hashlines")
            return
        try:
            for ssid in self._cracked_ssids(job, res.key, cap):
                self.vault.save_wpa_psk(ssid, cap.bssid, res.key)
                self.vault.propagate_verified_psk(cap.bssid, ssid, res.key).add_done_callback(_log_propagation)
        except OSError:
            logger.exception(f"Failed to save the recovered key for {job.capture_path}")

    @staticmethod
    def _cracked_ssids(job: JobState, key: str, cap: PersistedCapture) -> list[Optional[str]]:
        """SSIDs of the job's lines the key cracks; else the first line's SSID, then the capture's
        (hashcat's nonce-error-correction cracks lines ``psk_matches`` rejects)."""
        lines = [line for _, line in read_hc22000(job.hashlines_path)]
        cracked = list(dict.fromkeys(line.ssid for line in lines if wpa_psk.psk_matches(key, line)))
        return cracked or [next((line.ssid for line in lines if line.ssid), cap.ssid)]

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
        """Drop a finished job and its files in the jobs dir, and persist."""
        if self.jobs.pop(job_id, None) is None:
            return
        directory = jobs_dir()
        if directory.is_dir():
            for path in directory.iterdir():
                if path.name.startswith(f"{job_id}."):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        logger.warning(f"Could not remove {path}", exc_info=True)
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
