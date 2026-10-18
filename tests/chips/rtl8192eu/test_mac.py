"""MAC bring-up: endpoints, queue pages/priority, TRXFF/TX boundaries, LLT, RCR."""
from __future__ import annotations

import pytest

from wifit3.chips.rtl8192eu.constants import (
    AUTO_LLT_INIT_LLT,
    BEACON_DISABLE_TSF_UPDATE,
    BEACON_DMA_ATIME_INT_TIME,
    CR_MAC_RX_ENABLE,
    CR_MAC_TX_ENABLE,
    DRIVER_EARLY_INT_TIME,
    MCU_WINT_INIT_READY,
    NORMAL_SIE_EP_TX_HIGH_MASK,
    NORMAL_SIE_EP_TX_LOW_MASK,
    NORMAL_SIE_EP_TX_NORMAL_MASK,
    RCR_MONITOR,
    REG_AUTO_LLT,
    REG_BEACON_CTRL,
    REG_BEACON_DMA_TIME,
    REG_BEACON_TCFG,
    REG_CR,
    REG_DRIVER_EARLY_INT,
    REG_HIMR0,
    REG_HIMR1,
    REG_MACID,
    REG_MCU_FW_DL,
    REG_NORMAL_SIE_EP_TX,
    REG_RCR,
    REG_RQPN,
    REG_RQPN_NPQ,
    REG_RX_DRVINFO_SZ,
    REG_RXFLTMAP1,
    REG_TBTT_PROHIBIT,
    REG_TDECTRL,
    REG_TRXFF_BNDY,
    REG_TXDMA_OFFSET_CHK,
    REG_TXPKTBUF_BCNQ_BDNY,
    REG_TXPKTBUF_MGQ_BDNY,
    REG_TXPKTBUF_WMAC_LBK_BF_HD,
    REG_TRXDMA_CTRL,
    TXDESC_QUEUE_BE,
    TXDESC_QUEUE_BEACON,
    TXDESC_QUEUE_BK,
    TXDESC_QUEUE_MGNT,
    TXDESC_QUEUE_VI,
    TXDESC_QUEUE_VO,
    TXDMA_OFFSET_DROP_DATA_EN,
    TX_TOTAL_PAGE_NUM_8192E,
)
from wifit3.chips.rtl8192eu.mac import (
    REG_ACLK_MON,
    REG_MAR,
    REG_QUEUE_CTRL,
    TRXFF_BOUNDARY_8192E,
    TxQueues,
    admit_ack_frames,
    apply_mac_init_table,
    apply_monitor_rx_filter,
    auto_llt_table,
    config_endpoints,
    drop_ack_frames,
    enable_rx_data_path,
    endpoint_index_for_queue,
    init_beacon_params,
    init_queue_priority,
    init_queue_reserved_page,
    init_rtl8192e_tail,
    is_chip_warm,
    post_fw_mac_init,
    post_phy_mac_init,
    pre_fw_queue_init,
    set_mac_addr,
    set_trxff_rx_page_boundary,
    set_tx_buffer_boundary,
    tx_lane_endpoints,
)
from wifit3.chips.rtl8192eu.phy_tables import MAC_INIT_TABLE_8192E

from tests.chips.rtl8192eu._mock import FailingReadTransport, MockTransport

ALL_QUEUES = TxQueues(high=True, normal=True, low=True)


class LltTransport(MockTransport):
    """``REG_AUTO_LLT`` self-clears once the HW has built the LLT ring."""

    def write32(self, addr: int, val: int) -> None:
        super().write32(addr, val)
        if addr == REG_AUTO_LLT:
            self._store(addr, [((val & ~AUTO_LLT_INIT_LLT) >> (8 * i)) & 0xFF for i in range(4)])


# ---- endpoints ----------------------------------------------------------


def test_config_endpoints_reads_the_sie_flags():
    t = MockTransport()
    t.seed(REG_NORMAL_SIE_EP_TX,
           NORMAL_SIE_EP_TX_HIGH_MASK | NORMAL_SIE_EP_TX_LOW_MASK, 2)
    assert config_endpoints(t, 3) == TxQueues(high=True, normal=False, low=True)

    t2 = MockTransport()
    t2.seed(REG_NORMAL_SIE_EP_TX, NORMAL_SIE_EP_TX_NORMAL_MASK, 2)
    assert config_endpoints(t2, 3) == TxQueues(high=False, normal=True, low=False)


