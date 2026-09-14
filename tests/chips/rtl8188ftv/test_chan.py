"""rtl8188ftv M5 channel-1 tune + RX acceptance.

Covers the values computed from kernel C (8188f.c:400-643, core.c:7494-7971)
independent of the capture: spur calibration registers, 20 MHz BB RMW
arithmetic, RF TRX_BW writes, monitor RCR value.  Wire-order replay is gated
live in scripts/chips/rtl8188ftv/verify_pcap.py.
"""
from wifit3.chips.rtl8188ftv.chan import set_channel_2g_20mhz
from wifit3.chips.rtl8188ftv.mac import configure_filter, enable_rx_path


class _Fake:
    """Records writes; returns a canned value per register read.

    Writes are fed back so subsequent reads of the same address return the
    last-written value, matching hardware behaviour for RMW sequences.
    """

    def __init__(self):
        self.reads: dict[int, int] = {}
        self.writes: list[tuple[int, int]] = []

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
        self.reads[addr] = (self.reads.get(addr, 0) & ~0xFF) | (val & 0xFF)

    def write16(self, addr, val):
        self.writes.append((addr, val))
        mask = (1 << 16) - 1
        self.reads[addr] = (self.reads.get(addr, 0) & ~mask) | (val & mask)

    def write32(self, addr, val):
        self.writes.append((addr, val))
        self.reads[addr] = val


def test_set_channel_2g_20mhz_ch1_wire_values():
    t = _Fake()
    # HSSI readback path used by phy.read_rfreg (core.c:867-905):
    # 0840 writes are the LSSI RF transport, so RF reads go through the
    # 0824/0820 readback dance and land in reads{} below.
    t.reads.update({
        0x0824: 0x80390204,   # REG_FPGA0_XA_HSSI_PARM2 (PI set)
        0x0820: 0x01000100,   # REG_FPGA0_XA_HSSI_PARM1 (PI bit)
        0x08b8: 0x00107c07,   # REG_HSPI_XA_READBACK → MODE_AG = 0x7c07
        0x0c40: 0x1F78403F,   # REG_OFDM0_RX_D_SYNC_PATH
        0x0900: 0x00000000,   # REG_FPGA1_RF_MODE
        0x0800: 0x83045700,   # REG_FPGA0_RF_MODE (20 MHz path)
        0x0ce4: 0x10000000,   # REG_OFDM0_TX_PSDO_NOISE_WEIGHT
        0x0c10: 0x18800000,   # REG_OFDM0_XA_RX_AFE
        0x0d2c: 0xCB979975,   # REG_OFDM1_CFO_TRACKING
        0x0954: 0x4A880000,   # REG_OFDM_RX_DFIR
    })
    set_channel_2g_20mhz(t, 1)

    # REG_FPGA0_XA_LSSI_PARM writes carry the RF reg in bits 31:20.
    lssi = [v for a, v in t.writes if a == 0x0840]
    assert lssi == [
        0x01807C01,   # MODE_AG ch1 (0x7c07 & ~0x3ff | 1)
        0x01800C01,   # TRX_BW ch1 | BW_20MHZ
        0x08700065,   # RXG_MIX_SWBW 20 MHz
        0x01C00000,   # RX_BB2 20 MHz
        0x0DF00140,   # GAIN_CCA
        0x01B01C6C,   # RX_G2
    ]

    ofdm0 = [v for a, v in t.writes if a == 0x0c40]
    assert ofdm0 == [0x1F78403F, 0x1F78423F]

    cfo = [v for a, v in t.writes if a == 0x0d2c]
    assert cfo == [0xCB979975]  # ch1: no spur → BIT(28) stays clear

    fpga1 = [v for a, v in t.writes if a == 0x0900]
    assert fpga1 == [0x00000000]

    fpga0 = [v for a, v in t.writes if a == 0x0800]
    assert fpga0 == [0x83045700, 0x83045700, 0x83045700]

    psdo = [v for a, v in t.writes if a == 0x0ce4]
    assert psdo == [0x10000000, 0x10000000]

    rx_afe = [v for a, v in t.writes if a == 0x0c10]
    assert rx_afe == [0x18800000]

    dfir = [v for a, v in t.writes if a == 0x0954]
    assert dfir == [0x4A800000, 0x4A300000]


def test_enable_rx_path_writes_filtermaps_and_igi():
    t = _Fake()
    t.reads[0x0c50] = 0x69553420   # REG_OFDM0_XA_AGC_CORE1
    enable_rx_path(t)
    assert t.writes == [
        (0x06A4, 0xFFFF),
        (0x06A0, 0xFFFF),
        (0x0c50, 0x6955341E),
    ]


def test_configure_filter_monitor_rcr_value():
    t = _Fake()
    configure_filter(t)
    assert t.writes == [(0x0608, 0x7000702F)]