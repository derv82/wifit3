import asyncio
from unittest.mock import MagicMock

import pytest
import usb.core

from wifit3.chips.rt3070.rx import read_rx_burst as rt3070_read_rx_burst
from wifit3.chips.rtl8187.rx import read_rx_burst as rtl8187_read_rx_burst
from wifit3.chips.rx_reader import RxReaderThread


@pytest.fixture(params=[rt3070_read_rx_burst, rtl8187_read_rx_burst],
                ids=["rt3070", "rtl8187"])
def read_rx_burst(request):
    return request.param


@pytest.mark.parametrize("error_number", [60, 110, 10060, None],
                         ids=["macos", "linux", "windows", "no-errno"])
def test_read_rx_burst_ignores_usb_timeout(read_rx_burst, error_number):
    dev = MagicMock()
    dev.read.side_effect = usb.core.USBTimeoutError(
        "Operation timed out", error_code=-7, errno=error_number)

    assert read_rx_burst(dev, 0x81) is None


@pytest.mark.parametrize("error_number,message", [
    (110, "Operation timed out"),
    (10060, "Operation timed out"),
    (None, "USB timeout"),
])
def test_read_rx_burst_preserves_timeout_fallbacks(read_rx_burst, error_number, message):
    dev = MagicMock()
    dev.read.side_effect = usb.core.USBError(message, errno=error_number)

    assert read_rx_burst(dev, 0x81) is None


@pytest.mark.parametrize("error_number,backend", [(19, -4), (5, -1), (13, -3)])
def test_read_rx_burst_propagates_other_usb_errors(read_rx_burst, error_number, backend):
    dev = MagicMock()
    error = usb.core.USBError("USB failure", error_code=backend, errno=error_number)
    dev.read.side_effect = error

    with pytest.raises(usb.core.USBError) as raised:
        read_rx_burst(dev, 0x81)

    assert raised.value is error


def test_read_rx_burst_returns_received_data(read_rx_burst):
    dev = MagicMock()
    dev.read.return_value = bytearray(b"received frame")

    assert read_rx_burst(dev, 0x81, max_size=4096, timeout_ms=25) == b"received frame"
    dev.read.assert_called_once_with(0x81, 4096, 25)


async def test_reader_survives_macos_timeouts_then_detects_unplug(read_rx_burst):
    dev = MagicMock()
    unplugged = usb.core.USBError("No such device", error_code=-4, errno=19)
    dev.read.side_effect = [
        *[usb.core.USBTimeoutError("Operation timed out", error_code=-7, errno=60)
          for _ in range(6)],
        b"received frame",
        unplugged,
    ]
    dispatched = []
    fatals = []
    fatal_event = asyncio.Event()

    def on_fatal(error):
        fatals.append(error)
        fatal_event.set()

    reader = RxReaderThread(asyncio.get_running_loop(), lambda: read_rx_burst(dev, 0x81),
                            dispatched.append, on_fatal=on_fatal)
    reader.start()
    try:
        await asyncio.wait_for(fatal_event.wait(), timeout=1.0)
    finally:
        await reader.stop()

    assert dispatched == [b"received frame"]
    assert fatals == [unplugged]
    assert dev.read.call_count == 8