def test_config_endpoints_falls_back_to_the_bulk_out_count():
    for nr_out_eps, expected in (
        (3, TxQueues(high=True, normal=True, low=True)),
        (2, TxQueues(high=True, normal=True, low=False)),
        (1, TxQueues(high=True, normal=False, low=False)),
    ):
        t = MockTransport()          # REG_NORMAL_SIE_EP_TX reads 0
        assert config_endpoints(t, nr_out_eps) == expected


def test_config_endpoints_sie_masks_win_over_the_endpoint_count():
    t = MockTransport()
    t.seed(REG_NORMAL_SIE_EP_TX,
           NORMAL_SIE_EP_TX_NORMAL_MASK | NORMAL_SIE_EP_TX_LOW_MASK, 2)
    # nr_out_eps=1 alone would give (high,); the SIE masks must decide instead.
    assert config_endpoints(t, 1) == TxQueues(high=False, normal=True, low=True)


def test_config_endpoints_sie_masks_do_not_need_bulk_out_endpoints():
    t = MockTransport()
    t.seed(REG_NORMAL_SIE_EP_TX,
           NORMAL_SIE_EP_TX_NORMAL_MASK | NORMAL_SIE_EP_TX_LOW_MASK, 2)
    # The SIE branch returns before the count fallback, so zero must NOT raise.
    assert config_endpoints(t, 0) == TxQueues(high=False, normal=True, low=True)


def test_config_endpoints_rejects_zero_endpoints():
    with pytest.raises(ValueError, match="end-point count 0"):
        config_endpoints(MockTransport(), 0)


# ---- MAC table / queues -------------------------------------------------


def test_apply_mac_init_table_writes_all_101_bytes():
    t = MockTransport()
    assert apply_mac_init_table(t) == len(MAC_INIT_TABLE_8192E) == 101
    assert [w for w in t.writes if w[0] == "w8"][0] == ("w8", 0x011, 0xEB)
    assert [w for w in t.writes if w[0] == "w8"][-1] == ("w8", 0x70B, 0x87)


def test_post_fw_mac_init_applies_only_the_mac_table():
    t = MockTransport()
    post_fw_mac_init(t)
    assert len(t.writes) == len(MAC_INIT_TABLE_8192E) == 101
    assert all(w[0] == "w8" for w in t.writes)


def test_queue_reserved_page_partitions_high_low_and_normal():
    t = MockTransport()
    init_queue_reserved_page(t, ALL_QUEUES)

    assert t.last_write_to(REG_RQPN_NPQ) == 0     # NPQ/EPQ partitions disabled
    # LOAD | hi=8 | lo=0xc | pub=0xf3-8-0xc-0-1=0xde
    expected = 0x80000000 | 0x08 | (0x0C << 8) | (0xDE << 16)
    assert expected == 0x80DE0C08
    assert t.last_write_to(REG_RQPN) == expected


def test_queue_priority_three_endpoints_routes_low_normal_high():
    t = MockTransport()
    t.seed(REG_TRXDMA_CTRL, 0x0005, 2)
    init_queue_priority(t, ALL_QUEUES)
    # preserve[2:0]=5; VO/VI/MG/HL=3 (HIGH), BE/BK=1 (LOW), VI=2 (NORMAL).
    assert t.last_write_to(REG_TRXDMA_CTRL) == 0xF5B5


def test_queue_priority_two_endpoints_routes_high_and_normal():
    t = MockTransport()
    init_queue_priority(t, TxQueues(high=True, normal=True, low=False))
    assert t.last_write_to(REG_TRXDMA_CTRL) == 0xFAF0


def test_queue_priority_rejects_an_empty_queue_set():
    with pytest.raises(ValueError):
        init_queue_priority(MockTransport(), TxQueues(high=False, normal=False, low=False))


def test_tx_lane_endpoints_three_lanes_rank_high_normal_low():
    lanes = tx_lane_endpoints(ALL_QUEUES)

    # MGNT/BEACON share out_ep[0]; the AC queues take their lane's rank (`core.c:2688-2710`).
    assert lanes.mgnt == 0
    assert lanes.beacon == 0
    assert lanes.be == 2      # LOW
    assert lanes.bk == 2      # LOW
    assert lanes.vi == 1      # NORMAL
    assert lanes.vo == 0      # HIGH


