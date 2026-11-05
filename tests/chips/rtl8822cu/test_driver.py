"""RTL8822CU driver TX/ACK/active-monitor unit tests (no hardware)."""
import asyncio
import struct
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
import usb.core

import wifit3.chips.rtl8822cu.driver as drv
from wifit3.chips.rtl8822cu.chipid import ChipInfo
from wifit3.chips.rtl8822cu.constants import (
    DIS_DPD_RATE_ALL,
    HALMAC_RF_1T1R,
    HALMAC_RF_2T2R,
)
from wifit3.chips.rtl8822cu.driver import RTL8822CUDriver
from wifit3.chips.rtl8822cu.efuse import EfuseInfo
from wifit3.chips.rtl8822cu.firmware import MacHiddenRpt
from wifit3.chips.rtl8822cu.mac import set_mac_addr
from wifit3.chips.rtl8822cu.transport import EndpointLayout
from wifit3.chips.driver import FakeMacSupport
from wifit3.errors import BringUpError
from wifit3.wlan.interface import WlanInterface


def _ack_buf(ra: bytes, *, crc_err: bool = False, icv_err: bool = False) -> bytes:
    """A bulk-IN buffer with one 14-byte on-wire ACK to ``ra`` (10-B MPDU + 4-B HW FCS)."""
    desc = bytearray(24)
    flags = (int(crc_err) << 14) | (int(icv_err) << 15)
    struct.pack_into("<I", desc, 0, 14 | flags)
    mpdu = bytearray(10)
    mpdu[0] = 0xD4                              # FC: ACK control subtype
    mpdu[4:10] = ra                             # addr1 / RA
    return bytes(desc) + bytes(mpdu) + b"\x00\x00\x00\x00"


def _deauth() -> bytes:
    return b"\xc0\x00\x00\x00" + b"\x02\x11\x11\x11\x11\x11" + b"\x22" * 6 + b"\x33" * 6 + b"\x00\x00" + b"\x07\x00"


def _driver() -> RTL8822CUDriver:
    d = RTL8822CUDriver(MagicMock())
    d.transport = MagicMock()          # driver ctor wraps dev in a real transport; tests mock it
    d._parsed = []
    d.register_rx_callback(d._parsed.append)
    return d


def test_fake_mac_is_spoofable():
    assert _driver().FAKE_MAC is FakeMacSupport.SPOOFABLE


async def test_inject_frame_sends_desc_and_stamped_frame(monkeypatch):
    d = _driver()
    d._bulk_out_eps = [0x05, 0x06, 0x08]
    sent: list[bytes] = []

    def _fake_write(dev, ep, payload, timeout_ms=200):
        sent.append(bytes(payload))
        return len(payload)

    monkeypatch.setattr(drv, "write_bulk", _fake_write)
    frame = _deauth()
    assert await d.inject_frame(frame) is True
    assert len(sent) == 1
    assert len(sent[0]) == 48 + len(frame)
    assert d._tx_bytes == len(frame)
    assert sent[0][:2] == (len(frame) & 0xFFFF).to_bytes(2, "little")   # TXPKTSIZE
    assert sent[0][48:48 + 22] == frame[:22]        # header up to seq_ctl unchanged
    assert sent[0][48 + 22:48 + 24] == b"\x10\x00"  # seq_ctl stamped to seq 1 (first inject)
    assert sent[0][48 + 24:] == frame[24:]          # remainder unchanged


async def test_inject_frame_pads_usb_packet_boundary(monkeypatch):
    d = _driver()
    d._bulk_out_eps = [0x05, 0x06, 0x08]
    d.layout = EndpointLayout(0, (0x84,), (0x05, 0x06, 0x08), 512)
    sent = []
    monkeypatch.setattr(drv, "write_bulk", lambda dev, ep, payload: sent.append(payload) or len(payload))
    frame = _deauth().ljust(464, b"\x00")

    assert await d.inject_frame(frame)
    assert len(sent[0]) == 520
    assert sent[0][48:56] == bytes(8)
    assert len(sent[0][56:]) == len(frame)


async def test_inject_frame_returns_false_without_bulk_out():
    d = _driver()
    d._bulk_out_eps = []
    assert await d.inject_frame(_deauth()) is False


async def test_inject_frame_returns_false_on_short_write(monkeypatch):
    d = _driver()
    d._bulk_out_eps = [0x05]

    def _short(dev, ep, payload, timeout_ms=200):
        return len(payload) - 1

    monkeypatch.setattr(drv, "write_bulk", _short)
    assert await d.inject_frame(_deauth()) is False
    assert d._tx_bytes == 0


