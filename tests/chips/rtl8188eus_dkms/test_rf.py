"""Hardware-free regression for the RTL8188EUS (DKMS) RF (radio-A) config.

Full byte-for-byte replay lives in ``scripts/chips/rtl8188eus_dkms/verify_pcap.py``;
this locks the LSSI write encoding and the RFENV setup ordering.
"""
from wifit3.chips.rtl8188eus_dkms import rf
from wifit3.chips.rtl8188eus_dkms.constants import RF_LSSI_WRITE_A


class Tx:
    def __init__(self, reads=None):
        self.w32 = []
        self.ops = []                 # ordered ("R"/"W", addr, value)
        self._reads = dict(reads or {})

    def read32(self, a):
        v = self._reads.get(a, 0x00000000)
        self.ops.append(("R", a, v))
        return v

    def write32(self, a, v):
        self.w32.append((a, v & 0xFFFFFFFF))
        self.ops.append(("W", a, v & 0xFFFFFFFF))


def test_lssi_write_encoding():
    # First radio_a row 0x000,0x00030000 -> ((0<<20)|0x30000)&0x0FFFFFFF = 0x00030000.
    t = Tx()
    rf._emit_rf(t)(0x000, 0x00030000)
    assert t.w32 == [(RF_LSSI_WRITE_A, 0x00030000)]
    # RF reg 0x18 with a 20-bit value: addr in [27:20], data in [19:0].
    t = Tx()
    rf._emit_rf(t)(0x018, 0x00012345)
    assert t.w32 == [(RF_LSSI_WRITE_A, (0x18 << 20) | 0x12345)]


def test_delay_address_is_not_written():
    t = Tx()
    rf._emit_rf(t)(0xFFE, 0x00000000)   # 50 ms settling delay marker
    assert t.w32 == []


def test_rf_config_rfenv_then_table_then_restore():
    t = Tx(reads={0x0870: 0x07000760, 0x0860: 0x66F60110, 0x0824: 0x00390204})
    rf.phy_rf_config(t)
    addrs = [a for a, _ in t.w32]
    # RFENV setup touches 0x860 (x2) and 0x824 (x2) before any LSSI write...
    first_lssi = addrs.index(RF_LSSI_WRITE_A)
    assert set(addrs[:first_lssi]) == {0x0860, 0x0824}
    # ...the bulk are LSSI writes (91 radio-A rows on this card)...
    assert addrs.count(RF_LSSI_WRITE_A) == 91
    # ...and the final write restores RFENV on 0x870.
    assert addrs[-1] == 0x0870


def test_serial_read_path_a_pi_readback():
    # cap1 op 1573-1577: path-A read of RF_CHNLBW (0x18). 0x820[8]=1 -> PI readback.
    t = Tx(reads={0x0824: 0x00390204, 0x0820: 0x01000100, 0x08B8: 0x00107407})
    val = rf._phy_rf_serial_read(t, 0, rf.RF_CHNLBW)
    assert t.ops == [
        ("R", 0x0824, 0x00390204),       # tmplong = HSSI param2 (path A)
        ("W", 0x0824, 0x00390204),       # tmplong & ~read-edge (edge already clear)
        ("W", 0x0824, 0x8C390204),       # (offset 0x18 << 23) | read-edge
        ("R", 0x0820, 0x01000100),       # RfPiEnable = HSSI param1[8] = 1
        ("R", 0x08B8, 0x00107407),       # PI read-back (TransceiverA_HSPI_Readback)
    ]
    assert val == 0x07407                 # masked to bLSSIReadBackData (20 bits)


def test_serial_read_path_b_serial_readback():
    # cap1 op 1578-1583: path-B read. 0x828[8]=0 -> non-PI (serial) read-back at 0x8a4.
    t = Tx(reads={0x0824: 0x8C390204, 0x082C: 0x00000000,
                  0x0828: 0x00000000, 0x08A4: 0x00033333})
    val = rf._phy_rf_serial_read(t, 1, rf.RF_CHNLBW)
    assert t.ops == [
        ("R", 0x0824, 0x8C390204),       # tmplong still read from path-A param2
        ("R", 0x082C, 0x00000000),       # tmplong2 = path-B param2
        ("W", 0x0824, 0x0C390204),       # path-A param2 with read-edge cleared
        ("W", 0x082C, 0x8C000000),       # path-B param2 staged with offset + edge
        ("R", 0x0828, 0x00000000),       # RfPiEnable = path-B param1[8] = 0
        ("R", 0x08A4, 0x00033333),       # serial read-back (rFPGA0_XB_LSSIReadBack)
    ]
    assert val == 0x33333


def test_read_rf_chnl_val_returns_both_paths():
    t = Tx(reads={0x0824: 0x00390204, 0x0820: 0x01000100, 0x08B8: 0x00107407,
                  0x082C: 0x00000000, 0x0828: 0x00000000, 0x08A4: 0x00000000})
    assert rf.read_rf_chnl_val(t) == (0x07407, 0x00000)


def test_serial_write_encoding():
    t = Tx()
    rf.phy_rf_serial_write(t, 0, 0x30, 0x18000)
    assert t.w32 == [(0x0840, (0x30 << 20) | 0x18000)]


def test_set_rf_reg_full_mask_is_direct_write():
    # mask == RFREGOFFSETMASK -> no read, direct LSSI write.
    t = Tx()
    rf.set_rf_reg(t, 0, 0x31, 0xFFFFF, 0x0000F)
    assert t.ops == [("W", 0x0840, (0x31 << 20) | 0x0000F)]


def test_set_rf_reg_masked_does_serial_rmw():
    # mask != full -> serial-read RF 0xef (0x800a0... readback 0xa0), set bit19, write.
    t = Tx(reads={0x0824: 0x00390204, 0x0820: 0x01000100, 0x08B8: 0x000000A0})
    rf.set_rf_reg(t, 0, 0xEF, 0x80000, 0x1)
    assert t.w32[-1] == (0x0840, (0xEF << 20) | 0x800A0)
