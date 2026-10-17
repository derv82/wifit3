import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, Optional

from wifit3.crack import wpa_psk
from wifit3.crack.hc22000_format import Hc22000Line, parse_hc22000
from wifit3.crack.passwords import read_passwords, ssid_passwords, ssid_variants
from wifit3.dot11 import wpa
from wifit3.models import PersistedCapture, ToolCapability, ToolResult, ToolStatus
from wifit3.models.jobs import JobAction, JobState
from wifit3.vault.tools.cracker import CrackerTool, WordlistQueue, expand_wordlists


logger = logging.getLogger(__name__)


@dataclass
class _WordlistRun:
    wordlist: Path
    ssid_passwords: list[str]                   # tried before the wordlist's own passwords
    lines_by_essid: dict[bytes, list[Hc22000Line]]
    passwords_tried: int = 0
    passwords_total: int = 0
    key: Optional[str] = None
    exhausted: bool = False
    error: Optional[str] = None
    stop_requested: threading.Event = field(default_factory=threading.Event)
    unpaused: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread = field(init=False)

    def request_stop(self) -> None:
        self.stop_requested.set()
        self.unpaused.set()


def _lines_by_essid(capture_path: str) -> dict[bytes, list[Hc22000Line]]:
    by_essid: dict[bytes, list[Hc22000Line]] = {}
    for raw in Path(capture_path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = parse_hc22000(raw)
        if line is not None:
            by_essid.setdefault(bytes.fromhex(line.essid), []).append(line)
    return by_essid


class CpuCrackTool(CrackerTool[WordlistQueue]):
    name = "cpu_crack"
    label = "CPU Crack"
    description = "Built-in CPU cracker (WPA/WPA2/PMKID)"
    capabilities = ToolCapability.KILLABLE | ToolCapability.SINGLETON
    queue_type = WordlistQueue

    def __init__(self) -> None:
        self._runs: Dict[str, _WordlistRun] = {}

    def launch(self, job: JobState, capture: PersistedCapture) -> None:
        requested = job.config.get("wordlists") or []
        if not requested:
            raise ValueError("Missing wordlists in config")
        wordlists = expand_wordlists(requested)
        if not wordlists:
            raise ValueError(f"Wordlist not found: {', '.join(requested)}")
        self._start_wordlist(job, WordlistQueue(wordlists=wordlists))

    def _start_wordlist(self, job: JobState, queue: WordlistQueue) -> None:
        lines_by_essid = _lines_by_essid(job.capture_path)
        if not lines_by_essid:
            raise ValueError(f"No hashlines in {job.capture_path}")
        essid_passwords = [psk for essid in lines_by_essid
                           for psk in ssid_variants(essid.decode("utf-8", errors="replace"))]
        first_passwords = [] if queue.index else list(dict.fromkeys(
            essid_passwords + ssid_passwords(job.config.get("extra_passwords", []))))
        run = _WordlistRun(wordlist=Path(str(queue.current)), ssid_passwords=first_passwords,
                           lines_by_essid=lines_by_essid)
        run.unpaused.set()
        run.thread = threading.Thread(target=self._crack, args=(run,), daemon=True,
                                      name=f"cpu_crack {queue.label}")
        self._runs[job.job_id] = run
        queue.write_to(job)
        run.thread.start()
        logger.info(f"cpu_crack running for {queue.label}")

    def _crack(self, run: _WordlistRun) -> None:
        try:
            run.passwords_total = sum(1 for _ in self._passwords(run))
            for psk in self._passwords(run):
                run.unpaused.wait()
                if run.stop_requested.is_set():
                    return
                if self._psk_cracks(psk, run.lines_by_essid):
                    run.key = psk
                    return
                run.passwords_tried += 1
            run.exhausted = True
        except Exception as exc:
            logger.exception(f"cpu_crack failed on {run.wordlist}")
            run.error = str(exc)

    @staticmethod
    def _passwords(run: _WordlistRun) -> Iterator[str]:
        yield from run.ssid_passwords
        already_tried = set(run.ssid_passwords)
        yield from (psk for psk in read_passwords(run.wordlist) if psk not in already_tried)

    @staticmethod
    def _psk_cracks(psk: str, lines_by_essid: dict[bytes, list[Hc22000Line]]) -> bool:
        for essid, lines in lines_by_essid.items():
            pmk = wpa.derive_pmk(psk, essid)
            if any(wpa_psk.psk_matches(pmk, line) for line in lines):
                return True
        return False

    def poll_status(self, job: JobState, assume_dead: bool = False) -> ToolResult:
        queue = WordlistQueue.from_job(job)
        run = self._runs.get(job.job_id)
        if run is not None and run.key is not None:
            del self._runs[job.job_id]
            return self._cracked(run.key)
        if run is not None and not assume_dead and run.thread.is_alive():
            percent = run.passwords_tried / max(run.passwords_total, 1) * 100
            state = "Running" if run.unpaused.is_set() else "Paused"
            return self._running(self._progress_msg(state, percent), percent, queue)

        self._runs.pop(job.job_id, None)
        ended = self._wordlist_ended(job, queue, exhausted=run is not None and run.exhausted)
        if ended is not None:
            return ended
        reason = run.error if run is not None else None
        return ToolResult(status=ToolStatus.ERROR, value=reason or "cpu_crack ended without recovering the key")

    def _live_run(self, job: JobState) -> Optional[_WordlistRun]:
        run = self._runs.get(job.job_id)
        return run if run is not None and run.thread.is_alive() else None

    def _request_stop(self, job: JobState, queue: WordlistQueue) -> bool:
        run = self._live_run(job)
        if run is None:
            return False
        run.request_stop()
        return True

    def _request_pause(self, job: JobState, queue: WordlistQueue) -> bool:
        run = self._live_run(job)
        if run is None:
            return False
        run.unpaused.clear()
        return True

    def _request_resume(self, job: JobState, queue: WordlistQueue) -> bool:
        run = self._live_run(job)
        if run is None:
            return False
        run.unpaused.set()
        return True

    def _kill(self, job: JobState) -> None:
        run = self._runs.get(job.job_id)
        if run is not None:
            run.request_stop()
            run.thread.join()

    def _running_actions(self, queue: WordlistQueue, progress_msg: str) -> JobAction:
        if queue.stopped or queue.skipping_wordlist:
            return JobAction.STOP
        return self._queue_actions(queue) | self._pause_action(progress_msg)