async def test_inject_frame_returns_false_on_usb_error(monkeypatch):
    d = _driver()
    d._bulk_out_eps = [0x05]

    def _boom(dev, ep, payload, timeout_ms=200):
        raise usb.core.USBError("boom")

    monkeypatch.setattr(drv, "write_bulk", _boom)
    assert await d.inject_frame(_deauth()) is False


def test_stamp_tx_seq_increments():
    d = _driver()
    frame = _deauth()
    a = d._stamp_tx_seq(frame)
    b = d._stamp_tx_seq(frame)
    seq_a = struct.unpack_from("<H", a, 22)[0] >> 4
    seq_b = struct.unpack_from("<H", b, 22)[0] >> 4
    assert seq_b == (seq_a + 1) & 0xFFF          # each inject gets the next seq (EN_HWSEQ=0)
    assert a[:22] == frame[:22] and a[24:] == frame[24:]   # only seq_ctl changes


def test_stamp_tx_seq_passes_short_control_frames():
    assert _driver()._stamp_tx_seq(b"\xd4\x00" + b"\x11" * 8) == b"\xd4\x00" + b"\x11" * 8


def test_set_mac_addr_programs_reg_macid():
    transport = MagicMock()
    set_mac_addr(transport, bytes.fromhex("aabbccddeeff"))
    transport.write32.assert_called_once_with(0x0610, 0xddccbbaa)
    transport.write16.assert_called_once_with(0x0614, 0xffee)


def test_set_mac_addr_rejects_bad_length():
    with pytest.raises(ValueError):
        set_mac_addr(MagicMock(), b"\x01\x02\x03")


async def test_enter_active_monitor_programs_macid_and_returns_mac():
    d = _driver()
    mac = bytes.fromhex("020000000001")
    assert await d.enter_active_monitor(mac) == mac
    d.transport.write32.assert_called_once_with(0x0610, 0x00000002)
    d.transport.write16.assert_called_once_with(0x0614, 0x0100)


async def test_exit_active_monitor_restores_efuse_mac():
    d = _driver()
    d.mac_address = "aa:bb:cc:dd:ee:ff"
    await d.enter_active_monitor(bytes.fromhex("020000000001"))
    d.transport.reset_mock()
    await d.exit_active_monitor()
    d.transport.write32.assert_called_once_with(0x0610, 0xddccbbaa)
    d.transport.write16.assert_called_once_with(0x0614, 0xffee)


async def test_wlan_interface_arms_forged_mac_via_active_monitor():
    d = _driver()
    iface = WlanInterface(d, "rtl8822cu", "test")
    armed = await iface.set_fake_mac(bytes.fromhex("020000000001"))
    assert armed == "02:00:00:00:00:01"
    d.transport.write32.assert_called_once_with(0x0610, 0x00000002)


async def test_enable_rx_acks_admits_ack_control_frames():
    d = _driver()
    d.transport.read16.return_value = 0x0000
    await d.enable_rx_acks()
    d.transport.write16.assert_called_once_with(0x06A2, 1 << 13)


async def test_disable_rx_acks_drops_ack_control_frames():
    d = _driver()
    d.transport.read16.return_value = 0xFFFF
    await d.disable_rx_acks()
    d.transport.write16.assert_called_once_with(0x06A2, 0xFFFF & ~(1 << 13))


async def test_ack_filter_restores_original_bit_after_repeated_enable():
    d = _driver()
    d.transport.read16.side_effect = [0x2001, 0x2001, 0x2002]
    await d.enable_rx_acks()
    await d.enable_rx_acks()
    await d.disable_rx_acks()
    assert d.transport.write16.call_args_list[-1].args == (0x06A2, 0x2002)


