"""rtl8812au_dkms driver glue NOT covered by the byte-for-byte pcap gate:
the RX dispatch fan-out, the 2.4 GHz-only ``set_channel`` guard (5 GHz is M7),
and the M6 ``inject_frame`` stub. (The bring-up sequence is verified in
``scripts/rtl8812au_dkms/verify_pcap.py``; the thread/loop hand-off in
``tests/chips/test_rx_reader.py``.)"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import wifit3.chips.rtl8812au_dkms.driver as drv


def _fake_params():
    return SimpleNamespace(bb_swing_2g=[0x200, 0x200], bb_swing_5g=[0x200, 0x200],
                           rfe_type=3, tx_power_2g="2g", tx_power_5g="5g")


def _patch_tune(monkeypatch, calls):
    monkeypatch.setattr(drv.chan, "set_channel_bw", lambda t, ch, **kw: calls.__setitem__("ch", ch))
    monkeypatch.setattr(drv.txpower, "set_tx_power", lambda t, ch, p: calls.__setitem__("2g", (ch, p)))
    monkeypatch.setattr(drv.txpower, "set_tx_power_5g", lambda t, ch, p: calls.__setitem__("5g", (ch, p)))


def test_dispatch_decodes_parses_and_fires_callback(monkeypatch):
    d = drv.Rtl8812auDkmsDriver(MagicMock())
    monkeypatch.setattr(
        drv, "iter_frames",
        lambda buf: [(b"\x80mpdu", -42)] if buf == b"BULK" else [],
    )
    monkeypatch.setattr(
        drv.WlanFrameParser, "parse_80211_frame",
        staticmethod(lambda mpdu, rssi: {"type": "beacon", "rssi": rssi}),
    )
    got = []
    d.register_rx_callback(got.append)
    d._dispatch(b"BULK")
    assert got == [{"type": "beacon", "rssi": -42}]


def test_dispatch_no_callback_is_safe(monkeypatch):
    d = drv.Rtl8812auDkmsDriver(MagicMock())
    monkeypatch.setattr(drv, "iter_frames", lambda buf: [(b"x", -1)])
    monkeypatch.setattr(
        drv.WlanFrameParser, "parse_80211_frame",
        staticmethod(lambda mpdu, rssi: {"type": "beacon"}),
    )
    d._dispatch(b"BULK")   # no callback registered -> must not raise


def test_dispatch_drops_unparseable(monkeypatch):
    d = drv.Rtl8812auDkmsDriver(MagicMock())
    monkeypatch.setattr(drv, "iter_frames", lambda buf: [(b"junk", -1)])
    monkeypatch.setattr(
        drv.WlanFrameParser, "parse_80211_frame",
        staticmethod(lambda mpdu, rssi: None),   # parser rejects it
    )
    got = []
    d.register_rx_callback(got.append)
    d._dispatch(b"BULK")
    assert got == []


async def test_set_channel_2ghz_routes_to_2g_txpower(monkeypatch):
    calls = {}
    _patch_tune(monkeypatch, calls)
    d = drv.Rtl8812auDkmsDriver(MagicMock())
    d._params = _fake_params()
    assert await d.set_channel(6) is True
    assert d._channel == 6
    assert calls["ch"] == 6 and calls["2g"] == (6, "2g") and "5g" not in calls


async def test_set_channel_5ghz_routes_to_5g_txpower(monkeypatch):
    calls = {}
    _patch_tune(monkeypatch, calls)
    d = drv.Rtl8812auDkmsDriver(MagicMock())
    d._params = _fake_params()
    assert await d.set_channel(36) is True
    assert d._channel == 36
    assert calls["ch"] == 36 and calls["5g"] == (36, "5g") and "2g" not in calls


async def test_set_channel_scan_skips_txpower(monkeypatch):
    calls = {}
    _patch_tune(monkeypatch, calls)
    d = drv.Rtl8812auDkmsDriver(MagicMock())
    d._params = _fake_params()
    assert await d.set_channel(36, scan=True) is True
    # A scan hop tunes only — no per-rate txagc re-apply (TX-only, skipped to save dwell).
    assert calls == {"ch": 36}


async def test_set_channel_without_params_is_false():
    d = drv.Rtl8812auDkmsDriver(MagicMock())
    # set_channel before connect (no EFUSE params yet) must not raise.
    assert await d.set_channel(6) is False


async def test_inject_frame_builds_desc_and_sends_on_bulk_out():
    transport = MagicMock()
    d = drv.Rtl8812auDkmsDriver(transport)
    # deauth-shaped MPDU: fc/dur(4) + addr1(6) + addr2(6) + addr3(6) + seq(2) + reason(2).
    frame = bytes.fromhex("c0000000") + b"\x11" * 6 + b"\x22" * 6 + b"\x33" * 6 + b"\x00\x00\x07\x00"
    assert await d.inject_frame(frame) is True
    transport.bulk_out.assert_called_once()
    sent = transport.bulk_out.call_args.args[0]
    # [40-byte fake TXDESC | frame]; the frame is appended verbatim (HW appends the FCS).
    assert len(sent) == 40 + len(frame)
    assert sent.endswith(frame)


async def test_inject_frame_rejects_runt():
    transport = MagicMock()
    d = drv.Rtl8812auDkmsDriver(transport)
    assert await d.inject_frame(b"\x00" * 6) is False     # < 10 B: no addr1 to read BMC
    transport.bulk_out.assert_not_called()