@pytest.mark.parametrize(
    ("queues", "expected"),
    [
        # `core.c:2663-2668`: indices are hard-coded 0/1 by lane pair.
        (TxQueues(high=True, normal=True, low=False), (0, 0, 1, 1, 0, 0)),
        (TxQueues(high=True, normal=False, low=True), (0, 0, 1, 1, 0, 0)),
        (TxQueues(high=False, normal=True, low=True), (0, 0, 1, 1, 0, 0)),
        # A single lane routes everything to out_ep[0] (`core.c:2645-2649`).
        (TxQueues(high=True, normal=False, low=False), (0, 0, 0, 0, 0, 0)),
        (TxQueues(high=False, normal=True, low=False), (0, 0, 0, 0, 0, 0)),
        (TxQueues(high=False, normal=False, low=True), (0, 0, 0, 0, 0, 0)),
    ],
)
def test_tx_lane_endpoints_one_and_two_lane_cards(queues, expected):
    lanes = tx_lane_endpoints(queues)
    assert (lanes.mgnt, lanes.beacon, lanes.be, lanes.bk, lanes.vi, lanes.vo) == expected


def test_endpoint_index_for_queue_maps_every_queue():
    lanes = tx_lane_endpoints(ALL_QUEUES)

    assert endpoint_index_for_queue(TXDESC_QUEUE_MGNT, lanes) == 0
    assert endpoint_index_for_queue(TXDESC_QUEUE_BEACON, lanes) == 0
    assert endpoint_index_for_queue(TXDESC_QUEUE_BE, lanes) == 2
    assert endpoint_index_for_queue(TXDESC_QUEUE_BK, lanes) == 2
    assert endpoint_index_for_queue(TXDESC_QUEUE_VI, lanes) == 1
    assert endpoint_index_for_queue(TXDESC_QUEUE_VO, lanes) == 0
    with pytest.raises(ValueError, match="no bulk-OUT lane"):
        endpoint_index_for_queue(0x13, lanes)      # TXDESC_QUEUE_CMD has no data path


def test_pre_fw_queue_init_returns_the_lane_table():
    assert pre_fw_queue_init(MockTransport(), ALL_QUEUES) == tx_lane_endpoints(ALL_QUEUES)


def test_pre_fw_queue_init_emits_pages_priority_then_boundary():
    t = MockTransport()
    pre_fw_queue_init(t, ALL_QUEUES)

    assert t.writes == [
        ("w32", REG_RQPN_NPQ, 0),
        ("w32", REG_RQPN, 0x80DE0C08),
        ("w16", REG_TRXDMA_CTRL, 0xF5B0),
        ("w16", REG_TRXFF_BNDY + 2, TRXFF_BOUNDARY_8192E),
    ]
    assert t.reads == [("r16", REG_TRXDMA_CTRL)]


# ---- RX/TX page boundaries ---------------------------------------------


def test_trxff_boundary_is_0x3cff_at_register_plus_two():
    t = MockTransport()
    set_trxff_rx_page_boundary(t)
    assert TRXFF_BOUNDARY_8192E == 0x3CFF
    assert t.last_write_to(REG_TRXFF_BNDY + 2) == 0x3CFF


def test_tx_buffer_boundary_writes_total_page_num_plus_one():
    t = MockTransport()
    set_tx_buffer_boundary(t)
    expected = TX_TOTAL_PAGE_NUM_8192E + 1
    assert expected == 0xF4
    for reg in (
        REG_TXPKTBUF_BCNQ_BDNY,
        REG_TXPKTBUF_MGQ_BDNY,
        REG_TXPKTBUF_WMAC_LBK_BF_HD,
        REG_TRXFF_BNDY,
        REG_TDECTRL + 1,
    ):
        assert t.last_write_to(reg) == 0xF4, f"missing 0x{reg:04x}"


def test_post_phy_mac_init_runs_boundary_llt_and_quirks():
    t = LltTransport()
    post_phy_mac_init(t)
    assert t.last_write_to(REG_TXPKTBUF_BCNQ_BDNY) == 0xF4
    assert t.writes_to(REG_AUTO_LLT)[0][2] & AUTO_LLT_INIT_LLT
    assert t.last_write_to(REG_TXDMA_OFFSET_CHK) == TXDMA_OFFSET_DROP_DATA_EN


def test_auto_llt_sets_the_bit_then_waits_for_the_hw_to_clear_it():
    t = LltTransport()
    auto_llt_table(t)
    assert t.writes_to(REG_AUTO_LLT)[0][2] & AUTO_LLT_INIT_LLT
    assert not (t._load(REG_AUTO_LLT, 4) & AUTO_LLT_INIT_LLT)


def test_auto_llt_raises_when_the_bit_never_clears():
    t = MockTransport()          # write persists -> the poll never sees it clear
    with pytest.raises(IOError, match="AUTO_LLT_INIT_LLT"):
        auto_llt_table(t)


# ---- RX filter ----------------------------------------------------------