@pytest.mark.parametrize("operation", ["tune", "tx", "mac", "ack"])
async def test_close_waits_for_cancelled_runtime_executor(monkeypatch, operation):
    d = _driver()
    d._bulk_out_eps = [5]
    started = threading.Event()
    release = threading.Event()
    calls = []

    def blocked(*args, **kwargs):
        calls.append(operation)
        started.set()
        assert release.wait(3)
        if operation == "tx":
            return len(args[2])
        if operation == "ack":
            return 0

    if operation == "tune":
        monkeypatch.setattr(drv, "switch_channel", blocked)
        task = asyncio.create_task(d.set_channel(6))
    elif operation == "tx":
        monkeypatch.setattr(drv, "write_bulk", blocked)
        task = asyncio.create_task(d.inject_frame(_deauth()))
    elif operation == "mac":
        monkeypatch.setattr(drv, "set_mac_addr", blocked)
        task = asyncio.create_task(d.enter_active_monitor(b"\x02" * 6))
    else:
        d.transport.read16.side_effect = blocked
        task = asyncio.create_task(d.enable_rx_acks())

    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        closing = asyncio.create_task(d.close())
        await asyncio.sleep(0.02)
        assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await closing
        assert await d.set_channel(6) is False
        assert await d.inject_frame(_deauth()) is False
        with pytest.raises(RuntimeError):
            await d.enter_active_monitor(b"\x02" * 6)
        with pytest.raises(RuntimeError):
            await d.disable_rx_acks()
        assert calls == [operation]
    finally:
        release.set()


def test_rx_dispatch_continues_after_frame_callback_failure(monkeypatch, caplog):
    d = _driver()
    frame = _deauth()
    stat = MagicMock(crc_err=False, icv_err=False)
    monkeypatch.setattr(drv, "iter_bulk_frames", lambda *args, **kwargs: iter([
        (stat, frame, -50), (stat, frame, -51),
    ]))
    monkeypatch.setattr(drv.WlanFrameParser, "parse_80211_frame", lambda mpdu, rssi: rssi)
    callback = MagicMock(side_effect=[ValueError("bad frame"), None])
    d.register_rx_callback(callback)

    d._rx_dispatch(b"aggregate")

    assert callback.call_count == 2
    assert "RX frame dispatch failed" in caplog.text


def test_rx_dispatch_uses_configured_rx_paths(monkeypatch):
    d = _driver()
    d.trx_path.rx_ant_status = 0x02
    options = {}

    def frames(buf, **kwargs):
        options.update(kwargs)
        return []

    monkeypatch.setattr(drv, "iter_bulk_frames", frames)
    d._rx_dispatch(b"buffer")
    assert options["rx_ant_status"] == 0x02


def test_tap_counts_ack_to_our_mac():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._ack_detect_on = True
    d._rx_dispatch(_ack_buf(ra))
    assert d.acks_seen(ra) == 1
    assert d._parsed == []          # an ACK is never handed to the frame parser


def test_tap_ignores_ack_to_foreign_mac():
    d = _driver()
    ra = bytes.fromhex("aabbccddeeff")
    d._ack_detect_on = True
    d._rx_dispatch(_ack_buf(ra))
    assert d.acks_seen(ra) == 0


@pytest.mark.parametrize("error", ["crc_err", "icv_err"])
def test_tap_ignores_hardware_marked_corrupt_acks(error):
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._ack_detect_on = True
    d._rx_dispatch(_ack_buf(ra, **{error: True}))
    assert d.acks_seen(ra) == 0


def test_tap_off_by_default():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._rx_dispatch(_ack_buf(ra))    # _ack_detect_on stays False
    assert d.acks_seen(ra) == 0


async def test_set_channel_tunes_via_switch_channel(monkeypatch):
    d = _driver()
    tuned: list[int] = []
    monkeypatch.setattr(drv, "switch_channel", lambda transport, ch, txpwr: tuned.append(ch))
    assert await d.set_channel(6, scan=True) is True
    assert tuned == [6]
    assert d._current_channel == 6 and d.current_band_is_2g is True


async def test_set_channel_tracks_the_5ghz_band(monkeypatch):
    d = _driver()
    tuned: list[int] = []
    monkeypatch.setattr(drv, "switch_channel", lambda transport, ch, txpwr: tuned.append(ch))
    assert await d.set_channel(149, scan=False) is True
    assert tuned == [149]
    assert d._current_channel == 149 and d.current_band_is_2g is False


async def test_set_channel_commits_band_state_before_releasing_control_lock(monkeypatch):
    d = _driver()

    class LockProbe:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            assert d._current_channel == 149
            assert d.current_band_is_2g is False

    d._control_lock = LockProbe()
    monkeypatch.setattr(drv, "switch_channel", lambda *args: None)

    assert await d.set_channel(149) is True


async def test_set_channel_reports_failure(monkeypatch):
    d = _driver()

    def _boom(transport, ch, txpwr):
        raise ValueError("bad channel")

    monkeypatch.setattr(drv, "switch_channel", _boom)
    assert await d.set_channel(6, scan=True) is False


