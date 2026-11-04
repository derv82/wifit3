"""Android USB file descriptors for Termux: one libusb context per granted ``termux-usb`` fd,
kept fresh by a single background poller."""
from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

import usb.core

LIST_INTERVAL = 2.0
LIST_FAILURE_INTERVAL = 15.0
ACQUIRE_TIMEOUT = 120.0
ACQUIRE_BACKOFF = 30.0
ACQUIRE_BACKOFF_MAX = 600.0

_lock = threading.RLock()
_env_lock = threading.Lock()
_entries: dict[str, "_Entry"] = {}
_denied: dict[str, "_Denial"] = {}
_reconciling = False
_poller: threading.Thread | None = None
_last_error: str | None = None


@dataclass(frozen=True)
class _Entry:
    path: str
    fd: int
    backend: object


@dataclass(frozen=True)
class _Denial:
    retry_at: float
    delay: float


def list_devices() -> list[str]:
    """The usbfs paths Android currently exposes to this app."""
    return _list_raw() or []


def _list_raw() -> list[str] | None:
    try:
        return json.loads(subprocess.check_output(["termux-usb", "-l"], text=True))
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def send_fd(sock_path: str) -> None:
    """Callback mode: forward ``TERMUX_USB_FD`` to wifit3 over its Unix socket."""
    raw = os.environ.get("TERMUX_USB_FD")
    if raw is None:
        print("TERMUX_USB_FD is not set; cannot forward the descriptor", file=sys.stderr)
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(sock_path)
        socket.send_fds(sock, [b"wifit3"], [int(raw)])


def _listen(sock_path: str) -> socket.socket:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    server.settimeout(1.0)
    return server


def _recv_fd(server: socket.socket, proc: subprocess.Popen, timeout: float) -> int | None:
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                conn, _ = server.accept()
                break
            except TimeoutError:
                if proc.poll() is not None or time.monotonic() >= deadline:
                    return None
        try:
            _, fds, _, _ = socket.recv_fds(conn, 1, 1)
        finally:
            conn.close()
        return fds[0] if fds else None
    finally:
        server.close()


