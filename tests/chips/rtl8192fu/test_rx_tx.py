"""rtl8192fu RX (rxdesc24 + jaguar2 PHY status + C2H) and TX (txdesc40) paths."""
from wifit3.chips.rtl8192fu import constants as C
from wifit3.chips.rtl8192fu.core import rtl8xxxu_config_endpoints_no_sie, rtl8xxxu_init_queue_priority
from wifit3.chips.rtl8192fu.rx import C2H_8723B_BT_INFO, rtl8xxxu_c2hcmd_callback, rtl8xxxu_parse_rxdesc24
from wifit3.chips.rtl8192fu.tx import RTL8XXXU_TXDESC40_SIZE, rtl8xxxu_tx

from .fake import make_priv

BEACON = bytes.fromhex("80000000ffffffffffff0200000000010200000000010000") + bytes(12)


def _rx(frame: bytes, *, phy_stats: bytes | None = None, crc32: bool = False, rpt_sel: bool = False) -> bytes:
    drvinfo = phy_stats or b""
    dw0 = len(frame) | (len(drvinfo) // 8) << 16
    if crc32:
        dw0 |= C.BIT(14)
    if phy_stats is not None:
        dw0 |= C.BIT(26)
    dw2 = C.BIT(28) if rpt_sel else 0
    desc = dw0.to_bytes(4, "little") + bytes(4) + dw2.to_bytes(4, "little") + bytes(12)
    return desc + drvinfo + frame


def _page1(pwdb_a: int, pwdb_b: int) -> bytes:
    return bytes([1, pwdb_a, pwdb_b]) + bytes(29)


def test_page1_phy_status_signal_is_strongest_path():
    priv, _ = make_priv()
    priv.rx_paths = 2
    got = list(rtl8xxxu_parse_rxdesc24(priv, _rx(BEACON, phy_stats=_page1(50, 60))))
    assert got == [(BEACON, 60 - 110)]


def test_crc32_frame_is_dropped():
    priv, _ = make_priv()
    priv.rx_paths = 2
    assert list(rtl8xxxu_parse_rxdesc24(priv, _rx(BEACON, phy_stats=_page1(50, 60), crc32=True))) == []


def test_no_phy_stats_yields_no_signal():
    priv, _ = make_priv()
    assert list(rtl8xxxu_parse_rxdesc24(priv, _rx(BEACON))) == [(BEACON, None)]


def test_rpt_sel_packet_is_queued_as_c2h():
    priv, _ = make_priv()
    c2h = bytes([C2H_8723B_BT_INFO, 0, 0, 0x00])
    assert list(rtl8xxxu_parse_rxdesc24(priv, _rx(c2h, rpt_sel=True))) == []
    assert priv.c2hcmd_queue == [c2h]


def test_bt_info_c2h_while_unassociated_sends_tdma_and_coex_table_0():
    priv, transport = make_priv()
    priv.c2hcmd_queue.append(bytes([C2H_8723B_BT_INFO, 0, 0, 0x00]))
    rtl8xxxu_c2hcmd_callback(priv)

    assert priv.c2hcmd_queue == []
    assert transport.writes() == [
        (C.REG_HMBOX_EXT0_8723B, 4, 0x0000),
        (C.REG_HMBOX_0, 4, 0x00000860),
        (C.REG_BT_COEX_TABLE1, 4, 0x55555555),
        (C.REG_BT_COEX_TABLE2, 4, 0x55555555),
        (C.REG_BT_COEX_TABLE3, 4, 0x00FFFFFF),
        (C.REG_BT_COEX_TABLE4, 1, 0x03),
    ]


def _tx_priv():
    priv, transport = make_priv()
    priv.out_ep, priv.nr_out_eps = [0x05, 0x06, 0x08, 0x09, 0x0C, 0x0D], 6
    rtl8xxxu_config_endpoints_no_sie(priv)
    rtl8xxxu_init_queue_priority(priv)
    return priv


def _checksum_ok(desc: bytes) -> bool:
    csum = 0
    for i in range(0, 32, 2):
        csum ^= int.from_bytes(desc[i:i + 2], "little")
    return csum == 0xFFFF


def test_deauth_goes_out_on_the_management_pipe():
    priv = _tx_priv()
    deauth = bytes.fromhex("c0003a01020000000002020000000001020000000001a0010700")
    endpoint, urb = rtl8xxxu_tx(priv, deauth)
    desc = urb[:RTL8XXXU_TXDESC40_SIZE]

    assert endpoint == 0x05
    assert urb[RTL8XXXU_TXDESC40_SIZE:] == deauth
    assert int.from_bytes(desc[0:2], "little") == len(deauth)
    assert desc[2] == RTL8XXXU_TXDESC40_SIZE
    assert int.from_bytes(desc[4:8], "little") >> C.TXDESC_QUEUE_SHIFT & 0x1F == C.TXDESC_QUEUE_MGNT
    assert int.from_bytes(desc[36:40], "little") >> C.TXDESC40_SEQ_SHIFT == 0x1A
    assert _checksum_ok(desc)


def test_null_data_goes_out_on_the_best_effort_pipe():
    priv = _tx_priv()
    null = bytes.fromhex("48010000020000000002020000000001020000000002") + bytes(2)
    endpoint, urb = rtl8xxxu_tx(priv, null)
    assert endpoint == 0x08
    assert _checksum_ok(urb[:RTL8XXXU_TXDESC40_SIZE])
