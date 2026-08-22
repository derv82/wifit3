import pytest
from unittest.mock import patch

from wifit3.persist.config import Config


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """UI tests build WifiteApp, which loads and persists Config: keep that off the real on-disk
    config file and reset the class-level defaults so a theme edit can't leak between tests."""
    monkeypatch.setattr("wifit3.persist.config._PATH", tmp_path / "config.toml")
    Config.theme = "wifit3-green-dark"
    Config.scanner_sort = "signal"
    Config.scanner_sort_reverse = True
    Config.silenced_bssids = []
    yield


@pytest.fixture
def no_usb_devices():
    """Bus scan finds zero cards, so booting WifiteApp touches no real backend.

    Opt-in (NOT autouse) by design: the app-boot / UI tests don't want hardware, but the
    libusb-backend-failure tests need the real usb.core.find path intact, so a global stub
    would make those untestable. Tests request it with @pytest.mark.usefixtures("no_usb_devices").
    """
    with patch('usb.core.find', return_value=[]):
        yield
