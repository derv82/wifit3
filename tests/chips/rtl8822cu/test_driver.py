"""RTL8822CU driver TX/ACK/active-monitor unit tests (no hardware)."""
import struct
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
    assert sent[0][:2] == (len(frame) & 0xFFFF).to_bytes(2, "little")   # TXPKTSIZE
    assert sent[0][48:48 + 22] == frame[:22]        # header up to seq_ctl unchanged
    assert sent[0][48 + 22:48 + 24] == b"\x10\x00"  # seq_ctl stamped to seq 1 (first inject)
    assert sent[0][48 + 24:] == frame[24:]          # remainder unchanged


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
    monkeypatch.setattr(drv, "watchdog_tick", lambda *args: calls.append(args))
    d._watchdog_state = MagicMock()

    d._run_watchdog_tick()
    assert calls == []

    d.is_warm = True
    d._current_channel = 149
    d._run_watchdog_tick()
    assert calls == [(d.transport, d._watchdog_state, 149)]


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
