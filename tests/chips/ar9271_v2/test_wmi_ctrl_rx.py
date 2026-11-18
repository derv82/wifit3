"""WMI replies handled as REG_IN delivers them: ath9k_htc_rx_msg -> ath9k_wmi_ctrl_rx (htc_hst.c:406-490,
wmi.c:215-254) and ath9k_wmi_cmd's wait (wmi.c:310-366)."""
import errno
import queue
import struct

import pytest
import usb.core

from wifit3.chips.ar9271_v2 import constants as C, htc, reg as R
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import WMI, WMI_START_RECV_CMDID

CTRL_EPID = 1


def _reply(cmd_id, seq, value=b"\x00\x00\x00\x2a", epid=CTRL_EPID, flags=0, trailer=b""):
    body = struct.pack(">HH", cmd_id, seq) + value + trailer
    return struct.pack(">BBH", epid, flags, len(body)) + bytes([len(trailer), 0, 0, 0]) + body


class FakeDev:
    """Every REG_OUT command is answered on REG_IN with ``replies(cmd_id, seq)`` (a list of frames)."""

    def __init__(self, replies=None):
        self.replies = replies or (lambda cmd_id, seq: [_reply(cmd_id, seq)])
        self.reg_in = queue.Queue()

    def write(self, ep, data, timeout=None):
        cmd_id, seq = struct.unpack_from(">HH", data, C.HTC_FRAME_HDR_LEN)
        for frame in self.replies(cmd_id, seq):
            self.reg_in.put(frame)
        return len(data)

    def read(self, ep, length, timeout=None):
        try:
            return bytearray(self.reg_in.get(timeout=0.01))
        except queue.Empty:
            raise usb.core.USBTimeoutError("Operation timed out") from None


@pytest.fixture
def started():
    transports = []

    def start(dev):
        t = AR9271Transport(dev)
        w = WMI(t, ctrl_epid=CTRL_EPID)
        t.alloc_reg_in_urbs()
        transports.append(t)
        return w

    yield start
    for t in transports:
        t.dealloc_reg_in_urbs()


def test_reply_through_the_reader_completes_the_command(started):
    w = started(FakeDev())
    assert w.reg_read(R.AR_CFG) == 0x2a


def test_events_and_stale_replies_are_dropped(started):
    def replies(cmd_id, seq):
        return [_reply(0x1001, 0, b"\xff\xff\xff\xff"),
                _reply(cmd_id, seq - 1, b"\xff\xff\xff\xff"),
                _reply(cmd_id, seq)]

    w = started(FakeDev(replies))
    assert w.reg_read(R.AR_CFG) == 0x2a


def test_other_endpoints_are_dropped(started):
    def replies(cmd_id, seq):
        return [_reply(cmd_id, seq, b"\xff\xff\xff\xff", epid=CTRL_EPID + 1), _reply(cmd_id, seq)]

    w = started(FakeDev(replies))
    assert w.reg_read(R.AR_CFG) == 0x2a


def test_trailer_is_trimmed():
    bodies = []
    htc.rx_msg(_reply(0x0014, 1, flags=C.HTC_FLAGS_RECV_TRAILER, trailer=b"\xee\xee"),
               CTRL_EPID, bodies.append)
    assert bodies == [struct.pack(">HH", 0x0014, 1) + b"\x00\x00\x00\x2a"]


def test_a_bad_vaddr_firmware_panic_is_logged(caplog):
    bodies = []
    htc.rx_msg(struct.pack("<4I", 0x33221199, 0x1C, 0x8E0012AB, 0x00500000), CTRL_EPID, bodies.append)
    assert bodies == []
    assert "ath: firmware panic! exccause: 0x0000001c; pc: 0x8e0012ab; badvaddr: 0x00500000." in caplog.text


def test_a_bad_epid_firmware_panic_is_logged(caplog):
    htc.rx_msg(struct.pack("<2I", 0x33221299, 0x2A), CTRL_EPID, lambda body: None)
    assert "ath: firmware panic! bad epid: 0x0000002a" in caplog.text


def test_an_unknown_firmware_panic_pattern_is_logged(caplog):
    htc.rx_msg(struct.pack("<2I", 0x00000099, 0), CTRL_EPID, lambda body: None)
    assert "ath: unknown panic pattern!" in caplog.text


def test_timeout_clears_last_seq_id_and_the_late_reply_is_dropped(started):
    late = []

    def replies(cmd_id, seq):
        if cmd_id == WMI_START_RECV_CMDID:
            late.append(_reply(cmd_id, seq, b"\xff\xff\xff\xff"))
            return []
        return late + [_reply(cmd_id, seq)]

    w = started(FakeDev(replies))
    with pytest.raises(usb.core.USBTimeoutError):
        w.cmd(WMI_START_RECV_CMDID, b"", timeout_ms=20)
    assert w.last_seq_id == 0
    assert w.reg_read(R.AR_CFG) == 0x2a


def test_unplug_during_the_wait_raises_device_gone(started):
    class GoneDev(FakeDev):
        def read(self, ep, length, timeout=None):
            raise usb.core.USBError("No such device", errno=errno.ENODEV)

    w = started(GoneDev(lambda cmd_id, seq: []))
    with pytest.raises(usb.core.USBError) as exc_info:
        w.cmd(WMI_START_RECV_CMDID, b"", timeout_ms=20)
    assert exc_info.value.errno == errno.ENODEV


def test_short_replies_are_zero_padded_to_the_length_read(started):
    # ath9k_wmi_rsp_callback copies cmd_rsp_len bytes whatever the reply's length [SRC] wmi.c:209-210
    w = started(FakeDev(lambda cmd_id, seq: [_reply(cmd_id, seq, b"\x12\x34")]))
    assert w.reg_read(R.AR_CFG) == 0x12340000
    assert w.multi_reg_read([R.AR_CFG, R.AR_CFG]) == [0x12340000, 0]
    assert w.get_fw_version() == (0x1234, 0)


def test_events_go_to_the_event_tasklet(started):
    events = []

    def replies(cmd_id, seq):
        return [_reply(0x1007, 0, b"\x01\x05\x50\x00"), _reply(cmd_id, seq)]

    w = started(FakeDev(replies))
    w.event_tasklet = lambda cmd_id, event: events.append((cmd_id, event))
    assert w.reg_read(R.AR_CFG) == 0x2a
    assert events == [(0x1007, b"\x01\x05\x50\x00")]
