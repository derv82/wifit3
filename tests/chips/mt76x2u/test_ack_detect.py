"""mt76x2u TX-ACK detection: the RX tap routes the AP's link-layer ACKs to a MAC we inject as
through the base tally; the _enable/_disable_rx_acks hooks flip MT_RX_FILTR_CFG bit 10; the
software seq-stamp + global retry routing. No hardware — synthetic frames + a mocked transport."""
import struct
from unittest.mock import AsyncMock, MagicMock

from wifit3.chips.mt76x2u.constants import (
    MT_RX_FILTR_CFG,
    MT_RX_FILTR_CFG_ACK,
    MT_TX_RETRY_CFG,
)
from wifit3.chips.mt76x2u.driver import MT76x2UDriver
from wifit3.chips.mt76x2u.tx import _TXWI_ACK_CTL_REQ

_RXFILT_BASE = 0x00015B97   # monitor RX filter with the ACK-admit bit already clear


def _ack_rx(ra: bytes) -> bytes:
    """A bulk-IN URB whose MPDU is a 10-byte 802.11 ACK to ``ra``: 36-byte RXWI prefix,
    ctl.MPDU_LEN = 10, MPDU begins at _HEADER_LEN (36)."""
    data = bytearray(64)
    struct.pack_into("<I", data, 0, 60)          # rxfce
    struct.pack_into("<I", data, 8, 10 << 16)    # ctl.MPDU_LEN = 10
    data[36] = 0xD4                              # FC: ACK control subtype
    data[40:46] = ra                            # addr1 / RA
    return bytes(data)


def _mgmt_frame(ta: bytes = bytes.fromhex("020000000001")) -> bytes:
    """A 24-B deauth: FC=0xC0, addr1 (RA), addr2 (TA=our injected source), addr3, seqctl=0."""
    return (b"\xc0\x00\x00\x00" + bytes.fromhex("aabbccddeeff") + ta
            + bytes.fromhex("aabbccddeeff") + b"\x00\x00")


def _driver() -> MT76x2UDriver:
    d = MT76x2UDriver(MagicMock(), MagicMock())
    d._parsed = []
    d.register_rx_callback(d._parsed.append)
    return d


def test_tap_counts_ack_to_our_mac():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._ack_detect_on = True
    d._our_tx_macs.add(ra)
    d._on_raw_rx(_ack_rx(ra))
    assert d.acks_seen(ra) == 1
    assert d._parsed == []          # an ACK is never handed to the frame parser


def test_tap_ignores_ack_to_foreign_mac():
    d = _driver()
    ra = bytes.fromhex("aabbccddeeff")
    d._ack_detect_on = True
    d._on_raw_rx(_ack_rx(ra))
    assert d.acks_seen(ra) == 0     # armed, but not one of ours


def test_tap_off_by_default():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._on_raw_rx(_ack_rx(ra))       # _ack_detect_on stays False
    assert d.acks_seen(ra) == 0


async def test_enable_rx_acks_clears_ack_filter_bit():
    d = _driver()
    d.transport.read32 = MagicMock(return_value=_RXFILT_BASE | MT_RX_FILTR_CFG_ACK)
    writes: list[tuple[int, int]] = []
    d.transport.write32 = lambda reg, val: writes.append((reg, val))
    await d.enable_rx_acks()
    assert d._ack_detect_on is True
    assert (MT_RX_FILTR_CFG, _RXFILT_BASE) in writes   # ACK bit cleared to admit the AP's ACKs


async def test_disable_rx_acks_restores_ack_filter_bit():
    d = _driver()
    d.transport.read32 = MagicMock(return_value=_RXFILT_BASE)
    writes: list[tuple[int, int]] = []
    d.transport.write32 = lambda reg, val: writes.append((reg, val))
    await d.disable_rx_acks()
    assert d._ack_detect_on is False
    assert (MT_RX_FILTR_CFG, _RXFILT_BASE | MT_RX_FILTR_CFG_ACK) in writes


def test_stamp_tx_seq_advances_seqno():
    d = _driver()
    f1 = d._stamp_tx_seq(_mgmt_frame())
    assert f1[22:24] == b"\x10\x00"     # one step is 0x10 in seq_ctrl bits [4:15]
    f2 = d._stamp_tx_seq(_mgmt_frame())
    assert f2[22:24] == b"\x20\x00"


async def test_inject_requests_ack_and_sends_once():
    d = _driver()
    d.transport.async_write_bulk = AsyncMock(
        side_effect=lambda ep, blob, timeout_ms=500: len(blob))
    assert await d.inject_frame(_mgmt_frame()) is True   # base -> _stamp_tx_seq -> _inject_frame
    d.transport.async_write_bulk.assert_awaited_once()
    _ep, blob = d.transport.async_write_bulk.await_args.args[:2]
    txwi = blob[4:24]                                    # [TXINFO 4][TXWI 20]
    assert txwi[4] & _TXWI_ACK_CTL_REQ                   # ACK requested (HW retry armed)


async def test_mac_reset_routes_retry_limit(monkeypatch):
    from wifit3.chips.mt76x2u import mac as mac_mod
    monkeypatch.setattr(mac_mod, "_mac_fixup_xtal", lambda t: None)
    transport = MagicMock()
    transport.read32 = MagicMock(return_value=0)
    writes: list[tuple[int, int]] = []
    transport.write32 = lambda reg, val: writes.append((reg, val))
    assert await mac_mod.mac_reset(transport, short_retry_limit=7) is True
    retry = [v for (r, v) in writes if r == MT_TX_RETRY_CFG]
    assert retry and retry[0] & 0xFF == 7                     # SHORT_RTY_LIMIT
    assert retry[0] == (0x47F01F0F & ~0xFF) | 7              # LONG/11B/mode bytes untouched


async def test_mac_reset_default_keeps_captured_retry(monkeypatch):
    from wifit3.chips.mt76x2u import mac as mac_mod
    monkeypatch.setattr(mac_mod, "_mac_fixup_xtal", lambda t: None)
    transport = MagicMock()
    transport.read32 = MagicMock(return_value=0)
    writes: list[tuple[int, int]] = []
    transport.write32 = lambda reg, val: writes.append((reg, val))
    assert await mac_mod.mac_reset(transport) is True        # no override
    retry = [v for (r, v) in writes if r == MT_TX_RETRY_CFG]
    assert retry and retry[0] == 0x47F01F0F                  # verify_pcap cold path stays byte-exact
