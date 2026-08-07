"""RTL8822CU driver TX/ACK/active-monitor unit tests (no hardware)."""
import struct
from unittest.mock import MagicMock

import pytest
import usb.core

import wifit3.chips.rtl8822cu.driver as drv
from wifit3.chips.rtl8822cu.driver import RTL8822CUDriver
from wifit3.chips.rtl8822cu.mac import set_mac_addr
from wifit3.chips.driver import FakeMacSupport
from wifit3.wlan.interface import WlanInterface


def _ack_buf(ra: bytes) -> bytes:
    """A bulk-IN buffer with one 14-byte on-wire ACK to ``ra`` (10-B MPDU + 4-B HW FCS)."""
    desc = bytearray(24)
    struct.pack_into("<I", desc, 0, 14)         # rxdw0: pkt_len=14, all flags clear
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


async def test_inject_frame_sends_desc_and_frame_unchanged(monkeypatch):
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
    assert sent[0][48:] == frame                                        # HW stamps seq


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


def test_stamp_tx_seq_is_identity():
    d = _driver()
    frame = _deauth()
    assert d._stamp_tx_seq(frame) is frame      # Realtek HW-stamps via EN_HWSEQ


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


def test_tap_off_by_default():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._rx_dispatch(_ack_buf(ra))    # _ack_detect_on stays False
    assert d.acks_seen(ra) == 0