async def test_set_channel_rejects_unsupported_values_without_tuning(monkeypatch):
    d = _driver()
    tune = MagicMock()
    monkeypatch.setattr(drv, "switch_channel", tune)

    assert await d.set_channel(200, scan=True) is False
    tune.assert_not_called()


def test_watchdog_tick_runs_only_for_a_live_driver(monkeypatch):
    d = _driver()
    calls = []
    monkeypatch.setattr(drv, "watchdog_tick", lambda *args, **kwargs: calls.append((args, kwargs)))
    d._watchdog_state = drv.WatchdogState()

    d._run_watchdog_tick()
    assert calls == []

    d.is_warm = True
    d._current_channel = 149
    d._run_watchdog_tick()
    assert calls == [((d.transport, d._watchdog_state, 149), {"tx_bytes": 0})]


def test_watchdog_uses_monitor_tx_counter_not_bulk_rx(monkeypatch):
    d = _driver()
    d.is_warm = True
    d._watchdog_state = drv.WatchdogState()
    d._bulk_in_ep = 0x84
    d._tx_bytes = 123
    monkeypatch.setattr(drv, "read_rx_burst", lambda *args, **kwargs: b"x" * 300_000)
    tick = MagicMock()
    monkeypatch.setattr(drv, "watchdog_tick", tick)

    assert len(d._rx_read_once()) == 300_000
    d._run_watchdog_tick()

    tick.assert_called_once_with(d.transport, d._watchdog_state, 1, tx_bytes=123)


# --- bring up wiring: the EFUSE and the MAC hidden report, not literals --------

# Every bring up callee that touches hardware; the test replaces each with a recorder so only
# the driver's own wiring runs.
_BRINGUP_CALLEES = (
    "download_firmware", "load_firmware", "init_mac_cfg", "init_mac_flow_tail",
    "send_general_info", "mac_power_on", "mac_power_off", "config_rx_info", "enable_bb_rf",
    "config_bb_rf", "init_usb_cfg", "sync_rcr", "config_trx_path", "odm_dm_init", "phy_bf_init",
    "btcoex_wifionly_hw_config", "init_misc", "txpwr_idx_state",
)


def _efuse(antenna_opt: int, board_option: int = 0x01) -> EfuseInfo:
    logical = bytearray(b"\xff" * 768)
    logical[0x00:0x02] = b"\x29\x81"          # RTL_EEPROM_ID
    logical[0xB9] = 0x71                       # crystal cap
    logical[0xC1] = board_option
    logical[0xC8] = 0x00                       # tpt_mode 0 -> PWR_IDX
    logical[0xC9] = antenna_opt
    logical[0xCA] = 0x01                       # rfe_type
    return EfuseInfo(True, True, bytes(logical), b"\xff" * 512)


def _run_bringup(monkeypatch, efuse: EfuseInfo, *, ant_num: int = 2, hw_stype: int = 0x00,
                 rf_2t2r: bool = True) -> tuple[RTL8822CUDriver, dict[str, list[dict]]]:
    calls: dict[str, list[dict]] = {}
    for name in _BRINGUP_CALLEES:
        def _record(*args, _name=name, **kwargs):
            calls.setdefault(_name, []).append(kwargs)
            if _name == "config_bb_rf":
                return (0, 0)            # (cck_gi_l_bnd, cck_gi_u_bnd) the driver unpacks
        monkeypatch.setattr(drv, name, _record)
    monkeypatch.setattr(drv, "read_efuse", lambda t: efuse)
    monkeypatch.setattr(drv, "read_chip_info", lambda t: ChipInfo(
        chip_id=drv.CHIP_ID_RTL8822CU, cut=5, rf_2t2r=rf_2t2r, rom_version=0,
        raw_cfg1=0, raw_cfg2=0, raw_status1=0, chip_ver=5, raw_multifunc=0))
    monkeypatch.setattr(drv, "read_mac_hidden_rpt", lambda t: MacHiddenRpt(
        package_type=1, hw_stype=hw_stype, ant_num=ant_num))
    d = _driver()
    d._bringup(d.transport)
    return d, calls


def test_bringup_skips_cache_lookup_when_retrying(monkeypatch):
    d, _ = _run_bringup(monkeypatch, _efuse(0x33))
    get = MagicMock(side_effect=AssertionError("cache lookup during retry"))
    monkeypatch.setattr(drv.dack_cache, "get", get)
    d._skip_dack_cache = True
    d._bringup(d.transport)
    get.assert_not_called()
    assert not d._dack_restored


