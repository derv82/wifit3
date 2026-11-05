from __future__ import annotations

import pytest

from tests.verify_pcap.capture_replay import CaptureDivergence, CaptureReplay
from tests.verify_pcap.test_waivers import REALTEK_OTHER_REGISTER, REALTEK_WAIVED_REGISTER, realtek_read
from tests.verify_pcap.virtual_clock_loop import MAIN_CALLER, VirtualClockEventLoop

PROBE_REGISTER = 0x00F0


def test_probe_reply_answers_only_before_the_capture_s_first_op_and_is_not_compared() -> None:
    probe_reply = realtek_read(PROBE_REGISTER, 0x1234)
    ops = [realtek_read(REALTEK_OTHER_REGISTER, 0x1), realtek_read(REALTEK_WAIVED_REGISTER, 0x2)]
    loop = VirtualClockEventLoop(0.0, lambda: True, frozenset())
    replay = CaptureReplay(ops, lambda captured, request: captured.same_request(request),
                           lambda request, cursor_differs: False, lambda ops, position: None,
                           lambda caller_ops, request: False, loop)
    replay.probe_replies.append(probe_reply)

    assert replay.match_request(realtek_read(PROBE_REGISTER, 0), MAIN_CALLER) is probe_reply
    assert replay.probe_replies == []
    assert replay.reproduced_by == [None, None]
    assert replay.match_request(realtek_read(REALTEK_OTHER_REGISTER, 0), MAIN_CALLER) is ops[0]
    replay.probe_replies.append(probe_reply)
    with pytest.raises(CaptureDivergence):
        replay.match_request(realtek_read(PROBE_REGISTER, 0), MAIN_CALLER)
