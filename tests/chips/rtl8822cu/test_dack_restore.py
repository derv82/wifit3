from unittest.mock import Mock

from wifit3.chips.rtl8822cu import cal


def test_disabled_restore_does_not_touch_device(monkeypatch):
    read = Mock()
    write = Mock()
    monkeypatch.setattr(cal, "get_bb_reg", read)
    monkeypatch.setattr(cal, "set_bb_reg", write)
    cal._dack_restore(object(), cal.DackState())
    read.assert_not_called()
    write.assert_not_called()


def test_restore_order_tables_and_poll_retries(monkeypatch):
    events = []
    counts = {}
    device = object()
    state = cal.DackState(dack_en=True)
    state.dck_d = [[[1, 2], [3, 4]], [[5, 6], [7, 8]]]
    state.biask_d = [[11, 12], [13, 14]]
    state.msbk_d = [[[path * 100 + branch * 20 + i for i in range(15)]
                     for branch in range(2)] for path in range(2)]

    def read(t, addr, mask):
        events.append(("r", addr, mask))
        if mask == cal.MASKDWORD:
            return {0x1860: 21, 0x4160: 22, 0x9B4: 23}[addr]
        key = (addr, mask)
        counts[key] = counts.get(key, 0) + 1
        if mask == 0x7FFF80:
            return 0xFFFF if counts[key] >= 4 else 0
        if mask == 0xF:
            return 6 if counts[key] >= 2 else 0
        return 0

    monkeypatch.setattr(cal, "get_bb_reg", read)
    monkeypatch.setattr(cal, "set_bb_reg", lambda t, addr, mask, val: events.append(("w", addr, mask, val)))
    cal.dac_cal(device, state)

    assert events[:4] == [("r", 0x1860, cal.MASKDWORD), ("r", 0x4160, cal.MASKDWORD),
                          ("r", 0x9B4, cal.MASKDWORD), ("w", 0x9B4, cal.MASKDWORD, 0xDB66DB00)]
    dck = [("w", addr, mask, value) for addr, mask, value in (
        (0x18BC, 1 << 19, 1), (0x18BC, 0xF0000000, 1), (0x18C0, 0xF, 2),
        (0x18D8, 1 << 19, 1), (0x18D8, 0xF0000000, 3), (0x18DC, 0xF, 4),
        (0x41BC, 1 << 19, 1), (0x41BC, 0xF0000000, 5), (0x41C0, 0xF, 6),
        (0x41D8, 1 << 19, 1), (0x41D8, 0xF0000000, 7), (0x41DC, 0xF, 8))]
    start = events.index(dck[0])
    assert events[start:start + len(dck)] == dck
    for path, branches in enumerate((((0x18B8, 0x2808, 0x18B4, 0x18B0), (0x18D4, 0x2834, 0x18D0, 0x18CC)),
                                      ((0x41B8, 0x4508, 0x41B4, 0x41B0), (0x41D4, 0x4534, 0x41D0, 0x41CC)))):
        for branch, (trigger, status, data, ctrl) in enumerate(branches):
            assert counts[status, 0x7FFF80] == 4
            assert counts[status, 0xFF] == 2
            assert counts[status, 0xF] == 2
            expected = [("w", trigger, 0x6000000, 0), ("w", trigger, 0x6000000, 2),
                        ("r", status, 0xFF), ("r", status, 0xF),
                        ("w", trigger, 0x6000000, 0), ("w", trigger, 0x6000000, 2),
                        ("r", status, 0xFF), ("r", status, 0xF)]
            for i in range(15):
                expected += [("w", data, 4, 0), ("w", data, 0xFF8, state.msbk_d[path][branch][i]),
                             ("w", ctrl, 0xF0000000, i), ("w", data, 4, 1)]
            expected.append(("w", data, 4, 0))
            start = events.index(expected[0], events.index(("r", 0x4534, 0x7FFF80)))
            assert events[start:start + len(expected)] == expected
    assert events[-4:] == [("w", 0x18B0, 0x1FF8000, 11), ("w", 0x18CC, 0x1FF8000, 12),
                           ("w", 0x41B0, 0x1FF8000, 13), ("w", 0x41CC, 0x1FF8000, 14)]
    assert ("w", 0x1860, cal.MASKDWORD, 21) in events
    assert ("w", 0x4160, cal.MASKDWORD, 22) in events
    assert ("w", 0x9B4, cal.MASKDWORD, 23) in events


def test_poll_timeout_is_bounded_and_restore_continues(monkeypatch):
    counts = {}
    writes = []

    def read(t, addr, mask):
        key = (addr, mask)
        counts[key] = counts.get(key, 0) + 1
        return 0

    monkeypatch.setattr(cal, "get_bb_reg", read)
    monkeypatch.setattr(cal, "set_bb_reg", lambda t, addr, mask, val: writes.append((addr, mask, val)))
    cal._dack_restore(object(), cal.DackState(dack_en=True))
    for addr in (0x2808, 0x2834, 0x4508, 0x4534):
        assert counts[addr, 0x7FFF80] == 20000
        assert counts[addr, 0xFF] == 10000
        assert counts[addr, 0xF] == 10000
    assert writes[-1] == (0x41CC, 0x1FF8000, 0)
