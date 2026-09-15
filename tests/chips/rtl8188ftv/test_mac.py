"""rtl8188ftv M3 init_device_post_phy + helpers.

Covers the values computed from kernel C (core.c:3950-4373, 8188f.c:645-683)
independent of the capture: RFSW antenna mask, TX buffer boundary, PBP,
burst/aggregation RMW arithmetic.  Wire-order replay is gated live in
scripts/chips/rtl8188ftv/verify_pcap.py.
"""
from wifit3.chips.rtl8188ftv.constants import (
    CR_MAC_RX_ENABLE,
    CR_MAC_TX_ENABLE,
    FPGA0_RF_ANTSW,
    FPGA0_RF_ANTSWB,
    FPGA0_RF_BD_CTRL_SHIFT,
    FPGA0_RF_PAPE,
    FPGA0_RF_TRSW,
    FPGA0_RF_TRSWB,
    MCU_WINT_INIT_READY,
    PAGE_NUM_HI_PQ_8188F,
    PAGE_NUM_NORM_PQ_8188F,
    REG_CCK_PD_THRESH,
    REG_CR,
    REG_FPGA0_XAB_RF_SW_CTRL,
    REG_FPGA0_XA_RF_INT_OE,
    REG_FPGA0_TX_INFO,
    REG_MCU_FW_DL,
    REG_RQPN,
    REG_RQPN_NPQ,
    REG_RXDMA_AGG_PG_TH,
    REG_RXDMA_PRO_8723B,
    REG_TRXDMA_CTRL,
    REG_TRXFF_BNDY,
    RQPN_LOAD,
    TRXFF_BOUNDARY_8188F,
    TX_TOTAL_PAGE_NUM_8188F,
)
from wifit3.chips.rtl8188ftv.efuse import EfuseDefaults
from wifit3.chips.rtl8188ftv.mac import (
    init_aggregation,
    init_burst,
    init_device_post_phy,
    init_queue_priority_2ep,
    init_queue_reserved_page,
    init_statistics,
    is_chip_warm,
    set_trxff_rx_page_boundary,
)


class _Fake:
    """Records writes; returns a canned value per register read."""

    def __init__(self):
        self.reads: dict[int, int] = {}
        self.writes: list[tuple[int, object]] = []

    def _val(self, addr: int, width: int) -> int:
        v = self.reads.get(addr, 0)
        return v & ((1 << (8 * width)) - 1)

    def read8(self, addr):
        return self._val(addr, 1)

    def read16(self, addr):
        return self._val(addr, 2)

    def read32(self, addr):
        return self._val(addr, 4)

    def write8(self, addr, val):
        self.writes.append((addr, val))

    def write16(self, addr, val):
        self.writes.append((addr, val))

    def write32(self, addr, val):
        self.writes.append((addr, val))

    @property
    def written(self) -> dict[int, object]:
        return dict(self.writes)


def test_init_device_post_phy_rfsw_and_bounds():
    t = _Fake()
    init_device_post_phy(t, EfuseDefaults())

    w = t.written
    rfsw = (FPGA0_RF_TRSW | FPGA0_RF_TRSWB | FPGA0_RF_ANTSW |
            FPGA0_RF_ANTSWB |
            ((FPGA0_RF_ANTSW | FPGA0_RF_ANTSWB) << FPGA0_RF_BD_CTRL_SHIFT) |
            FPGA0_RF_PAPE | (FPGA0_RF_PAPE << FPGA0_RF_BD_CTRL_SHIFT))
    assert rfsw == 0x07000760
    assert w[REG_FPGA0_XAB_RF_SW_CTRL] == 0x07000760
    assert w[REG_FPGA0_TX_INFO] == 0x00000003
    assert w[REG_FPGA0_XA_RF_INT_OE] == 0x66F60210
    assert w[REG_CCK_PD_THRESH] == 0x83


def test_init_device_post_phy_tx_boundary_and_pbp():
    t = _Fake()
    t.reads = {0x0224: 0x00002020}  # REG_AUTO_LLT
    init_device_post_phy(t, EfuseDefaults())

    w = t.written
    assert w[0x0424] == 0xF8  # REG_TXPKTBUF_BCNQ_BDNY = 0xf7 + 1
    assert w[0x0425] == 0xF8
    assert w[0x045D] == 0xF8
    assert w[0x0114] == 0xF8
    assert w[0x0209] == 0xF8
    assert w[0x0104] == 0x22  # REG_PBP = 2<<4 | 2<<0
    assert w[0x0224] == 0x00012020  # REG_AUTO_LLT |= AUTO_LLT_INIT_LLT


def test_init_burst_rxdma_rmw():
    t = _Fake()
    t.reads = {REG_RXDMA_PRO_8723B: 0x04, 0x04C7: 0x00, 0x001C: 0x00}
    init_burst(t)

    w = t.written
    # 0x04 | burst_size(0x10) | burst_cnt(0x0c) | dma_mode(0x02) = 0x1e
    assert w[REG_RXDMA_PRO_8723B] == 0x1E
    assert w[0x04C7] == 0x80        # HT single AMPDU enable
    assert w[0x001C] == 0x60        # RSV_CTRL |= WLOCK_1C | DIS_PRST
    assert w[0x04CA] == 0x0C14      # max aggr num


