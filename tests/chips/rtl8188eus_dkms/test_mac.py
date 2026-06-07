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
        self.w32 = []

    def write8(self, a, v):
        self.w8.append((a, v & 0xFF))

    def write16(self, a, v):
        self.w16.append((a, v & 0xFFFF))

    def write32(self, a, v):
        self.w32.append((a, v & 0xFFFFFFFF))

    def read8(self, a):
        return 0x00

    def read16(self, a):
        return 0x0000

    def read32(self, a):
        return 0x00000000   # LLT poll: NO_ACTIVE immediately


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


def test_init_misc01():
    t = RecTx()
    mac.init_misc01(t)
    assert (0x0214, 0x00) in t.w8                  # RQPN_NPQ
    assert (0x0200, 0x80A70000) in t.w32           # RQPN (all-public, 1-EP)
    assert (0x010C, 0xFAF0) in t.w16               # TRXDMA queue map (read low3=0)
    assert (0x0116, 0x25FF) in t.w16               # RX FF boundary
    assert (0x0104, 0x11) in t.w8                  # PBP page size


def test_init_misc02_key_writes():
    t = RecTx()
    mac.init_misc02(t)
    d8 = dict(t.w8)
    d32 = dict(t.w32)
    assert d32[0x0608] == 0x700060CE                # STA RCR
    assert d32[0x0620] == 0xFFFFFFFF                # accept-all multicast
    assert d32[0x0440] == 0x000FFFF1                # RRSR (read 0 -> mask|val)
    assert d8[0x0640] == 0x40                       # ACKTO
    # RX_AGG_USB: TRXDMA clears RXDMA_AGG_EN (read 0 -> stays 0), USB sets USB_AGG_EN.
    assert d8[0x010C] == 0x00 and d8[0xFE55] == 0x08
    assert d32[0x0484] == 0xFFFFFFFF                # MACID no-link


def test_set_macid():
    # cap1 op 1894-1899: program the efuse MAC into REG_MACID (0x610-0x615) byte-wise.
    t = RecTx()
    mac.set_macid(t, bytes.fromhex("d46e0e0dadbf"))
    assert t.w8 == [(0x0610, 0xD4), (0x0611, 0x6E), (0x0612, 0x0E),
                    (0x0613, 0x0D), (0x0614, 0xAD), (0x0615, 0xBF)]


def test_invalidate_cam_all():
    # cap1 op 1588: clear all security-cam entries (CAM_POLLING|CAM_CLR).
    t = RecTx()
    mac.invalidate_cam_all(t)
    assert t.w32 == [(0x0670, 0xC0000000)]


def test_init_misc11_tail():
    # cap1 op 1629-1630: disable BAR (0x4cc) then enable HW seq num (0x423=0xFF).
    # Antenna-selection + RFE writes are no-ops on this card (no external PA/LNA).
    t = RecTx()
    mac.init_misc11_tail(t)
    assert t.w32 == [(0x04CC, 0x0201FFFF)]
    assert t.w8 == [(0x0423, 0xFF)]


def test_tx_buffer_boundary():
    t = RecTx()
    mac.init_tx_buffer_boundary(t, bndy=0xA8)
    assert t.w8 == [(0x0424, 0xA8), (0x0425, 0xA8), (0x045D, 0xA8),
                    (0x0114, 0xA8), (0x0209, 0xA8)]


def test_llt_chain_structure():
    t = RecTx()
    mac.init_llt(t, bndy=0xA8, last=175)
    # 176 LLT writes: 0..166 -> next, 167 -> 0xFF, 168..174 -> next, 175 -> 0xA8.
    assert len(t.w32) == 176
    # Decode (addr, data) from each REG_LLT_INIT write (op bits 30 set).
    entries = [((v >> 8) & 0xFF, v & 0xFF) for _, v in t.w32]
    assert entries[0] == (0, 1) and entries[166] == (166, 167)
    assert entries[167] == (167, 0xFF)               # end of list
    assert entries[168] == (168, 169) and entries[174] == (174, 175)
    assert entries[175] == (175, 0xA8)               # ring: last -> boundary
    assert all((v >> 30) & 0x3 == 1 for _, v in t.w32)  # WRITE_ACCESS op