def test_bringup_feeds_config_trx_path_the_computed_rf_path(monkeypatch):
    """The 2T2R values that used to be literals (BB_PATH_AB / BB_PATH_AB / 2)."""
    d, calls = _run_bringup(monkeypatch, _efuse(0x33))

    assert (d.rfpath.trx_path_bmp, d.rfpath.max_tx_cnt) == (0x33, 2)
    assert calls["config_trx_path"] == [dict(tx_path=0x3, rx_path=0x3, max_tx_cnt=2, rfe_type=0x01)]


def test_a_1t1r_efuse_narrows_every_wired_field(monkeypatch):
    d, calls = _run_bringup(monkeypatch, _efuse(0x11))

    assert (d.rfpath.trx_path_bmp, d.rfpath.max_tx_cnt) == (0x11, 1)
    assert calls["config_trx_path"] == [dict(tx_path=0x1, rx_path=0x1, max_tx_cnt=1, rfe_type=0x01)]
    assert calls["send_general_info"][-1] == dict(
        rf_type=HALMAC_RF_1T1R, tx_ant=0x1, rx_ant=0x1, package=1)


def test_cycle_two_general_info_follows_the_rf_path(monkeypatch):
    _d, calls = _run_bringup(monkeypatch, _efuse(0x33))

    first, second = calls["send_general_info"]
    assert first == dict(rf_type=HALMAC_RF_1T1R, tx_ant=0x1, rx_ant=0x1, package=0)
    assert second == dict(rf_type=HALMAC_RF_2T2R, tx_ant=0x3, rx_ant=0x3, package=1)


def test_bringup_feeds_config_bb_rf_the_efuse_derived_values(monkeypatch):
    _d, calls = _run_bringup(monkeypatch, _efuse(0x33))

    assert calls["config_bb_rf"] == [dict(cut=5, rfe_type=0x01, crystal_cap=0x71,
                                          dis_dpd_rate=DIS_DPD_RATE_ALL)]
    assert calls["btcoex_wifionly_hw_config"] == [{}]


def test_bringup_leaves_bt_combo_coexistence_tables_untouched(monkeypatch, caplog):
    _d, calls = _run_bringup(monkeypatch, _efuse(0x33, board_option=0x21))

    assert "btcoex_wifionly_hw_config" not in calls
    assert "BT-combo board" in caplog.text


def test_monitor_entry_enables_tx_only_hardware_led_after_monitor_mode(monkeypatch):
    d = _driver()
    d.efuse = _efuse(0x33)
    calls: list[str] = []
    monkeypatch.setattr(drv, "arm_monitor", lambda *args: calls.append("arm"))
    monkeypatch.setattr(drv, "switch_channel", lambda *args: calls.append("tune"))
    monkeypatch.setattr(drv, "enter_monitor_mode", lambda *args: calls.append("monitor"))
    monkeypatch.setattr(drv, "enable_tx_blink", lambda *args: calls.append("led"))
    monkeypatch.setattr(d, "_log_state", lambda *args: None)
    monkeypatch.setattr(d, "_log_rx_registers", lambda *args: None)

    d._monitor_entry()

    assert calls == ["arm", "tune", "monitor", "led"]
    assert d._tx_led_enabled is True


def test_monitor_entry_does_not_claim_the_tx_led_pin_on_bt_combo(monkeypatch):
    d = _driver()
    d.efuse = _efuse(0x33, board_option=0x21)
    led = MagicMock()
    monkeypatch.setattr(drv, "arm_monitor", lambda *args: None)
    monkeypatch.setattr(drv, "switch_channel", lambda *args: None)
    monkeypatch.setattr(drv, "enter_monitor_mode", lambda *args: None)
    monkeypatch.setattr(drv, "enable_tx_blink", led)
    monkeypatch.setattr(d, "_log_state", lambda *args: None)
    monkeypatch.setattr(d, "_log_rx_registers", lambda *args: None)

    d._monitor_entry()

    led.assert_not_called()
    assert d._tx_led_enabled is False


def test_monitor_entry_continues_when_tx_led_setup_fails(monkeypatch, caplog):
    d = _driver()
    d.efuse = _efuse(0x33)
    monkeypatch.setattr(drv, "arm_monitor", lambda *args: None)
    monkeypatch.setattr(drv, "switch_channel", lambda *args: None)
    monkeypatch.setattr(drv, "enter_monitor_mode", lambda *args: None)
    monkeypatch.setattr(drv, "enable_tx_blink", lambda *args: (_ for _ in ()).throw(IOError("LED absent")))
    monkeypatch.setattr(d, "_log_state", lambda *args: None)
    monkeypatch.setattr(d, "_log_rx_registers", lambda *args: None)

    d._monitor_entry()

    assert "TX LED setup failed" in caplog.text
    assert d._tx_led_enabled is False


