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


def _rx_desc(n: int) -> bytes:
    import struct
    return struct.pack("<IIIIII", n, 0, 0, 0, 0, 0)


def test_rx_dispatch_control_frames_parse_to_none():
    from wifit3.chips.rtl8188ftv_dkms import rx as rx_mod
    from wifit3.dot11.parser import WlanFrameParser
    # Synthetic bulk-IN buffer: two control frames, 8-byte aligned entries
    # (recorded frame 3747 held the same b4/c4 pair at lengths 20/14).
    p1 = b"\xb4\x00" + bytes(18)
    p2 = b"\xc4\x00" + bytes(12)
    buf = _rx_desc(20) + p1 + bytes(4) + _rx_desc(14) + p2 + bytes(2)
    pkts = list(rx_mod.iter_rx(buf))
    assert [len(p) for _, p in pkts] == [20, 14]
    assert [p[:2] for _, p in pkts] == [b"\xb4\x00", b"\xc4\x00"]
    assert all(WlanFrameParser.parse_80211_frame(p, -100) is None
               for _, p in pkts)


def test_rx_dispatch_beacon_bssid():
    from wifit3.chips.rtl8188ftv_dkms import rx as rx_mod
    from wifit3.dot11.parser import WlanFrameParser
    # Synthetic beacon for the log-known AP: 24B header + 12B fixed params
    # + SSID IE, wrapped in one RX descriptor entry.
    bssid = bytes.fromhex("1c634979c104")
    hdr = (b"\x80\x00\x00\x00" + b"\xff" * 6 + bssid + bssid + b"\x00\x00")
    beacon = hdr + bytes(12) + b"\x00\x04TEST"
    buf = _rx_desc(len(beacon)) + beacon
    bssids = set()
    for attrib, payload in rx_mod.iter_rx(buf):
        if attrib["c2h"] or len(payload) < 24:
            continue
        try:
            pkt = WlanFrameParser.parse_80211_frame(payload, -100)
        except Exception:  # noqa: BLE001
            continue
        if pkt is not None and pkt.bssid:
            bssids.add(pkt.bssid.lower())
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


def test_stamp_tx_seq_uses_mgnt_seq_without_advancing():
    import struct
    d = _driver()
    d.hal = {"mgnt_seq": 41}
    frame = bytes.fromhex("c0000000") + bytes(20)
    out = d._stamp_tx_seq(frame)
    assert out[22:24] == struct.pack("<H", (41 << 4) & 0xFFF0)
    assert out[:22] == frame[:22] and out[24:] == frame[24:]
    assert d.hal["mgnt_seq"] == 41


def test_inject_frame_sends_mgnt_urb_and_advances_seq():
    import asyncio
    from wifit3.chips.rtl8188ftv_dkms import tx as tx_mod
    d = _driver()
    d.transport = MagicMock()
    d.hal = {"mgnt_seq": 5}
    ap = bytes.fromhex("1c634979c104")
    card = bytes.fromhex("44efbf1f9dfb")
    frame = tx_mod.stamp_seqnum(tx_mod.build_deauth_frame(ap, card, ap, 0), 5)
    assert asyncio.run(d._inject_frame(frame)) is True
    data = d.transport.bulk_out.call_args[0][0]
    assert data[40:] == frame
    assert data[:40] == tx_mod.build_mgnt_desc(size=len(frame), seq=5,
                                               bmc=False)
    assert d.hal["mgnt_seq"] == 6


def test_inject_frame_sends_data_without_consuming_seq_on_reject():
    import asyncio
    d = _driver()
    d.transport = MagicMock()
    d.hal = {"mgnt_seq": 5}
    qos = (bytes((0x88, 0x01, 0x00, 0x00)) + bytes(18) + bytes((0x50, 0x00))
           + bytes(36))
    assert asyncio.run(d._inject_frame(qos)) is True
    assert d.hal["mgnt_seq"] == 6
    assert asyncio.run(d._inject_frame(b"\xD4" + bytes(30))) is False
    assert asyncio.run(d._inject_frame(b"\xc0" * 10)) is False
    assert d.transport.bulk_out.call_count == 1
    assert d.hal["mgnt_seq"] == 6