def _reap(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        proc.wait()
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def acquire(device: str | None = None) -> int | None:
    """Request ``device`` (or the first attached device) and return its fd, or None.
    The socket is bound before ``termux-usb`` starts, so a fast callback cannot race it."""
    if device is None:
        found = list_devices()
        if not found:
            _set_error("no USB device is visible; connect the adapter and install the "
                       "Termux:API app")
            return None
        device = found[0]
    fd = _request_fd(device)
    if fd is None:
        _defer(device)
    return fd


def _request_fd(device: str) -> int | None:
    base = os.environ.get("PREFIX") or os.environ.get("HOME") or "/data/data/com.termux/files/usr"
    sock_path = os.path.join(base, "tmp", f"wifit3-usb-{os.getpid()}-{threading.get_ident()}.sock")
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        _set_error(f"cannot use the USB handshake socket: {exc}")
        return None

    try:
        server = _listen(sock_path)
    except OSError as exc:
        _set_error(f"cannot open the USB handshake socket: {exc}")
        return None

    callback = f"{sys.executable} -m wifit3.device.termux_usb --send-fd {sock_path}"
    try:
        proc = subprocess.Popen(["termux-usb", "-r", "-E", "-e", callback, device],
                                stderr=subprocess.DEVNULL)
    except OSError as exc:
        server.close()
        _set_error(f"termux-usb could not start: {exc}")
        return None
    try:
        return _recv_fd(server, proc, ACQUIRE_TIMEOUT)
    except OSError as exc:
        _set_error(f"could not receive the USB descriptor: {exc}")
        return None
    finally:
        _reap(proc)
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass


def last_error() -> str | None:
    """Why the last background reconcile failed, or None when it succeeded."""
    with _lock:
        return _last_error


def fd_hung_up(fd: int) -> bool:
    try:
        poller = select.poll()
        poller.register(fd, select.POLLERR | select.POLLHUP | select.POLLNVAL)
        return bool(poller.poll(0))
    except (OSError, ValueError):
        return True


def new_backend(fd: int):
    """A libusb context whose initial scan reads ``fd`` through the temporary env override."""
    from usb.backend import libusb1

    try:
        libusb1.get_backend()
        lib = getattr(libusb1, "_lib", None)
        factory = getattr(libusb1, "_LibUSB", None)
        if lib is None or factory is None:
            return None
        with _env_lock:
            previous = os.environ.get("TERMUX_USB_FD")
            os.environ["TERMUX_USB_FD"] = str(fd)
            try:
                return factory(lib)
            finally:
                if previous is None:
                    os.environ.pop("TERMUX_USB_FD", None)
                else:
                    os.environ["TERMUX_USB_FD"] = previous
    except Exception:
        return None


def _set_error(message: str | None) -> None:
    global _last_error
    with _lock:
        _last_error = message


def _close_entry(entry: _Entry) -> None:
    try:
        os.close(entry.fd)
    except OSError:
        pass


def _add_entry(path: str, fd: int) -> bool:
    backend = new_backend(fd)
    if backend is None:
        return False
    with _lock:
        if path in _entries:
            return False
        _entries[path] = _Entry(path, fd, backend)
    return True


def _defer(path: str) -> None:
    now = time.monotonic()
    with _lock:
        previous = _denied.get(path)
        delay = ACQUIRE_BACKOFF if previous is None else min(
            previous.delay * 2, ACQUIRE_BACKOFF_MAX)
        _denied[path] = _Denial(now + delay, delay)


def _reconcile() -> bool:
    global _reconciling
    with _lock:
        if _reconciling:
            return True
        _reconciling = True
    try:
        paths = _list_raw()
        if paths is None:
            _set_error("termux-usb is unavailable; install the Termux:API app and its "
                       "termux-api package")
            return False
        now = time.monotonic()
        with _lock:
            stale = [e for e in _entries.values() if e.path not in paths or fd_hung_up(e.fd)]
            for entry in stale:
                _entries.pop(entry.path, None)
            for gone in [p for p in _denied if p not in paths]:
                _denied.pop(gone, None)
            wanted = [p for p in paths
                      if p not in _entries and (p not in _denied or _denied[p].retry_at <= now)]
        for entry in stale:
            _close_entry(entry)
        _set_error(None)
        for path in wanted:
            fd = acquire(path)
            if fd is None:
                continue
            if not _add_entry(path, fd):
                os.close(fd)
                _defer(path)
                continue
            with _lock:
                _denied.pop(path, None)
        return True
    finally:
        with _lock:
            _reconciling = False


def _poll_loop() -> None:
    while True:
        try:
            ok = _reconcile()
        except Exception as exc:
            _set_error(f"USB watcher failed: {exc}")
            ok = False
        time.sleep(LIST_INTERVAL if ok else LIST_FAILURE_INTERVAL)


def _ensure_poller() -> None:
    global _poller
    if _poller is not None:
        return
    with _lock:
        if _poller is not None:
            return
        poller = threading.Thread(target=_poll_loop, name="termux-usb-watch", daemon=True)
        try:
            poller.start()
        except RuntimeError as exc:
            _set_error(f"USB watcher could not start: {exc}")
            return
        _poller = poller


def _backends() -> list[object]:
    with _lock:
        dead = [e for e in _entries.values() if fd_hung_up(e.fd)]
        for entry in dead:
            _entries.pop(entry.path, None)
        _ensure_poller()
        snapshot = [e.backend for e in _entries.values()]
    for entry in dead:
        _close_entry(entry)
    return snapshot


def enumerate_devices() -> list[usb.core.Device]:
    """Every device across all granted Android fds."""
    out: list[usb.core.Device] = []
    for backend in _backends():
        try:
            out.extend(usb.core.find(find_all=True, backend=backend) or [])
        except Exception:
            continue
    return out


def find_device(vid: int, pid: int, bus: int | None = None,
                address: int | None = None) -> usb.core.Device | None:
    """The first matching device across all granted Android fds."""
    for backend in _backends():
        extra = {"bus": bus, "address": address} if bus is not None and address is not None else {}
        try:
            dev = usb.core.find(idVendor=vid, idProduct=pid, backend=backend, **extra)
        except Exception:
            continue
        if dev is not None:
            return dev
    return None


def find_all_devices(vid: int, pid: int) -> list[usb.core.Device]:
    """Every device with this VID:PID across all granted Android fds."""
    out: list[usb.core.Device] = []
    for backend in _backends():
        try:
            out.extend(usb.core.find(find_all=True, idVendor=vid, idProduct=pid,
                                     backend=backend) or [])
        except Exception:
            continue
    return out


if __name__ == "__main__":
    if sys.argv[1:2] == ["--send-fd"]:
        send_fd(sys.argv[2])
    else:
        print("usage: python -m wifit3.device.termux_usb --send-fd SOCKET", file=sys.stderr)
        sys.exit(2)
