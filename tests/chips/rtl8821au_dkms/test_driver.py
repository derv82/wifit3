from unittest.mock import MagicMock

import pytest

from wifit3.chips.rtl8821au_dkms.driver import Rtl8821auDkmsDriver
from wifit3.errors import SetChannelError


async def test_set_channel_rejects_an_unsupported_channel_before_any_write():
    drv = object.__new__(Rtl8821auDkmsDriver)     # skip __init__ (no USB device needed)
    drv.transport = MagicMock()
    with pytest.raises(SetChannelError):
        await drv.set_channel(200)
    assert drv.transport.method_calls == []