def test_rx_dispatch_records_ack_with_and_without_fcs():
    """The monitor RCR appends FCS (BIT31), so an ACK arrives as 14 bytes; the
    tap must record both the bare 10-byte ACK and the FCS-appended 14-byte one."""
    import struct
    ra = bytes.fromhex("02b0b0000001")

    def buf_for(ack):                              # 24-byte desc (pkt_len only) + ack
        return struct.pack("<I", len(ack)) + bytes(20) + ack

    ack10 = bytes([0xD4, 0, 0, 0]) + ra           # FC=ACK, dur, RA
    ack14 = ack10 + bytes(4)                       # + appended FCS
    for ack in (ack10, ack14):
        d = _driver()
        d._ack_detect_on = True
        d._our_tx_macs.add(ra)
        d._rx_dispatch(buf_for(ack))
        assert d.acks_seen(ra) == 1

    d = _driver()                                  # tap disarmed -> no tally
    d._our_tx_macs.add(ra)
    d._rx_dispatch(buf_for(ack14))
    assert d.acks_seen(ra) == 0


def test_watchdog_close_with_no_task_started():
    import asyncio
    d = _driver()
    assert d._watchdog_task is None
    assert asyncio.run(d.close()) is None


def test_watchdog_loop_ticks_shared_hal_then_stops():
    import asyncio
    from wifit3.chips.rtl8188ftv_dkms import dm as dm_mod
    from wifit3.chips.rtl8188ftv_dkms import driver as drv_mod
    calls = []
    orig_tick, orig_period = dm_mod.watchdog_tick, drv_mod.WATCHDOG_PERIOD_S
    try:
        dm_mod.watchdog_tick = lambda t, st: calls.append(st) or {"all": 12}
        drv_mod.WATCHDOG_PERIOD_S = 0.01

        async def run():
            d = _driver()
            d.transport = MagicMock()
            d.hal = {"rem_cck": 1, "rem_ofdm": 0}
            task = asyncio.create_task(d._watchdog_loop())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return d

        d = asyncio.run(run())
        assert len(calls) >= 1
        assert all(st is d.hal for st in calls)
    finally:
        dm_mod.watchdog_tick = orig_tick
        drv_mod.WATCHDOG_PERIOD_S = orig_period


def test_watchdog_loop_skips_faulty_tick():
    import asyncio
    from wifit3.chips.rtl8188ftv_dkms import dm as dm_mod
    from wifit3.chips.rtl8188ftv_dkms import driver as drv_mod
    calls = []
    results = [IOError("usb gone"), {"all": 3}]

    def flaky(t, st):
        calls.append(st)
        res = results.pop(0) if results else {"all": 3}
        if isinstance(res, Exception):
            raise res
        return res

    orig_tick, orig_period = dm_mod.watchdog_tick, drv_mod.WATCHDOG_PERIOD_S
    try:
        dm_mod.watchdog_tick = flaky
        drv_mod.WATCHDOG_PERIOD_S = 0.01

        async def run():
            d = _driver()
            d.transport = MagicMock()
            d.hal = {"rem_cck": 0, "rem_ofdm": 0}
            task = asyncio.create_task(d._watchdog_loop())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run())
        assert len(calls) >= 2  # loop survived the faulty tick
    finally:
        dm_mod.watchdog_tick = orig_tick
        drv_mod.WATCHDOG_PERIOD_S = orig_period


