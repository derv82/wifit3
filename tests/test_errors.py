"""PII scrubbing in the fatal-error trace (WifiteFatalError.trace)."""
import usb.core

from wifit3.errors import _scrub_paths, is_device_gone


def test_device_gone_matches_no_device_backend_code():
    e = usb.core.USBError("no dev", errno=None)
    e.backend_error_code = -4  # LIBUSB_ERROR_NO_DEVICE
    assert is_device_gone(e)


def test_device_gone_matches_enodev_errno():
    assert is_device_gone(usb.core.USBError("no dev", errno=19))


def test_device_gone_rejects_timeout_and_io_and_non_usb():
    timeout = usb.core.USBError("timeout", errno=110)
    timeout.backend_error_code = -7  # LIBUSB_ERROR_TIMEOUT
    io = usb.core.USBError("io", errno=5)
    io.backend_error_code = -1       # LIBUSB_ERROR_IO, too broad to treat as unplug
    assert not is_device_gone(timeout)
    assert not is_device_gone(io)
    assert not is_device_gone(RuntimeError("boom"))


def test_scrub_trims_in_tree_frame_to_wifit3_relative():
    raw = '  File "C:\\Users\\xxxx\\Documents\\Projects\\wifit3\\src\\wifit3\\ui\\splash.py", line 1, in f\n'
    assert _scrub_paths(raw).startswith('  File "wifit3\\src\\wifit3\\ui\\splash.py"')


def test_scrub_collapses_home_in_external_frame(monkeypatch):
    monkeypatch.setattr("os.path.expanduser", lambda _p: r"C:\Users\xxxx")
    raw = '  File "C:\\Users\\xxxx\\AppData\\uv\\Lib\\asyncio\\threads.py", line 2, in g\n'
    out = _scrub_paths(raw)
    assert "xxxx" not in out
    assert "~\\AppData\\uv\\Lib\\asyncio\\threads.py" in out


def test_scrub_handles_posix_in_tree_frame():
    raw = '  File "/home/xxxx/projects/wifit3/src/wifit3/x.py", line 3, in h\n'
    assert 'File "wifit3/src/wifit3/x.py"' in _scrub_paths(raw)
