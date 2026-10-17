import asyncio
import threading
from types import SimpleNamespace

from wifit3.ui.app import WifiteApp


async def test_job_action_and_followup_poll_run_off_event_loop():
    loop_thread = threading.get_ident()
    calls = []

    def action(job_id):
        calls.append(("action", job_id, threading.get_ident()))
        return True

    def poll_jobs():
        calls.append(("poll", None, threading.get_ident()))

    manager = SimpleNamespace(poll_jobs=poll_jobs, get_active_jobs=lambda: [])
    host = SimpleNamespace(
        _job_lock=asyncio.Lock(),
        vault=SimpleNamespace(manager=manager),
        active_jobs=None,
        _notify_completions=lambda: calls.append(("notify", None, threading.get_ident())),
    )

    applied = await WifiteApp._run_job_action(host, "job-1", action)

    assert applied is True
    assert calls[0][:2] == ("action", "job-1")
    assert calls[1][:2] == ("poll", None)
    assert calls[0][2] != loop_thread
    assert calls[1][2] != loop_thread
    assert calls[2] == ("notify", None, loop_thread)


async def test_periodic_job_poll_is_coalesced_while_job_work_runs():
    polled = []
    manager = SimpleNamespace(
        poll_jobs=lambda: polled.append(True),
        get_active_jobs=lambda: [],
    )
    lock = asyncio.Lock()
    host = SimpleNamespace(
        _job_lock=lock,
        vault=SimpleNamespace(manager=manager),
        active_jobs=None,
        _notify_completions=lambda: None,
    )

    async with lock:
        await WifiteApp._poll_jobs(host)

    assert polled == []


async def test_periodic_job_poll_runs_off_event_loop():
    loop_thread = threading.get_ident()
    poll_threads = []
    manager = SimpleNamespace(
        poll_jobs=lambda: poll_threads.append(threading.get_ident()),
        get_active_jobs=lambda: [],
    )
    notified = []
    host = SimpleNamespace(
        _job_lock=asyncio.Lock(),
        vault=SimpleNamespace(manager=manager),
        active_jobs=None,
        _notify_completions=lambda: notified.append(threading.get_ident()),
    )

    await WifiteApp._poll_jobs(host)

    assert poll_threads and poll_threads[0] != loop_thread
    assert notified == [loop_thread]
