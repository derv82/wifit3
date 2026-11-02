"""With the vault open on Uncracked-500, run a CPU Crack job on it that exhausts its wordlist, then one that
cracks it; print the time from the job poll that ended each until the item pane has painted the result."""
import asyncio
import time
from pathlib import Path

from make_vault_fixture import UNCRACKED_PSK
from vault_bench import bssid_of, open_vault, painted, select, shown_state, terminal_size, wait_for
from wifit3.models import ToolStatus
from wifit3.models.jobs import ToolName
from wifit3.persist.ap_state import ApState
from wifit3.persist.exhaustion import wordlist_identity
from wifit3.ui.app import WifiteApp

UNFINISHED = (ToolStatus.QUEUED, ToolStatus.RUNNING)


class JobPollTimingApp(WifiteApp):
    """WifiteApp noting when the job poll that ended each job started."""

    def __init__(self) -> None:
        super().__init__(cli_log_level="unset")
        self.ending_poll_started: dict[str, float] = {}

    def _poll_jobs(self) -> None:
        started = time.perf_counter()
        unfinished = [job for job in self.vault.manager.jobs.values() if job.status in UNFINISHED]
        super()._poll_jobs()
        for job in unfinished:
            if job.status not in UNFINISHED:
                self.ending_poll_started[job.job_id] = started


def shows_exhausted(state: ApState, identity: str) -> bool:
    return any(wordlist.identity == identity for wordlists in state.exhausted_wordlists.values()
               for wordlist in wordlists)


def shows_key(state: ApState, psk: str) -> bool:
    return any(key.value == psk for key in state.keys)


async def main() -> None:
    wrong_passwords = Path("wrong-passwords.txt")
    wrong_passwords.write_text("".join(f"wrong-password-{n}\n" for n in range(5)), encoding="utf-8")
    right_password = Path("right-password.txt")
    right_password.write_text(f"{UNCRACKED_PSK}\n", encoding="utf-8")
    app = JobPollTimingApp()
    async with app.run_test(size=terminal_size()) as pilot:
        await pilot.pause()
        drawer, _ = await open_vault(app)
        bssid = bssid_of(app, "Uncracked-500")
        await select(drawer, bssid)
        identity = wordlist_identity(wrong_passwords)
        for wordlist, shows_result in ((wrong_passwords, lambda state: shows_exhausted(state, identity)),
                                       (right_password, lambda state: shows_key(state, UNCRACKED_PSK))):
            await asyncio.sleep(1.0)
            state = app.vault.ap_state(bssid)
            config = {"wordlists": [str(wordlist.resolve())], "default_passwords": False,
                      "ap_ssid_passwords": False, "probed_ssid_passwords": False}
            job_id = app.vault.manager.submit_job(ToolName.CPU_CRACK, state.hashline_capture, config,
                                                  [h.raw for h in state.uncracked])
            await wait_for(lambda: job_id in app.ending_poll_started
                           and (shown := shown_state(drawer, bssid)) and shows_result(shown), timeout_s=600)
            elapsed = await painted(drawer) - app.ending_poll_started[job_id]
            print(f"{wordlist.name} ({app.vault.manager.jobs[job_id].status.value}): {elapsed * 1000:.0f} ms "
                  f"from the job poll that ended it to the pane painted")


if __name__ == "__main__":
    asyncio.run(main())
