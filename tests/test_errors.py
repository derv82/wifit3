"""PII scrubbing in the fatal-error trace (WifiteFatalError.trace)."""
from wifit3.errors import _scrub_paths


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
