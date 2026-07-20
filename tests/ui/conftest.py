import pytest
from unittest.mock import patch


@pytest.fixture
def no_usb_devices():
    """Bus scan finds zero cards, so booting WifiteApp touches no real backend.

    Opt-in (NOT autouse) by design: the app-boot / UI tests don't want hardware, but the
    libusb-backend-failure tests need the real usb.core.find path intact, so a global stub
    would make those untestable. Tests request it with @pytest.mark.usefixtures("no_usb_devices").
    """
    with patch('usb.core.find', return_value=[]):
        yield
