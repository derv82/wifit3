"""rtl8188ftv_dkms driver assembly: FW asset, RX dispatch, channel plumbing."""
from unittest.mock import MagicMock


def _driver():
    from wifit3.chips.rtl8188ftv_dkms.driver import Rtl8188ftvDkmsDriver
    d = Rtl8188ftvDkmsDriver(MagicMock())
    return d


def test_firmware_asset_loads():
    from wifit3.chips.rtl8188ftv_dkms import firmware as fw
    blob = fw.load_firmware_blob()
    assert len(blob) == 21020
    assert fw.parse_header(blob)[:3] == (4, 0, 0x88F1)


def test_rx_dispatch_control_frames_parse_to_none():
    import subprocess
    from wifit3.chips.rtl8188ftv_dkms import rx as rx_mod
    from wifit3.dot11.parser import WlanFrameParser
    out = subprocess.run(
        ["tshark", "-r",
         "driver_captures/captures_8188fu/capture-1.pcap",
         "-T", "fields", "-e", "usb.capdata",
         "-Y", "frame.number==3747"],
        capture_output=True, text=True, check=True).stdout.strip()
    pkts = list(rx_mod.iter_rx(bytes.fromhex(out)))
    assert [len(p) for _, p in pkts] == [20, 14]
    assert [p[:2] for _, p in pkts] == [b"\xb4\x00", b"\xc4\x00"]
    assert all(WlanFrameParser.parse_80211_frame(p, -100) is None
               for _, p in pkts)


def test_rx_dispatch_beacon_bssid():
    import subprocess
    from wifit3.chips.rtl8188ftv_dkms import rx as rx_mod
    from wifit3.dot11.parser import WlanFrameParser
    out = subprocess.run(
        ["tshark", "-r",
         "driver_captures/captures_8188fu/capture-1.pcap",
         "-T", "fields", "-e", "frame.number", "-e", "usb.capdata",
         "-Y", "usb.endpoint_address==0x81 && usb.urb_type==67 && frame.len>300"],
        capture_output=True, text=True, check=True).stdout
    bssids = set()
    for line in out.splitlines():
        _, _, capdata = line.partition("\t")
        capdata = capdata.strip()
        if not capdata:
            continue
        for attrib, payload in rx_mod.iter_rx(bytes.fromhex(capdata)):
            if attrib["c2h"] or len(payload) < 24:
                continue
            try:
                pkt = WlanFrameParser.parse_80211_frame(payload, -100)
            except Exception:  # noqa: BLE001
                continue
            if pkt is not None and pkt.bssid:
                bssids.add(pkt.bssid.lower())
        if "1c:63:49:79:c1:04" in bssids:
            break
    assert "1c:63:49:79:c1:04" in bssids


def test_set_channel_threads_hal():
    import asyncio
    from wifit3.chips.rtl8188ftv_dkms import chan as chan_mod
    d = _driver()
    d.transport = MagicMock()
    d.params = MagicMock()
    d.by_rate = MagicMock()
    d.hal = {"rf_chnl_val": 0xC01, "rem_cck": 1, "rem_ofdm": 0}
    calls = []
    orig = chan_mod.switch_channel
    try:
        chan_mod.switch_channel = lambda t, ch, hal, p, b, rc, ro: calls.append(
            (ch, hal["rf_chnl_val"], rc, ro))
        assert asyncio.run(d.set_channel(7)) is True
    finally:
        chan_mod.switch_channel = orig
    assert calls == [(7, 0xC01, 1, 0)]
    assert d.current_channel == 7


def test_set_channel_failure_returns_false():
    import asyncio
    d = _driver()
    d.transport = MagicMock()
    d.hal = {}
    d.params, d.by_rate = MagicMock(), MagicMock()
    from wifit3.chips.rtl8188ftv_dkms import chan as chan_mod
    orig = chan_mod.switch_channel
    try:
        def boom(*a, **k):
            raise IOError("usb gone")
        chan_mod.switch_channel = boom
        assert asyncio.run(d.set_channel(7)) is False
    finally:
        chan_mod.switch_channel = orig