# --- teardown: vendor rtw_halmac_deinit_hal powers the MAC off before USB release ---


def _chip_info(*, raw_cfg1: int = 0) -> ChipInfo:
    return ChipInfo(
        chip_id=drv.CHIP_ID_RTL8822CU, cut=5, rf_2t2r=True, rom_version=0,
        raw_cfg1=raw_cfg1, raw_cfg2=0, raw_status1=0, chip_ver=5, raw_multifunc=0)


async def test_connect_rejects_unsupported_bulk_out_layout_before_claiming(monkeypatch):
    d = _driver()
    d.transport.endpoints.return_value = EndpointLayout(0, (0x81,), (0x05, 0x06))
    claim = MagicMock()
    monkeypatch.setattr(d, "_claim", claim)

    with pytest.raises(BringUpError, match="3 bulk-OUT") as exc_info:
        await d.connect()

    assert exc_info.value.stage == "endpoints"
    claim.assert_not_called()


async def test_connect_starts_the_phydm_watchdog_after_monitor_entry(monkeypatch):
    d = _driver()
    d.transport.endpoints.return_value = EndpointLayout(0, (0x81,), (0x05, 0x06, 0x08))
    monkeypatch.setattr(d, "_claim", lambda: setattr(d, "_claimed", True))
    monkeypatch.setattr(d, "_bringup", lambda *args: None)
    monkeypatch.setattr(d, "_monitor_entry", lambda: None)

    reader = MagicMock()
    monkeypatch.setattr(drv, "RxReaderThread", lambda *args, **kwargs: reader)
    tasks: list[str] = []

    def _create_task(coro, *, name):
        tasks.append(name)
        coro.close()
        return MagicMock()

    monkeypatch.setattr(drv.asyncio, "create_task", _create_task)

    assert await d.connect() is True
    reader.start.assert_called_once()
    assert d.is_warm is True
    assert d._watchdog_state is not None
    assert tasks == ["rtl8822cu-watchdog"]


async def test_connect_cleans_up_a_failed_bringup(monkeypatch):
    d = _driver()
    d.transport.endpoints.return_value = EndpointLayout(0, (0x81,), (0x05, 0x06, 0x08))
    calls: list[str] = []

    def _claim():
        d._claimed = True

    def _bringup(*args):
        raise IOError("firmware did not start")

    monkeypatch.setattr(d, "_claim", _claim)
    monkeypatch.setattr(d, "_bringup", _bringup)
    monkeypatch.setattr(drv.usb.util, "release_interface",
                        lambda dev, interface: calls.append("release"))
    monkeypatch.setattr(drv.usb.util, "dispose_resources",
                        lambda dev: calls.append("dispose"))

    with pytest.raises(BringUpError, match="firmware did not start") as exc_info:
        await d.connect()

    assert exc_info.value.stage == "firmware"
    assert calls == ["release", "dispose"]
    assert d._claimed is False
    assert d._bulk_in_ep is None
    assert d._bulk_out_eps == []


@pytest.mark.parametrize("stage", ["_bringup", "_monitor_entry"])
@pytest.mark.parametrize("cancel_connect", [True, False])
async def test_connect_waits_for_executor_before_cleanup(monkeypatch, stage, cancel_connect):
    d = _driver()
    d.transport.endpoints.return_value = EndpointLayout(0, (0x81,), (0x05, 0x06, 0x08))
    started = threading.Event()
    finish = threading.Event()
    calls = []

    def blocked(*args):
        started.set()
        assert finish.wait(5)
        calls.append("executor_done")

    monkeypatch.setattr(d, "_claim", lambda: setattr(d, "_claimed", True))
    monkeypatch.setattr(d, "_bringup", lambda *args: None)
    monkeypatch.setattr(d, "_monitor_entry", lambda: None)
    monkeypatch.setattr(d, stage, blocked)
    monkeypatch.setattr(drv.usb.util, "release_interface", lambda *args: calls.append("release"))
    monkeypatch.setattr(drv.usb.util, "dispose_resources", lambda *args: None)
    reader = MagicMock(stop=AsyncMock())
    monkeypatch.setattr(drv, "RxReaderThread", lambda *args, **kwargs: reader)
    connect = asyncio.create_task(d.connect())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        if cancel_connect:
            connect.cancel()
            await asyncio.sleep(0)
            connect.cancel()
        close = asyncio.create_task(d.close())
        await asyncio.sleep(0.01)
        assert calls == []
        assert not close.done()
    finally:
        finish.set()
    if cancel_connect:
        with pytest.raises(asyncio.CancelledError):
            await connect
    else:
        assert await connect
    await close
    assert calls == ["executor_done", "release"]
    assert not d._claimed


