"""Offline checks on the RTL8192EU TX descriptor build and RX parse (no hardware)."""
from types import SimpleNamespace

from wifit3.chips.rtl8192eu import constants as C
from wifit3.chips.rtl8192eu import tx
from wifit3.chips.rtl8192eu.rx import rtl8192e_cck_rssi, rtl8xxxu_parse_rxdesc24

_TXDESC = 40


def _get32(buf: bytes, off: int) -> int:
    return int.from_bytes(buf[off:off + 4], "little")


def _mac_hdr(fc0: int, da: bytes) -> bytes:
    # 24-byte 802.11 header: FC, dur, addr1(DA), addr2, addr3, seq
    return bytes([fc0, 0x00, 0x00, 0x00]) + da + bytes(6) + bytes(6) + bytes(2)


def test_tx_desc_header_and_8192e_txdw0():
    priv = SimpleNamespace(pipe_out={C.TXDESC_QUEUE_BE: 0x03})
    frame = _mac_hdr(0x08, b"\x02\x00\x00\x00\x00\x01") + b"payload"  # data, unicast DA
    endpoint, urb = tx.rtl8xxxu_tx(priv, frame)

    assert endpoint == 0x03
    tx_desc, body = urb[:_TXDESC], urb[_TXDESC:]
    assert body == frame
    assert int.from_bytes(tx_desc[0:2], "little") == len(frame)
    assert tx_desc[2] == _TXDESC
    # txdw0 (u8 at offset 3): OWN BIT(7) | FIRST BIT(3) | LAST BIT(2); unicast DA -> no bcast bit.
    assert tx_desc[3] == 0x8c
    # Non-QoS data maps to the BE queue.
    assert (_get32(tx_desc, 4) >> C.TXDESC_QUEUE_SHIFT) == C.TXDESC_QUEUE_BE


def test_tx_desc_broadcast_and_mgmt_queue():
    priv = SimpleNamespace(pipe_out={C.TXDESC_QUEUE_MGNT: 0x05})
    frame = _mac_hdr(0x40, b"\xff\xff\xff\xff\xff\xff")  # probe request, broadcast DA
    _endpoint, urb = tx.rtl8xxxu_tx(priv, frame)
    tx_desc = urb[:_TXDESC]
    assert tx_desc[3] & (C.TXDESC_BROADMULTICAST & 0xFF)
    assert (_get32(tx_desc, 4) >> C.TXDESC_QUEUE_SHIFT) == C.TXDESC_QUEUE_MGNT


def test_tx_beacon_goes_on_the_mgmt_queue():
    priv = SimpleNamespace(pipe_out={C.TXDESC_QUEUE_MGNT: 0x05})
    endpoint, urb = tx.rtl8xxxu_tx(priv, _mac_hdr(0x80, b"\xff" * 6))
    assert endpoint == 0x05
    assert (_get32(urb[:_TXDESC], 4) >> C.TXDESC_QUEUE_SHIFT) == C.TXDESC_QUEUE_MGNT


def test_tx_csum_not_inverted_on_8192e():
    priv = SimpleNamespace(pipe_out={C.TXDESC_QUEUE_BE: 0x03})
    frame = _mac_hdr(0x08, b"\x02\x00\x00\x00\x00\x01")
    _endpoint, urb = tx.rtl8xxxu_tx(priv, frame)
    tx_desc = bytearray(urb[:_TXDESC])
    stored = int.from_bytes(tx_desc[28:30], "little")
    tx_desc[28:30] = b"\x00\x00"
    expected = 0
    for i in range(0, 32, 2):
        expected ^= int.from_bytes(tx_desc[i:i + 2], "little")
    # RTL8192E keeps the plain XOR csum (RTL8710B / RTL8192F invert it).
    assert stored == expected


def _rx_desc(pktlen: int, *, drvinfo_units: int, phy_stats: bool, rate: int,
             rpt_sel: bool = False, crc: bool = False) -> bytes:
    dw0 = (pktlen & 0x3FFF) | (drvinfo_units << 16)
    if crc:
        dw0 |= 1 << 14
    if phy_stats:
        dw0 |= 1 << 26
    dw2 = (1 << 28) if rpt_sel else 0
    dw3 = rate & 0x7F
    return dw0.to_bytes(4, "little") + bytes(4) + dw2.to_bytes(4, "little") + \
        dw3.to_bytes(4, "little") + bytes(8)


def _priv():
    fops = SimpleNamespace(cck_rssi=rtl8192e_cck_rssi)
    return SimpleNamespace(fops=fops, cck_agc_report_type=0, c2hcmd_queue=[])


def test_rx_ofdm_frame_and_signal():
    frame = _mac_hdr(0x80, bytes(6))  # beacon
    phy = bytearray(24)
    phy[4] = 200  # cck_sig_qual_ofdm_pwdb_all
    urb = _rx_desc(len(frame), drvinfo_units=3, phy_stats=True, rate=C.DESC_RATE_6M) + bytes(phy) + frame
    out = list(rtl8xxxu_parse_rxdesc24(_priv(), urb))
    assert len(out) == 1
    got_frame, signal = out[0]
    assert got_frame == frame
    assert signal == (200 >> 1) - 110


def test_rx_cck_frame_uses_cck_rssi():
    frame = _mac_hdr(0x80, bytes(6))
    phy = bytearray(24)
    phy[5] = (7 << 5) | 3  # lna_idx 7, vga_idx 3
    urb = _rx_desc(len(frame), drvinfo_units=3, phy_stats=True, rate=0x00) + bytes(phy) + frame
    _got_frame, signal = next(rtl8xxxu_parse_rxdesc24(_priv(), urb))
    assert signal == (-44) - (2 * 3)  # lna_gain_table_0[7] - 2*vga_idx


def test_rx_phy_stats_without_drvinfo_reads_the_frame_bytes():
    # core.c:6437: phy_stats is skb->data after the descriptor, even with drvinfo_sz 0.
    frame = _mac_hdr(0x80, bytes(6))
    urb = _rx_desc(len(frame), drvinfo_units=0, phy_stats=True, rate=C.DESC_RATE_6M) + frame
    got_frame, signal = next(rtl8xxxu_parse_rxdesc24(_priv(), urb))
    assert got_frame == frame
    assert signal == (frame[4] >> 1) - 110


def test_rx_crc_frame_dropped_and_c2h_routed():
    priv = _priv()
    frame = _mac_hdr(0x80, bytes(6))
    crc_urb = _rx_desc(len(frame), drvinfo_units=0, phy_stats=False, rate=0, crc=True) + frame
    assert list(rtl8xxxu_parse_rxdesc24(priv, crc_urb)) == []

    c2h = b"\x0c\x01\x02\x03"
    c2h_urb = _rx_desc(len(c2h), drvinfo_units=0, phy_stats=False, rate=0, rpt_sel=True) + c2h
    assert list(rtl8xxxu_parse_rxdesc24(priv, c2h_urb)) == []
    assert priv.c2hcmd_queue == [c2h]
