"""One test case per capture path; a capture missing on this machine is skipped (captures are gitignored)."""
from __future__ import annotations

import pytest

from tests.verify_pcap.verify_capture import VerifyCapture


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "capture_path" not in metafunc.fixturenames:
        return
    test_class = metafunc.cls
    assert test_class is not None and issubclass(test_class, VerifyCapture)
    params = []
    for path in test_class.CAPTURE_PATHS:
        marks = [] if path.exists() else [pytest.mark.skip(reason=f"capture not found: {path}")]
        params.append(pytest.param(path, id=f"{path.parent.name}/{path.stem}", marks=marks))
    metafunc.parametrize("capture_path", params)
