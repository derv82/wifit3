"""Hardware-free regression for the RTL8188EUS (DKMS) MAC config + phydm walker.

Locks the conditional-walker outcome on this board (driver1 = 0x00040200, plain
board) so the MAC reg table reproduces the cold-boot wire. Full byte-for-byte
replay lives in ``scripts/rtl8188eus_dkms/verify_pcap.py``.
"""
from wifit3.chips.rtl8188eus_dkms import mac, phy_cond


class RecTx:
    def __init__(self):
        self.w8 = []   # (addr, value)
        self.w16 = []

    def write8(self, a, v):
        self.w8.append((a, v & 0xFF))

    def write16(self, a, v):
        self.w16.append((a, v & 0xFFFF))


def test_driver1_for_this_board():
    # cut=A(0), interface=USB(0x02), platform=CE(0x04), package=0, board=0.
    assert phy_cond.DRIVER1 == 0x00040200


def test_board_gated_conditions_take_else_default():
    # The MAC table's 0x040 block is board-type gated; a plain board (board_type=0)
    # takes the ELSE default 0x040=0x00, never the 0x0C branches.
    t = RecTx()
    mac.phy_mac_config(t)
    writes_040 = [v for a, v in t.w8 if a == 0x040]
    assert writes_040 == [0x00]                 # exactly the ELSE default, no 0x0C


def test_known_writes_and_aggr_num():
    t = RecTx()
    mac.phy_mac_config(t)
    d = dict(t.w8)
    assert d[0x026] == 0x41 and d[0x027] == 0x35   # first two table rows
    assert d[0x700] == 0x21 and d[0x70B] == 0x87   # last table rows
    # USB build: REG_MAX_AGGR_NUM = (0x07 << 8) | 0x07.
    assert t.w16 == [(0x04CA, 0x0707)]


def test_check_positive_rejects_extlna_branch():
    # A condition requiring GLNA (bit0) must fail on a board with type_glna=0.
    assert phy_cond.check_positive(0x90000001, 0x00000000, 0x00000000) is False
    # A pure don't-care condition (low nibble 0) that bit-matches passes.
    assert phy_cond.check_positive(0x00000000, 0, 0) is True