async def test_connect_cleans_up_unexpected_exception(monkeypatch):
    d = _driver()
    d.transport.endpoints.return_value = EndpointLayout(0, (0x81,), (0x05, 0x06, 0x08))
    monkeypatch.setattr(d, "_claim", lambda: setattr(d, "_claimed", True))
    monkeypatch.setattr(d, "_bringup", MagicMock(side_effect=KeyError("unexpected")))
    release = MagicMock()
    monkeypatch.setattr(drv.usb.util, "release_interface", release)
    monkeypatch.setattr(drv.usb.util, "dispose_resources", MagicMock())

    with pytest.raises(KeyError, match="unexpected"):
        await d.connect()
    release.assert_called_once()
    assert not d._claimed


@pytest.mark.parametrize("error", [drv.DackRestoreError, drv.DackCalibrationError])
async def test_dack_failure_reinitializes_once_without_cache(monkeypatch, error):
    d = _driver()
    d.transport.endpoints.return_value = EndpointLayout(0, (0x81,), (5, 6, 8))
    calls = []
    old_states = (d.trx_path, d.h2c, d.power_trim, d.txgapk, d.phydm)

    def claim():
        calls.append("claim")
        d._claimed = True

    def bringup(*args):
        calls.append("bringup")
        if calls.count("bringup") == 1:
            d.chip_info = _chip_info()
            d._dack_cache_key = ("key",)
            raise error("dack failure")
        assert d._skip_dack_cache
        assert all(a is not b for a, b in zip(old_states, (d.trx_path, d.h2c, d.power_trim, d.txgapk, d.phydm)))
        assert d._bulk_in_ep == 0x81

    monkeypatch.setattr(d, "_claim", claim)
    monkeypatch.setattr(d, "_bringup", bringup)
    monkeypatch.setattr(d, "_monitor_entry", lambda: calls.append("monitor"))
    monkeypatch.setattr(drv.dack_cache, "discard", lambda key: calls.append(("discard", key)))
    monkeypatch.setattr(drv, "mac_power_off", lambda *args, **kwargs: calls.append("power_off"))
    monkeypatch.setattr(drv.usb.util, "release_interface", lambda *args: calls.append("release"))
    monkeypatch.setattr(drv.usb.util, "dispose_resources", lambda *args: calls.append("dispose"))
    monkeypatch.setattr(drv, "RxReaderThread", lambda *args, **kwargs: MagicMock(stop=AsyncMock()))
    assert await d.connect()
    assert calls == ["claim", "bringup", ("discard", ("key",)), "power_off", "release", "dispose",
                     "claim", "bringup", "monitor"]
    await d.close()


@pytest.mark.parametrize("failure", ["power", "release", "dispose", "missing_chip"])
async def test_dack_retry_fails_closed_when_reset_fails(monkeypatch, failure):
    d = _driver()
    d.transport.endpoints.return_value = EndpointLayout(0, (0x81,), (5, 6, 8))
    claim = MagicMock(side_effect=lambda: setattr(d, "_claimed", True))

    def bringup(*args):
        if failure != "missing_chip":
            d.chip_info = _chip_info()
        raise drv.DackCalibrationError("dack failure")

    def operation(name):
        if failure == name:
            raise usb.core.USBError(name)

    monkeypatch.setattr(d, "_claim", claim)
    monkeypatch.setattr(d, "_bringup", bringup)
    monkeypatch.setattr(drv, "mac_power_off", lambda *args, **kwargs: operation("power"))
    monkeypatch.setattr(drv.usb.util, "release_interface", lambda *args: operation("release"))
    monkeypatch.setattr(drv.usb.util, "dispose_resources", lambda *args: operation("dispose"))
    with pytest.raises(BringUpError, match="reset failed"):
        await d.connect()
    with pytest.raises(BringUpError, match="replug"):
        await d.connect()
    claim.assert_called_once()