def test_rcr_monitor_is_all_accept_bits():
    # Monitor superset WITHOUT CRC32/ICV accept (hardware drops bad frames):
    # ACCEPT_AP|PHYS_MATCH|MCAST|BCAST|DATA|CTRL|MGMT|HTC_LOC|APPEND_PHYSTAT|ICV|MIC.
    assert RCR_MONITOR == 0x7000780F


def test_apply_monitor_rx_filter_writes_the_monitor_rcr():
    t = MockTransport()
    apply_monitor_rx_filter(t)
    assert t.last_write_to(REG_RCR) == 0x7000780F


def test_ack_tap_gates_ctrl_subtype_13_only():
    t = MockTransport()
    admit_ack_frames(t)
    assert t.last_write_to(REG_RXFLTMAP1) == 1 << 13

    t2 = MockTransport()
    t2.seed(REG_RXFLTMAP1, 1 << 13, 2)
    drop_ack_frames(t2)
    assert t2.last_write_to(REG_RXFLTMAP1) == 0


def test_enable_rx_data_path_sets_drvinfo_himr_rcr_and_mar():
    t = MockTransport()

    enable_rx_data_path(t)

    assert t.last_write_to(REG_RX_DRVINFO_SZ) == 4
    assert t.last_write_to(REG_HIMR0) == 0
    assert t.last_write_to(REG_HIMR1) == 0
    assert t.last_write_to(REG_RCR) == 0x7000780F
    assert t.last_write_to(REG_MAR) == 0xFFFFFFFF
    assert t.last_write_to(REG_MAR + 4) == 0xFFFFFFFF
    # The RTL8192E chip tail is a separate block now, not part of the RX enable.
    assert t.writes_to(REG_QUEUE_CTRL) == []
    assert t.writes_to(REG_ACLK_MON) == []


def test_init_beacon_params_writes_the_five_registers():
    t = MockTransport()
    init_beacon_params(t)

    assert t.last_write_to(REG_BEACON_CTRL) == 0x1010
    assert t.last_write_to(REG_TBTT_PROHIBIT) == 0x6404
    assert t.last_write_to(REG_DRIVER_EARLY_INT) == DRIVER_EARLY_INT_TIME == 5
    assert t.last_write_to(REG_BEACON_DMA_TIME) == BEACON_DMA_ATIME_INT_TIME == 2
    assert t.last_write_to(REG_BEACON_TCFG) == 0x660F
    assert BEACON_DISABLE_TSF_UPDATE == 0x10


def test_init_rtl8192e_tail_clears_rts_bw_and_usb_mode():
    t = MockTransport()
    t.seed(REG_QUEUE_CTRL, 0xFF, 1)
    init_rtl8192e_tail(t)
    assert t.last_write_to(REG_QUEUE_CTRL) == 0xF7      # clear BIT(3)
    assert t.last_write_to(REG_ACLK_MON) == 0x00


# ---- REG_MACID (active monitor) ----------------------------------------


def test_set_mac_addr_writes_the_six_bytes_in_order():
    t = MockTransport()
    mac = bytes.fromhex("001122334455")

    set_mac_addr(t, mac)

    assert REG_MACID == 0x0610
    assert [w for w in t.writes if w[0] == "w8"] == [
        ("w8", REG_MACID + i, mac[i]) for i in range(6)
    ]
    assert t._load(REG_MACID, 6) == int.from_bytes(mac, "little")


def test_set_mac_addr_rejects_a_malformed_mac():
    t = MockTransport()
    with pytest.raises(ValueError):
        set_mac_addr(t, b"\x00" * 5)
    assert t.writes == []


# ---- warm detect --------------------------------------------------------


def test_is_chip_warm_needs_fw_ready_and_mac_enabled():
    t = MockTransport()
    t.seed(REG_MCU_FW_DL, MCU_WINT_INIT_READY)
    t.seed(REG_CR, CR_MAC_TX_ENABLE | CR_MAC_RX_ENABLE, 2)
    assert is_chip_warm(t) is True

    missing_fw = MockTransport()
    missing_fw.seed(REG_CR, CR_MAC_TX_ENABLE | CR_MAC_RX_ENABLE, 2)
    assert is_chip_warm(missing_fw) is False

    missing_mac = MockTransport()
    missing_mac.seed(REG_MCU_FW_DL, MCU_WINT_INIT_READY)
    assert is_chip_warm(missing_mac) is False


def test_is_chip_warm_is_false_when_the_read_fails():
    assert is_chip_warm(FailingReadTransport()) is False
