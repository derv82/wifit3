"""The rtw88 replay engine's bulk-IN (device->host RX) serving.

Covers the additive RX path: ``ReplayDevice.read`` pops the captured bulk-IN completions as a
FIFO in capture order and, when drained, raises a timeout the way a real idle pipe does so the
driver's ``read_rx_burst`` returns None instead of hanging. No hardware and no capture files:
every case here is self contained Python.
"""
import sys
from pathlib import Path

import pytest
import usb.core

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "scripts" / "porting"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtw88_base.rx_common import read_rx_burst  # noqa: E402


# --- ReplayDevice.read FIFO: capture-order serving + empty-as-timeout -----------------

def test_read_serves_bulk_in_completions_in_capture_order():
    responses = [b"\xaa\x11", b"\xbb\x22\x33", b"\xcc"]
    dev = rp.ReplayDevice([], responses=responses)
    got = [bytes(dev.read(0x84, 16384, 100)) for _ in responses]
    assert got == responses


def test_read_returns_array_like_pyusb():
    dev = rp.ReplayDevice([], responses=[b"\x01\x02\x03"])
    out = dev.read(0x84, 16384, 100)
    # pyusb hands back an array('B', ...); the driver does bytes(data) on it.
    assert bytes(out) == b"\x01\x02\x03"


def test_drained_fifo_raises_timeout_usberror():
    dev = rp.ReplayDevice([], responses=[b"\x01"])
    dev.read(0x84, 16384, 100)                       # consume the only entry
    with pytest.raises(usb.core.USBError) as excinfo:
        dev.read(0x84, 16384, 100)
    # errno 110 (ETIMEDOUT) / 10060 is what read_rx_burst treats as "pipe idle -> None".
    assert excinfo.value.errno in (110, 10060)


def test_read_rx_burst_returns_payload_then_none_on_drain():
    dev = rp.ReplayDevice([], responses=[b"\xde\xad\xbe\xef"])
    assert read_rx_burst(dev, 0x84) == b"\xde\xad\xbe\xef"
    # FIFO now drained: the timeout USBError is swallowed and read_rx_burst returns None,
    # so a driver RX pump loop terminates cleanly instead of crashing or spinning.
    assert read_rx_burst(dev, 0x84) is None


def test_reads_do_not_touch_the_out_cursor():
    # The RX FIFO is decoupled: serving reads must not advance the positional OUT cursor,
    # so the pre-frontier OUT verification is unchanged whether or not RX is pumped.
    dev = rp.ReplayDevice([{"dir": "BULK", "data": b"\x00", "frame": 1}], responses=[b"\x99"])
    dev.read(0x84, 16384, 100)
    assert dev.i == 0 and dev.resp_i == 1


def test_no_responses_keeps_prior_behavior():
    # A capture with no extracted RX ops (responses defaults to None) behaves as before:
    # an empty FIFO that reads as a timeout, and an untouched OUT cursor.
    dev = rp.ReplayDevice([{"dir": "BULK", "data": b"\x00", "frame": 1}])
    assert dev.responses == []
    assert read_rx_burst(dev, 0x84) is None
    assert dev.i == 0

