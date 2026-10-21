import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from wifit3.crack.hc22000_format import parse_hc22000
from wifit3.models import PersistedCapture, ToolCapability, ToolResult, ToolStatus
from wifit3.models.jobs import JobAction, JobState, ToolName
from wifit3.persist.config import Config
from wifit3.vault.tools.base import VaultTool
from wifit3.vault.tools.hashtopolis_api import (HashtopolisApi, HashtopolisUnreachable,
                                                ServerFile, user_api_endpoint)


logger = logging.getLogger(__name__)

POLL_SECONDS = 5.0
# hashcat 7.1.2 rejects FT-PSK (WPA*03/04) under 22000, and a failed benchmark deactivates the agent.
_UPLOADED_KINDS = ("01", "02")


@dataclass
class _ServerRun:
    api: HashtopolisApi
    wordlist: ServerFile
    hashlist_id: Optional[int] = None
    task_id: Optional[int] = None
    result: ToolResult = field(default_factory=lambda: ToolResult(
        status=ToolStatus.RUNNING, value="Uploading hashes...", actions=JobAction.STOP))
    stop_requested: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread = field(init=False)


def uploadable_hashlines(capture_path: str) -> list[str]:
    try:
        text = Path(capture_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = []
    for raw in text.splitlines():
        line = parse_hc22000(raw)
        if line is not None and line.kind in _UPLOADED_KINDS:
            lines.append(raw.strip())
    return list(dict.fromkeys(lines))


class HashtopolisTool(VaultTool):
    name = ToolName.HASHTOPOLIS
    label = "Hashtopolis"
    description = "Crack on a Hashtopolis server (WPA/WPA2/PMKID)"
    # ADOPTABLE: the task runs on the server, so a relaunched wifit3 goes back to polling it.
    capabilities = ToolCapability.ADOPTABLE

    def __init__(self) -> None:
        self._runs: Dict[str, _ServerRun] = {}

    def can_crack(self, capture: PersistedCapture) -> bool:
        return capture.path.endswith(".hc22000") and bool(uploadable_hashlines(capture.path))

    def auto_crack_config(self) -> Optional[dict[str, Any]]:
        settings = Config.hashtopolis
        if not (settings.url and settings.access_key
                and settings.wordlist_id and settings.access_group_id):
            return None
        return {"wordlist_id": settings.wordlist_id, "wordlist_name": settings.wordlist_name or "",
                "access_group_id": settings.access_group_id}

    def launch(self, job: JobState, capture: PersistedCapture) -> None:
        if not (Config.hashtopolis.url and Config.hashtopolis.access_key):
            raise ValueError("Hashtopolis server URL and access key are not set")
        hashlines = uploadable_hashlines(capture.path)
        if not hashlines:
            raise ValueError(f"No WPA*01/WPA*02 hashlines in {capture.path}")
        job.tool_state["server"] = _configured_server()
        run = self._new_run(job)
        name = f"wifit3 {capture.ssid or Path(capture.path).stem}"
        self._start(job, run, name, hashlines)

    def poll_status(self, job: JobState, assume_dead: bool = False) -> ToolResult:
        run = self._runs.get(job.job_id)
        if run is None:
            if not job.api_id:
                return ToolResult(status=ToolStatus.ERROR,
                                  value="wifit3 closed before the Hashtopolis task was created")
            started_on = job.tool_state.get("server")
            if started_on != _configured_server():
                return ToolResult(status=ToolStatus.ERROR,
                                  value=f"Hashtopolis URL changed since this job started on {started_on}")
            run = self._new_run(job)
            run.hashlist_id = job.tool_state.get("hashlist_id")
            run.task_id = int(job.api_id)
            run.result = ToolResult(status=ToolStatus.RUNNING, value="Reconnecting...",
                                    actions=JobAction.STOP)
            self._start(job, run, "", [])
        if run.task_id is not None:
            job.api_id = str(run.task_id)
            job.tool_state["hashlist_id"] = run.hashlist_id
        result = run.result
        if result.status != ToolStatus.RUNNING:
            del self._runs[job.job_id]
        return result

    def apply(self, action: JobAction, job: JobState) -> bool:
        run = self._runs.get(job.job_id)
        if action is not JobAction.STOP or run is None or not run.thread.is_alive():
            return False
        run.stop_requested.set()
        return True

    def _new_run(self, job: JobState) -> _ServerRun:
        wordlist = ServerFile(int(job.config["wordlist_id"]), str(job.config["wordlist_name"]))
        api = HashtopolisApi(str(Config.hashtopolis.url), str(Config.hashtopolis.access_key))
        return _ServerRun(api=api, wordlist=wordlist)

    def _start(self, job: JobState, run: _ServerRun, name: str, hashlines: list[str]) -> None:
        access_group_id = int(job.config.get("access_group_id") or 0)
        run.thread = threading.Thread(target=self._work,
                                      args=(run, name, hashlines, access_group_id),
                                      daemon=True, name=f"hashtopolis {job.job_id}")
        self._runs[job.job_id] = run
        run.thread.start()

    def _work(self, run: _ServerRun, name: str, hashlines: list[str],
              access_group_id: int) -> None:
        try:
            if run.task_id is None:
                cracker_version_id = run.api.newest_hashcat_version_id()
                run.hashlist_id = run.api.create_hashlist(name, hashlines, access_group_id)
                run.task_id = run.api.create_task(name, run.hashlist_id, run.wordlist,
                                                  cracker_version_id)
                logger.info(f"Hashtopolis task {run.task_id} on hashlist {run.hashlist_id}")
            while True:
                if run.stop_requested.is_set():
                    run.api.archive_task(run.task_id)
                    run.result = ToolResult(status=ToolStatus.STOPPED, value="Stopped by user")
                    return
                try:
                    run.result = self._task_result(run)
                except HashtopolisUnreachable as e:
                    run.result = ToolResult(status=ToolStatus.RUNNING, value=str(e),
                                            actions=JobAction.STOP)
                if run.result.status != ToolStatus.RUNNING:
                    return
                run.stop_requested.wait(POLL_SECONDS)
        except Exception as e:
            logger.exception("Hashtopolis job failed")
            run.result = ToolResult(status=ToolStatus.ERROR, value=str(e))

    @staticmethod
    def _task_result(run: _ServerRun) -> ToolResult:
        assert run.hashlist_id is not None and run.task_id is not None
        key = next(iter(run.api.cracked_keys(run.hashlist_id)), None)
        if key is not None:
            return ToolResult(status=ToolStatus.SUCCESS, value=f"Cracked! Key: {key}",
                              percent=100.0, key=key)
        progress = run.api.task_progress(run.task_id)
        if progress.is_complete:
            return ToolResult(status=ToolStatus.FAILURE, value=run.wordlist.filename, percent=100.0)
        if progress.keyspace == 0:
            return ToolResult(status=ToolStatus.RUNNING, value="Waiting for an agent",
                              detail=run.wordlist.filename, actions=JobAction.STOP)
        percent = min(progress.searched / progress.keyspace * 100, 100.0)
        return ToolResult(status=ToolStatus.RUNNING, value=f"Running ({percent:.2f}%)",
                          percent=percent, detail=run.wordlist.filename, actions=JobAction.STOP)


def _configured_server() -> Optional[str]:
    return user_api_endpoint(Config.hashtopolis.url) if Config.hashtopolis.url else None
