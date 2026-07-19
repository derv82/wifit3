"""mt76x0u TX-ACK detection: the RX tap routes the AP's link-layer ACKs to a MAC we inject as
through the base tally; the _enable/_disable_rx_acks hooks flip MT_RX_FILTR_CFG bit 10; the
software seq-stamp + global retry routing. No hardware — synthetic frames + a mocked transport."""
import struct
from unittest.mock import MagicMock

from wifit3.chips.mt76x0u.constants import (
    MT_RX_FILTR_CFG,
    MT_RX_FILTR_CFG_ACK,
    MT_TX_RETRY_CFG,
    MT_TXWI_ACK_CTL_REQ,
)
from wifit3.chips.mt76x0u.mac import init_mac_registers
from wifit3.chips.mt76x0u.driver import MT76x0UDriver

_RXFILT_BASE = 0x00017B97   # monitor RX filter with the ACK-admit bit already clear


def _ack_rx(ra: bytes) -> bytes:
    """A bulk-IN buffer whose MPDU is a 10-byte 802.11 ACK to ``ra``: 36-byte RXWI
    prefix, ctl.MPDU_LEN = 10, MPDU begins at HEADER_SIZE (36)."""
    data = bytearray(64)
    struct.pack_into("<H", data, 0, 60)          # dma_len
    struct.pack_into("<I", data, 8, 10 << 16)    # ctl.MPDU_LEN = 10
    data[36] = 0xD4                              # FC: ACK control subtype
    data[40:46] = ra                            # addr1 / RA
    return bytes(data)


def _mgmt_frame(ta: bytes = bytes.fromhex("020000000001")) -> bytes:
    """A 24-B deauth: FC=0xC0, addr1 (RA), addr2 (TA=our injected source), addr3, seqctl=0."""
    return (b"\xc0\x00\x00\x00" + bytes.fromhex("aabbccddeeff") + ta
            + bytes.fromhex("aabbccddeeff") + b"\x00\x00")


def _driver() -> MT76x0UDriver:
    d = MT76x0UDriver(MagicMock(), MagicMock())
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
    sent: list[tuple[int, bytes]] = []
    d.transport.bulk_out = lambda ep, pkt, timeout_ms=1000: sent.append((ep, pkt))
    assert await d.inject_frame(_mgmt_frame()) is True   # base -> _stamp_tx_seq -> _inject_frame
    assert len(sent) == 1
    _ep, pkt = sent[0]
    txwi = pkt[4:24]                                     # [DMA-info 4][TXWI 20]
    assert txwi[4] & MT_TXWI_ACK_CTL_REQ                 # ACK requested (HW retry armed)


def test_init_mac_registers_default_keeps_captured_retry():
    transport = MagicMock()
    transport.read32 = MagicMock(return_value=0)
    mcu = MagicMock()
    init_mac_registers(transport, mcu)                           # no override
    table = dict(mcu.random_write.call_args_list[0].args[1])
    assert table[MT_TX_RETRY_CFG] == 0x47D01F0F                  # verify_pcap cold path stays byte-exact