def test_init_aggregation_rmw():
    t = _Fake()
    t.reads = {
        0x0208: 0x0000F810,           # REG_TDECTRL (DWBCN0)
        REG_TRXDMA_CTRL: 0xF0,
        REG_RXDMA_AGG_PG_TH: 0x20002003,
        REG_RXDMA_PRO_8723B: 0x1E,
    }
    init_aggregation(t)

    w = t.written
    assert w[0x0208] == 0x0000F860   # &~(0xf<<4) | 6<<4
    assert w[0x0228] == 0x0C         # 6 << 1
    assert w[REG_TRXDMA_CTRL] == 0xF0  # &~RXDMA_AGG_EN
    assert w[REG_RXDMA_AGG_PG_TH] == 0x20000000  # &~BIT31 &~0xFF0F
    assert w[REG_RXDMA_PRO_8723B] == 0x1C  # &~BIT1


def test_init_statistics_writes():
    t = _Fake()
    t.reads = {0x0E28: 0x0, 0x0890: 0xFFFF0800, 0x0C0C: 0x6C6C6C6C}
    init_statistics(t)

    w = t.written
    assert w[0x0896] == 0xC350       # NHM timer + 2
    assert w[0x0892] == 0xFFFF       # NHM TH9/10 + 2
    assert w[0x0898] == 0xFFFFFF50   # TH3..TH0
    assert w[0x089C] == 0xFFFFFFFF   # TH7..TH4
    assert w[0x0E28] == 0xFF         # FPGA0_IQK |= 0xff
    assert w[0x0890] == 0xFFFF0900   # ~(BIT8|9|10) then BIT8
    assert w[0x0C0C] == 0x6C6C6CEC   # OFDM0_FA_RSTC |= BIT7


def test_init_queue_reserved_page_matches_capture():
    t = _Fake()
    init_queue_reserved_page(t)

    w = t.written
    # nq at shift 0; pubq = total - hq - nq - 1 = 0xf7 - 0x0c - 0x02 - 1
    assert w[REG_RQPN_NPQ] == PAGE_NUM_NORM_PQ_8188F
    assert w[REG_RQPN] == (
        RQPN_LOAD
        | PAGE_NUM_HI_PQ_8188F
        | ((TX_TOTAL_PAGE_NUM_8188F - PAGE_NUM_HI_PQ_8188F -
            PAGE_NUM_NORM_PQ_8188F - 1) << 16)
    )
    # capture-1 ops 945-946 ground truth
    assert w[REG_RQPN_NPQ] == 0x00000002
    assert w[REG_RQPN] == 0x80E8000C


def test_init_queue_priority_2ep_rmw():
    t = _Fake()
    t.reads = {REG_TRXDMA_CTRL: 0x0007}   # low 3 bits must survive the RMW
    init_queue_priority_2ep(t)

    w = t.written
    assert w[REG_TRXDMA_CTRL] == 0xFAF7   # routing OR'd over preserved bits


def test_init_queue_priority_2ep_matches_capture():
    t = _Fake()
    t.reads = {REG_TRXDMA_CTRL: 0x0000}   # capture-1 op 947 read
    init_queue_priority_2ep(t)

    assert t.written[REG_TRXDMA_CTRL] == 0xFAF0   # capture-1 op 948 ground truth


def test_set_trxff_rx_page_boundary_writes_capture_value():
    t = _Fake()
    set_trxff_rx_page_boundary(t)

    w = t.written
    assert w[REG_TRXFF_BNDY + 2] == TRXFF_BOUNDARY_8188F
    assert w[REG_TRXFF_BNDY + 2] == 0x3F7F   # capture-1 op 949


class _Raising:
    """Fake transport whose REG_CR reads raise (cold 8188F: CR unreadable)."""

    def read32(self, addr):
        if addr == REG_CR:
            raise OSError("Operation timed out")
        return 0x00000105  # REG_MCU_FW_DL: MCU_WINT_INIT_READY set


def test_is_chip_warm_cold_chip_returns_false_without_reading_cr():
    t = _Fake()
    t.reads = {REG_MCU_FW_DL: 0x00000105}  # FW bits but no MCU ready
    assert is_chip_warm(t) is False


def test_is_chip_warm_fw_not_running_returns_false_without_reading_cr():
    t = _Raising()  # REG_CR would raise; must not be touched
    t_basic = _Fake()
    t_basic.reads = {REG_MCU_FW_DL: 0x00000005 & ~(MCU_WINT_INIT_READY)}
    assert is_chip_warm(t_basic) is False
    assert is_chip_warm(t) is False  # MCU not ready -> short-circuits before CR


def test_is_chip_warm_cr_read_error_returns_false():
    t = _Fake()
    t.reads = {REG_MCU_FW_DL: MCU_WINT_INIT_READY | 0x5}
    t.read32 = lambda addr: (_ for _ in ()).throw(OSError("Operation timed out"))
    assert is_chip_warm(t) is False


def test_is_chip_warm_requires_mac_enable_bits():
    t = _Fake()
    t.reads = {
        REG_MCU_FW_DL: MCU_WINT_INIT_READY | 0x5,
        REG_CR: CR_MAC_TX_ENABLE,  # only TX
    }
    assert is_chip_warm(t) is False
    t.reads[REG_CR] = CR_MAC_TX_ENABLE | CR_MAC_RX_ENABLE
    assert is_chip_warm(t) is True