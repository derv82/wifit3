"""Termux fd acquisition and the multi-fd context manager."""
import os
import threading
import time

import pytest

from wifit3.device import termux_usb


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    termux_usb._entries.clear()
    termux_usb._denied.clear()
    termux_usb._reconciling = False
    termux_usb._poller = None
    termux_usb._last_error = None
    monkeypatch.delenv("TERMUX_USB_FD", raising=False)
    monkeypatch.setattr(termux_usb, "_ensure_poller", lambda: None)
    yield
    termux_usb._entries.clear()
    termux_usb._denied.clear()


class _Proc:
    def __init__(self):
        self.terminated = False
        self.killed = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


def test_list_devices_parses_json(monkeypatch):
    monkeypatch.setattr(termux_usb.subprocess, "check_output",
                        lambda *a, **k: '["/dev/bus/usb/001/002"]')
    assert termux_usb.list_devices() == ["/dev/bus/usb/001/002"]


def test_list_devices_empty_on_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("no termux-usb")

    monkeypatch.setattr(termux_usb.subprocess, "check_output", boom)
    assert termux_usb.list_devices() == []
    assert termux_usb._list_raw() is None


def test_fd_hung_up_detects_closed_fd():
    read_fd, write_fd = os.pipe()
    assert not termux_usb.fd_hung_up(read_fd)
    os.close(read_fd)
    assert termux_usb.fd_hung_up(read_fd)
    os.close(write_fd)


def test_send_and_receive_fd(tmp_path, monkeypatch):
    read_fd, write_fd = os.pipe()
    sock_path = str(tmp_path / "usb.sock")
    result = {}

    def receive():
        server = termux_usb._listen(sock_path)
        result["fd"] = termux_usb._recv_fd(server, _Proc(), timeout=5)

    thread = threading.Thread(target=receive)
    thread.start()
    deadline = time.monotonic() + 5
    while not os.path.exists(sock_path) and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)

    monkeypatch.setenv("TERMUX_USB_FD", str(read_fd))
    termux_usb.send_fd(sock_path)
    thread.join(5)

    received = result.get("fd")
    assert received is not None
    os.write(write_fd, b"hi")
    assert os.read(received, 2) == b"hi"

    os.close(received)
    os.close(read_fd)
    os.close(write_fd)


def test_send_fd_without_descriptor_is_a_no_op(tmp_path):
    termux_usb.send_fd(str(tmp_path / "missing.sock"))


def test_drops_dead_entry(monkeypatch):
    read_fd, write_fd = os.pipe()
    termux_usb._entries["/dev/bus/usb/001/002"] = termux_usb._Entry(
        "/dev/bus/usb/001/002", read_fd, "ctx")

    os.close(read_fd)
    assert termux_usb._backends() == []
    assert not termux_usb._entries

    os.close(write_fd)


def test_reconcile_adds_and_removes(monkeypatch):
    old = termux_usb._Entry("/dev/old", 99999, "old")
    termux_usb._entries["/dev/old"] = old
    monkeypatch.setattr(termux_usb, "_list_raw", lambda: ["/dev/new"])
    monkeypatch.setattr(termux_usb, "acquire", lambda path: 9)
    monkeypatch.setattr(termux_usb, "new_backend", lambda fd: f"ctx{fd}")
    monkeypatch.setattr(termux_usb, "fd_hung_up", lambda fd: False)
    closed = []
    monkeypatch.setattr(termux_usb, "_close_entry", closed.append)

    assert termux_usb._reconcile() is True

    assert set(termux_usb._entries) == {"/dev/new"}
    assert termux_usb._entries["/dev/new"].fd == 9
    assert closed == [old]
    assert not termux_usb._reconciling


def test_acquire_defers_a_denied_device(monkeypatch):
    monkeypatch.setattr(termux_usb, "_request_fd", lambda device: None)
    assert termux_usb.acquire("/dev/a") is None
    assert termux_usb._denied["/dev/a"].delay == termux_usb.ACQUIRE_BACKOFF


