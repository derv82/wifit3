"""rtl8812au_dkms driver glue NOT covered by the byte-for-byte pcap gate:
the RX dispatch fan-out, the 2.4 GHz-only ``set_channel`` guard (5 GHz is M7),
and the M6 ``inject_frame`` stub. (The bring-up sequence is verified in
``scripts/rtl8812au_dkms/verify_pcap.py``; the thread/loop hand-off in
``tests/chips/test_rx_reader.py``.)"""
from unittest.mock import MagicMock

import wifit3.chips.rtl8812au_dkms.driver as drv


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


async def test_set_channel_rejects_5ghz():
    d = drv.Rtl8812auDkmsDriver(MagicMock())
    # 5 GHz is M7 (chan._switch_band_5g raises) -> guarded out before any USB I/O.
    assert await d.set_channel(36) is False
    assert d._channel is None


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