async def test_dack_retry_only_once(monkeypatch):
    d = _driver()
    d.transport.endpoints.return_value = EndpointLayout(0, (0x81,), (5, 6, 8))
    claim = MagicMock(side_effect=lambda: setattr(d, "_claimed", True))

    def bringup(*args):
        d.chip_info = _chip_info()
        raise drv.DackCalibrationError("still failing")

    monkeypatch.setattr(d, "_claim", claim)
    monkeypatch.setattr(d, "_bringup", bringup)
    monkeypatch.setattr(drv, "mac_power_off", MagicMock())
    monkeypatch.setattr(drv.usb.util, "release_interface", MagicMock())
    monkeypatch.setattr(drv.usb.util, "dispose_resources", MagicMock())
    with pytest.raises(BringUpError, match="still failing"):
        await d.connect()
    assert claim.call_count == 2


async def test_dack_retry_cancellation_waits_for_reset(monkeypatch):
    d = _driver()
    d.transport.endpoints.return_value = EndpointLayout(0, (0x81,), (5, 6, 8))
    started, finish = threading.Event(), threading.Event()
    claim = MagicMock(side_effect=lambda: setattr(d, "_claimed", True))

    def bringup(*args):
        d.chip_info = _chip_info()
        raise drv.DackRestoreError("restore failed")

    def power_off(*args, **kwargs):
        started.set()
        assert finish.wait(5)

    monkeypatch.setattr(d, "_claim", claim)
    monkeypatch.setattr(d, "_bringup", bringup)
    monkeypatch.setattr(drv, "mac_power_off", power_off)
    monkeypatch.setattr(drv.usb.util, "release_interface", MagicMock())
    monkeypatch.setattr(drv.usb.util, "dispose_resources", MagicMock())
    task = asyncio.create_task(d.connect())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    claim.assert_called_once()
    assert not d._claimed


async def test_close_powers_mac_off_before_releasing_usb(monkeypatch):
    d = _driver()
    d._claimed = True
    d.is_warm = True
    d._bulk_in_ep = 0x81
    d._bulk_out_eps = [0x05]
    d.dack.dack_en = True
    d.chip_info = _chip_info(raw_cfg1=3 << 12)
    reader = MagicMock(stop=AsyncMock())
    d._rx_reader = reader
    calls: list[tuple] = []

    def _power_off(transport, *, cut_mask):
        calls.append(("power_off", transport, cut_mask))

    monkeypatch.setattr(drv, "mac_power_off", _power_off)
    monkeypatch.setattr(drv.usb.util, "release_interface",
                        lambda dev, interface: calls.append(("release", dev, interface)))
    monkeypatch.setattr(drv.usb.util, "dispose_resources",
                        lambda dev: calls.append(("dispose", dev)))

    await d.close()

    reader.stop.assert_awaited_once()
    assert calls == [
        ("power_off", d.transport, 1 << 4),
        ("release", d.dev, 0),
        ("dispose", d.dev),
    ]
    assert d._claimed is False
    assert d._rx_reader is None
    assert d.is_warm is False
    assert d._bulk_in_ep is None
    assert d._bulk_out_eps == []
    assert d.dack.dack_en is False


async def test_close_turns_off_the_tx_led_before_mac_power_off(monkeypatch):
    d = _driver()
    d._claimed = True
    d._tx_led_enabled = True
    d.chip_info = _chip_info()
    calls: list[str] = []

    monkeypatch.setattr(drv, "disable_tx_blink", lambda transport: calls.append("led_off"))
    monkeypatch.setattr(drv, "mac_power_off", lambda transport, *, cut_mask: calls.append("power_off"))
    monkeypatch.setattr(drv.usb.util, "release_interface", lambda dev, interface: None)
    monkeypatch.setattr(drv.usb.util, "dispose_resources", lambda dev: None)

    await d.close()

    assert calls == ["led_off", "power_off"]
    assert d._tx_led_enabled is False


async def test_close_releases_usb_when_mac_power_off_fails(monkeypatch):
    d = _driver()
    d._claimed = True
    d.chip_info = _chip_info()
    calls: list[str] = []

    def _power_off(transport, *, cut_mask):
        calls.append("power_off")
        raise IOError("device gone")

    monkeypatch.setattr(drv, "mac_power_off", _power_off)
    monkeypatch.setattr(drv.usb.util, "release_interface",
                        lambda dev, interface: calls.append("release"))
    monkeypatch.setattr(drv.usb.util, "dispose_resources",
                        lambda dev: calls.append("dispose"))

    await d.close()

    assert calls == ["power_off", "release", "dispose"]
    assert d._claimed is False