def test_denial_backoff_doubles_and_caps():
    termux_usb._defer("/dev/a")
    assert termux_usb._denied["/dev/a"].delay == termux_usb.ACQUIRE_BACKOFF
    termux_usb._defer("/dev/a")
    assert termux_usb._denied["/dev/a"].delay == termux_usb.ACQUIRE_BACKOFF * 2
    for _ in range(10):
        termux_usb._defer("/dev/a")
    assert termux_usb._denied["/dev/a"].delay == termux_usb.ACQUIRE_BACKOFF_MAX


def test_reconcile_skips_a_deferred_device(monkeypatch):
    monkeypatch.setattr(termux_usb, "_list_raw", lambda: ["/dev/a"])
    monkeypatch.setattr(termux_usb, "fd_hung_up", lambda fd: False)
    prompted = []
    monkeypatch.setattr(termux_usb, "acquire", lambda path: prompted.append(path))
    termux_usb._denied["/dev/a"] = termux_usb._Denial(time.monotonic() + 60, 30)

    assert termux_usb._reconcile() is True
    assert prompted == []


def test_reconcile_clears_backoff_after_a_grant(monkeypatch):
    monkeypatch.setattr(termux_usb, "_list_raw", lambda: ["/dev/a"])
    monkeypatch.setattr(termux_usb, "fd_hung_up", lambda fd: False)
    monkeypatch.setattr(termux_usb, "acquire", lambda path: 4)
    monkeypatch.setattr(termux_usb, "new_backend", lambda fd: "ctx")
    termux_usb._denied["/dev/a"] = termux_usb._Denial(0.0, 30)

    assert termux_usb._reconcile() is True
    assert "/dev/a" in termux_usb._entries
    assert "/dev/a" not in termux_usb._denied


def test_reconcile_reports_a_missing_termux_api(monkeypatch):
    monkeypatch.setattr(termux_usb, "_list_raw", lambda: None)
    assert termux_usb._reconcile() is False
    assert "Termux:API" in termux_usb.last_error()


def test_acquire_reaps_a_timed_out_process(monkeypatch):
    proc = _Proc()
    monkeypatch.setattr(termux_usb, "_listen", lambda path: type("S", (), {"close": lambda s: None})())
    monkeypatch.setattr(termux_usb.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(termux_usb, "_recv_fd", lambda *a, **k: None)

    assert termux_usb.acquire("/dev/bus/usb/001/002") is None
    assert proc.terminated


def test_acquire_listens_before_spawning_the_callback(monkeypatch):
    events = []
    monkeypatch.setattr(termux_usb, "_listen",
                        lambda path: events.append("listen") or type("S", (), {"close": lambda s: None})())
    monkeypatch.setattr(termux_usb.subprocess, "Popen",
                        lambda *a, **k: events.append("spawn") or _Proc())
    monkeypatch.setattr(termux_usb, "_recv_fd", lambda *a, **k: 3)

    assert termux_usb.acquire("/dev/bus/usb/001/002") == 3
    assert events == ["listen", "spawn"]


def test_enumerate_concatenates_contexts(monkeypatch):
    monkeypatch.setattr(termux_usb, "_backends", lambda: ["a", "b"])

    def find(**kw):
        return [kw["backend"], kw["backend"] + "2"]

    monkeypatch.setattr(termux_usb.usb.core, "find", find)
    assert termux_usb.enumerate_devices() == ["a", "a2", "b", "b2"]


def test_find_device_searches_contexts(monkeypatch):
    monkeypatch.setattr(termux_usb, "_backends", lambda: ["a", "b"])
    calls = []

    def find(**kw):
        calls.append(kw)
        return "dev" if kw["backend"] == "b" else None

    monkeypatch.setattr(termux_usb.usb.core, "find", find)
    assert termux_usb.find_device(0x1111, 0x2222, bus=1, address=2) == "dev"
    assert [c["backend"] for c in calls] == ["a", "b"]
    assert calls[0]["bus"] == 1 and calls[0]["address"] == 2


def test_find_all_devices(monkeypatch):
    monkeypatch.setattr(termux_usb, "_backends", lambda: ["a", "b"])
    monkeypatch.setattr(termux_usb.usb.core, "find", lambda **kw: [kw["backend"]])
    assert termux_usb.find_all_devices(1, 2) == ["a", "b"]
