"""Driver wiring: class surface, CCK-AGC snapshot, RX dispatch / ACK tap.

No hardware: the transport is replaced by the recording mock and the USB handle
stays a MagicMock (nothing is written to it on these paths).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import wifit3.chips.rtl8192eu.driver as drv
import wifit3.chips.rtl8192eu.mac as mac
from wifit3.chips.driver import FakeMacSupport
from wifit3.chips.rtl8192eu.constants import (
    REG_FPGA0_XA_HSSI_PARM2,
    REG_MACID,
    TXDESC_QUEUE_BE,
    TXDESC_QUEUE_BEACON,
    TXDESC_QUEUE_BK,
    TXDESC_QUEUE_MGNT,
    TXDESC_QUEUE_VO,
)
from wifit3.chips.rtl8192eu.driver import RTL8192EUDriver
from wifit3.chips.rtl8192eu.efuse import EfuseDefaults
from wifit3.chips.rtl8192eu.mac import TxQueues
from wifit3.errors import BringUpError

from tests.chips.rtl8192eu._mock import MockTransport

OUR_MAC = bytes.fromhex("020000000001")
REAL_MAC = bytes.fromhex("aabbccddeeff")
BSSID = bytes.fromhex("c006c3c9ec0b")
CLIENT = bytes.fromhex("d03f27223eef")


def deauth() -> bytes:
    frame = bytearray(26)
    frame[0] = 0xC0
    frame[4:10] = CLIENT
    frame[10:16] = BSSID
    frame[16:22] = BSSID
    return bytes(frame)


def plain_data() -> bytes:
    frame = bytearray(24)
    frame[0] = 0x08
    frame[1] = 0x01                      # ToDS
    frame[4:10] = BSSID
    frame[10:16] = CLIENT
    frame[16:22] = BSSID
    return bytes(frame)


def qos_data(tid: int) -> bytes:
    frame = bytearray(plain_data() + b"\x00\x00")
    frame[0] = 0x88                      # QoS Data
    frame[24] = tid
    return bytes(frame)


def beacon() -> bytes:
    frame = bytearray(80)
    frame[0] = 0x80
    frame[4:10] = b"\xff" * 6
    frame[10:16] = BSSID
    frame[16:22] = BSSID
    return bytes(frame)


def make_driver() -> RTL8192EUDriver:
    return RTL8192EUDriver(MagicMock())


def test_supported_channels_are_2g_1_to_14():
    assert RTL8192EUDriver.SUPPORTED_CHANNELS == list(range(1, 15))


def test_driver_reports_active_monitor_as_spoofable():
    assert RTL8192EUDriver.FAKE_MAC is FakeMacSupport.SPOOFABLE


def test_from_usb_device_wraps_the_usb_handle():
    dev = MagicMock()
    driver = RTL8192EUDriver.from_usb_device(dev, MagicMock())
    assert isinstance(driver, RTL8192EUDriver)
    assert driver.dev is dev
    assert driver.current_channel is None
    assert driver.mac_address is None


def test_stamp_tx_seq_is_identity():
    driver = make_driver()
    frame = b"\xc0\x00\x00\x00" + b"\xff" * 6 + OUR_MAC + b"\x00" * 8
    assert driver._stamp_tx_seq(frame) is frame


@pytest.mark.parametrize(("hssi_parm2", "expected"), [(1 << 9, 1), (0, 0)])
def test_cck_agc_report_type_reads_hssi_parm2_bit9(hssi_parm2, expected):
    driver = make_driver()
    transport = MockTransport()
    transport.seed(REG_FPGA0_XA_HSSI_PARM2, hssi_parm2)
    driver.transport = transport

    driver._read_cck_agc_report_type()

    assert driver._cck_agc_report_type == expected


# ---- TX endpoint selection (queue -> bulk-OUT pipe) ---------------------


def _endpoints() -> drv.Endpoints:
    return drv.Endpoints(bulk_in=[0x84], bulk_out=[0x05, 0x06, 0x08], interrupt=[])


def _wire_driver(monkeypatch, driver) -> list:
    """Point the driver at the 3-endpoint bench card and record every send."""
    sent: list = []
    driver._bulk_out_eps = [0x05, 0x06, 0x08]
    driver._tx_lanes = mac.tx_lane_endpoints(TxQueues(high=True, normal=True, low=True))
    monkeypatch.setattr(drv, "send_frame", lambda dev, ep, frame, **kw: sent.append((ep, kw["queue"])))
    return sent


async def test_inject_frame_sends_mgmt_on_the_high_lane(monkeypatch):
    driver = make_driver()
    sent = _wire_driver(monkeypatch, driver)

    assert await driver._inject_frame(deauth()) is True

    # `priv->pipe_out[TXDESC_QUEUE_MGNT] = out_ep[mgp=0]` (`core.c:2705`).
    assert sent == [(0x05, TXDESC_QUEUE_MGNT)]


async def test_inject_frame_sends_data_on_the_per_ac_lane(monkeypatch):
    driver = make_driver()
    sent = _wire_driver(monkeypatch, driver)

    assert await driver._inject_frame(qos_data(tid=6)) is True     # VO -> HIGH lane
    assert await driver._inject_frame(qos_data(tid=1)) is True     # BK -> LOW lane
    assert await driver._inject_frame(plain_data()) is True        # BE -> LOW lane

    # `voq`/`viq` = HIGH/NORMAL, `beq`/`bkq` = LOW on a 3-endpoint card
    # (`core.c:2609-2611`); the HIGH/NORMAL/LOW lanes are out_ep[0]/[1]/[2]
    # (`core.c:2688-2710`).
    assert sent == [
        (0x05, TXDESC_QUEUE_VO),
        (0x08, TXDESC_QUEUE_BK),
        (0x08, TXDESC_QUEUE_BE),
    ]


async def test_inject_frame_sends_a_beacon_on_the_first_pipe(monkeypatch):
    driver = make_driver()
    sent = _wire_driver(monkeypatch, driver)

    assert await driver._inject_frame(beacon()) is True

    assert sent == [(0x05, TXDESC_QUEUE_BEACON)]


async def test_inject_frame_probes_when_no_endpoints_are_cached(monkeypatch):
    driver = make_driver()
    sent = _wire_driver(monkeypatch, driver)
    driver._bulk_out_eps = []
    driver._tx_lanes = None
    monkeypatch.setattr(drv, "probe_endpoints", lambda dev, **kw: _endpoints())
    monkeypatch.setattr(drv, "config_endpoints", lambda t, n: TxQueues(True, True, True))

    assert await driver._inject_frame(deauth()) is True

    assert driver._bulk_out_eps == [0x05, 0x06, 0x08]
    assert sent == [(0x05, TXDESC_QUEUE_MGNT)]


async def test_inject_frame_fails_without_a_bulk_out_endpoint(monkeypatch):
    driver = make_driver()
    driver._bulk_out_eps = []
    monkeypatch.setattr(
        drv, "probe_endpoints",
        lambda dev, **kw: drv.Endpoints(bulk_in=[0x84], bulk_out=[], interrupt=[]),
    )

    assert await driver._inject_frame(deauth()) is False


# ---- set_channel (power-then-channel kernel order) ----------------------


async def test_set_channel_sets_tx_power_before_retuning(monkeypatch):
    driver = make_driver()
    order: list[str] = []
    monkeypatch.setattr(
        drv, "set_tx_power",
        lambda t, ch, efuse: order.append("set_tx_power"),
    )
    monkeypatch.setattr(
        drv, "set_channel_2g_20mhz",
        lambda t, ch: order.append("set_channel_2g_20mhz"),
    )

    assert await driver.set_channel(6) is True

    # `rtl8xxxu_config` re-applies the per-group TX power before retuning
    # (`core.c:6791-6793`).
    assert order == ["set_tx_power", "set_channel_2g_20mhz"]
    assert driver.current_channel == 6


def _ack_mpdu(ra: bytes) -> bytes:
    """A 10-byte FCS-stripped ACK (FC 0xD4, duration, RA)."""
    return b"\xd4\x00\x00\x00" + ra


def test_rx_dispatch_counts_acks_to_our_macs(monkeypatch):
    driver = make_driver()
    driver._parsed = []
    driver.register_rx_callback(driver._parsed.append)
    driver._our_tx_macs.add(OUR_MAC)
    driver._ack_detect_on = True
    monkeypatch.setattr(
        drv, "iter_bulk_frames", lambda buf, **kw: [(None, _ack_mpdu(OUR_MAC), -40)]
    )

    driver._rx_dispatch(b"BULK")

    assert driver.acks_seen(OUR_MAC) == 1
    assert driver._parsed == []          # ACKs never reach the frame parser


def test_rx_dispatch_ignores_acks_to_foreign_macs(monkeypatch):
    driver = make_driver()
    driver._ack_detect_on = True
    monkeypatch.setattr(
        drv, "iter_bulk_frames",
        lambda buf, **kw: [(None, _ack_mpdu(bytes.fromhex("aabbccddeeff")), -40)],
    )
    driver._rx_dispatch(b"BULK")
    assert driver.acks_seen(bytes.fromhex("aabbccddeeff")) == 0


def test_rx_dispatch_parses_frames_and_fires_the_callback(monkeypatch):
    driver = make_driver()
    monkeypatch.setattr(
        drv, "iter_bulk_frames",
        lambda buf, **kw: [(None, b"\x80mpdu", -42)] if buf == b"BULK" else [],
    )
    monkeypatch.setattr(
        drv.WlanFrameParser, "parse_80211_frame",
        staticmethod(lambda mpdu, rssi: {"type": "beacon", "rssi": rssi}),
    )
    got = []
    driver.register_rx_callback(got.append)

    driver._rx_dispatch(b"BULK")

    assert got == [{"type": "beacon", "rssi": -42}]


def test_rx_dispatch_without_a_callback_is_safe(monkeypatch):
    driver = make_driver()
    monkeypatch.setattr(drv, "iter_bulk_frames", lambda buf, **kw: [(None, b"x", -1)])
    driver._rx_dispatch(b"BULK")          # no callback -> must not raise


# ---- active monitor (REG_MACID re-point) --------------------------------


def _mac_writes(t: MockTransport) -> list[tuple[str, int, int]]:
    """The 8-bit writes landing in REG_MACID..REG_MACID+5, in order."""
    return [w for w in t.writes if w[0] == "w8" and REG_MACID <= w[1] < REG_MACID + 6]


async def test_enter_active_monitor_captures_the_old_mac_then_writes_the_forged_one():
    driver = make_driver()
    t = MockTransport()
    t.seed(REG_MACID, int.from_bytes(REAL_MAC, "little"), size=6)
    driver.transport = t

    returned = await driver.enter_active_monitor(OUR_MAC)

    assert returned == OUR_MAC
    # The previous 6 bytes are read (in kernel order) before the forged write.
    assert t.reads[:6] == [("r8", REG_MACID + i) for i in range(6)]
    assert _mac_writes(t) == [("w8", REG_MACID + i, OUR_MAC[i]) for i in range(6)]
    assert t._load(REG_MACID, 6) == int.from_bytes(OUR_MAC, "little")


async def test_exit_active_monitor_restores_the_previous_bytes():
    driver = make_driver()
    t = MockTransport()
    t.seed(REG_MACID, int.from_bytes(REAL_MAC, "little"), size=6)
    driver.transport = t
    await driver.enter_active_monitor(OUR_MAC)

    t.writes.clear()
    await driver.exit_active_monitor()

    assert _mac_writes(t) == [("w8", REG_MACID + i, REAL_MAC[i]) for i in range(6)]
    assert t._load(REG_MACID, 6) == int.from_bytes(REAL_MAC, "little")


async def test_exit_active_monitor_before_enter_is_a_no_op():
    driver = make_driver()
    t = MockTransport()
    driver.transport = t

    await driver.exit_active_monitor()      # must not raise

    assert t.writes == []
    assert t.reads == []


async def test_double_enter_keeps_the_first_capture_and_restores_the_original():
    driver = make_driver()
    t = MockTransport()
    t.seed(REG_MACID, int.from_bytes(REAL_MAC, "little"), size=6)
    driver.transport = t
    forged_a = bytes.fromhex("0200000000aa")
    forged_b = bytes.fromhex("0200000000bb")

    await driver.enter_active_monitor(forged_a)
    await driver.enter_active_monitor(forged_b)     # no exit in between

    # The REG_MACID snapshot is sticky: the second enter must not re-read (it would
    # capture forged A), so exactly six read8 ops touch the register.
    assert t.reads == [("r8", REG_MACID + i) for i in range(6)]

    t.writes.clear()
    await driver.exit_active_monitor()

    assert _mac_writes(t) == [("w8", REG_MACID + i, REAL_MAC[i]) for i in range(6)]
    assert t._load(REG_MACID, 6) == int.from_bytes(REAL_MAC, "little")


# ---- warm reattach (EFUSE re-read) --------------------------------------


async def test_warm_reattach_reads_the_efuse_and_sets_the_mac(monkeypatch):
    driver = make_driver()
    efuse = EfuseDefaults(mac_address=REAL_MAC)
    calls: list[str] = []
    monkeypatch.setattr(
        drv, "read_and_parse",
        lambda t: (calls.append("read_and_parse"), efuse)[1],
    )

    async def _finish(_update, *, from_warm):
        calls.append(f"finish_attach(from_warm={from_warm})")
        return True

    monkeypatch.setattr(driver, "_finish_attach", _finish)

    assert await driver._warm_reattach(lambda *a: None) is True
    assert calls == ["read_and_parse", "finish_attach(from_warm=True)"]
    assert driver._efuse is efuse
    assert driver.mac_address == REAL_MAC.hex(":")


async def test_warm_reattach_wraps_an_efuse_read_failure(monkeypatch):
    driver = make_driver()

    def _boom(_t):
        raise IOError("efuse boom")

    monkeypatch.setattr(drv, "read_and_parse", _boom)

    with pytest.raises(BringUpError) as excinfo:
        await driver._warm_reattach(lambda *a: None)
    assert excinfo.value.stage == "efuse"
    assert "efuse boom" in excinfo.value.detail

