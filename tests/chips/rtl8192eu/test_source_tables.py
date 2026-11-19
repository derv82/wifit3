"""Offline checks on source-derived RTL8192EU radio tables."""
from wifit3.chips.rtl8192eu import tables as T
from wifit3.chips.rtl8192eu.rx import RXDesc24, extract_mpdu
import pytest


def test_table_sentinels():
    for name in ('RTL8192E_MAC_INIT_TABLE', 'RTL8192EU_PHY_INIT_TABLE',
                 'RTL8XXX_AGC_8192EU_STD_TABLE', 'RTL8XXX_AGC_8192EU_HIGHPA_TABLE',
                 'RTL8192EU_RADIOA_INIT_TABLE', 'RTL8192EU_RADIOB_INIT_TABLE'):
        table = getattr(T, name)
        assert len(table) > 10
        assert table[-1][0] in (0xffff, 0xff)


def test_phy_variants_are_distinct():
    assert T.RTL8192EU_PHY_INIT_TABLE != T.RTL8192EU_PHY_INIT_TABLE_EXT_PA
    assert len(T.RTL8192EU_PHY_INIT_TABLE) == len(T.RTL8192EU_PHY_INIT_TABLE_EXT_PA)


def test_rx_descriptor_and_frame():
    dw0 = 4 | (1 << 16) | (2 << 24)
    header = dw0.to_bytes(4, 'little') + bytes(20)
    urb = header + bytes(8) + bytes(2) + b'abcd'
    desc, mpdu = extract_mpdu(urb)
    assert desc.packet_length == 4
    assert desc.payload_offset == 34
    assert mpdu == b'abcd'


def test_rx_short_and_corrupt():
    with pytest.raises(ValueError):
        RXDesc24.parse(bytes(23))
    with pytest.raises(ValueError):
        extract_mpdu((4).to_bytes(4, 'little') + bytes(20))
    with pytest.raises(ValueError):
        extract_mpdu(((1 << 14) | 4).to_bytes(4, 'little') + bytes(20) + b'abcd')