def _stubbed_bring_up_modules(monkeypatch, calls):
    """Stub every module function `_bring_up` touches; record FW#C2H/off."""
    import types
    from wifit3.chips.rtl8188ftv_dkms import driver as drv_mod
    params = types.SimpleNamespace(
        vid=0x0BDA, pid=0xF179, mac=bytes.fromhex("44efbf1f9dfb"),
        thermal=0x1A, crystal=0x1D)

    def noop(*a, **k):
        return None

    stubs = {
        "info_mod": {"read_chip_version": lambda t: object(),
                     "is_smic": lambda v: True},
        "prom_mod": {"read_adapter_info": lambda t, s: (params, b"")},
        "power_mod": {"power_on": lambda *a, **k: True,
                      "card_disable": lambda *a, **k: True,
                      "check_powered": lambda t: (0, 0)},
        "firmware_mod": {"load_firmware_blob": lambda: b"blob",
                         "download_firmware": lambda t, b: calls.append("fw") or (4, 0, 0x88F1)},
        "c2h_mod": {"request_hidden_report": lambda t: calls.append("c2h-req"),
                    "collect_hidden_report": lambda t: calls.append("c2h-get") or (0x19, b"")},
        "llt_mod": {"init_llt": lambda t: True, "enable_tx_report": noop},
        "txpower_mod": {"load_default_pg_tables": lambda: object(),
                        "set_level": noop},
        "mac_mod": {"init_antenna_selection": noop, "mac_config": noop},
        "bb_mod": {"bb_config": noop},
        "rf_mod": {"rf_config": noop},
        "chan_mod": {"tune_20": lambda t, ch, v, hal: 0xC01,
                     "switch_channel": noop},
        "sec_mod": {"invalidate_cam_all": noop},
        "dm_mod": {"common_info_self_init": noop, "dig_init_igi": lambda t: 0x20,
                   "nhm_init": noop, "adaptivity_init": noop,
                   "cfo_init_atc": noop, "thermal_swing_index": noop,
                   "tracking_init_second": noop},
        "cal_mod": {"lc_calibrate": noop},
        "iqk_mod": {"iq_calibrate": lambda t: {"final": 0xFF}},
        "track_mod": {"thermal_trigger": noop, "kfree_gain_offset": noop,
                      "tracking_init_state": lambda e: {}},
        "queues_mod": {"init_queue_reserved_page": noop,
                       "init_tx_buffer_boundary": noop,
                       "init_queue_priority": noop,
                       "init_page_boundary": noop,
                       "init_transfer_page_size": noop,
                       "init_driver_info_size": noop,
                       "init_macaddr": noop,
                       "init_network_type": noop,
                       "init_wmac_setting": noop,
                       "init_adaptive_ctrl": noop,
                       "init_edca": noop,
                       "init_rate_fallback": noop,
                       "init_retry_function": noop},
        "misc_mod": {"init_beacon_params": noop,
                     "init_burst": noop,
                     "agg_tx_update": noop,
                     "agg_rx_update": noop,
                     "init_hw_led": noop,
                     "drop_incorrect_bulk_out": noop,
                     "mcast2uni_lifetime": noop,
                     "turn_on_block": noop,
                     "misc11_tail": noop,
                     "init_gpio_setting": noop,
                     "hal_init_tail": noop},
        "mode_mod": {"set_station_opmode": noop, "enter_monitor": noop},
    }
    for mod_name, fns in stubs.items():
        mod = getattr(drv_mod, mod_name)
        for fn_name, fn in fns.items():
            monkeypatch.setattr(mod, fn_name, fn)


def test_bring_up_warm_skips_fw1_probe_tail(monkeypatch):
    import asyncio
    from wifit3.chips.rtl8188ftv_dkms import power as power_mod
    calls = []
    _stubbed_bring_up_modules(monkeypatch, calls)
    monkeypatch.setattr(power_mod, "is_chip_warm", lambda t: True)
    monkeypatch.setattr(power_mod, "warm_state", lambda t: (0xC6, 0x06FF))
    d = _driver()
    d.transport = MagicMock()
    assert asyncio.run(d._bring_up(lambda p, m: None)) is True
    assert calls.count("fw") == 1
    assert "c2h-req" not in calls and "c2h-get" not in calls
    assert d.current_channel == 1 and d.hal["channel"] == 1


def test_bring_up_cold_runs_fw1_probe_tail(monkeypatch):
    import asyncio
    from wifit3.chips.rtl8188ftv_dkms import power as power_mod
    calls = []
    _stubbed_bring_up_modules(monkeypatch, calls)
    monkeypatch.setattr(power_mod, "is_chip_warm", lambda t: False)
    d = _driver()
    d.transport = MagicMock()
    assert asyncio.run(d._bring_up(lambda p, m: None)) is True
    assert calls.count("fw") == 2
    assert calls.index("c2h-req") < calls.index("c2h-get")
