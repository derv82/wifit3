"""Unit tests for the RTL8822BU per-card FEM branches (rfe_type/cut generalization).

The reference card (rfe_type 3 iFEM, D-cut) is byte-gated by verify_pcap / verify_channels /
verify_initial_tune; these cover the NON-reference branches the gate can't exercise — the FEM CCA
table selection (`_ccapar_by_rfe`), the RFE pinmux dispatch (`_rfe_pinmux` -> ifem/efem/4_11), and
the switch_band SoML RxHP arm (`_switch_band_rxhp`) — asserted against the vendor phydm_hal_api8822b.c.
"""
from wifit3.chips.rtl8822bu_dkms import chan


class _BB:
    """Minimal BB transport: masked-RMW regs dict + RF-write capture (sipi write32 target)."""
    def __init__(self):
        self.regs: dict[int, int] = {}

    def read32(self, addr):
        return self.regs.get(addr, 0)

    def write32(self, addr, val):
        self.regs[addr] = val & 0xFFFFFFFF

    def read8(self, addr):
        return self.regs.get(addr, 0) & 0xFF

    def write8(self, addr, val):
        self.regs[addr] = val & 0xFF


def _field(bb, addr, mask):
    return (bb.regs.get(addr, 0) & mask) >> ((mask & -mask).bit_length() - 1)


# --- _ccapar_by_rfe: FEM CCA table selection --------------------------------

def test_ccapar_reference_ifem_rfe_table():
    """rfe 3 (reference) selects cca_ifem_ccut_rfe and writes no 0x83c (iFEM CCA)."""
    bb = _BB()
    chan._ccapar_by_rfe(bb, 1, True, rfe_type=3, cut=3, ant_2r=True)   # col 1 (2G/2R)
    assert bb.regs[0x082C] == 0x75DA8010
    assert bb.regs[0x0830] == 0x97A0EAAC
    assert bb.regs[0x0838] == 0x86666341
    assert 0x083C not in bb.regs


def test_ccapar_plain_ifem_table():
    """A non-{3,5,12,15,16,17,19} iFEM card (rfe 0) uses cca_ifem_ccut, not cca_ifem_ccut_rfe."""
    bb = _BB()
    chan._ccapar_by_rfe(bb, 1, True, rfe_type=0, cut=3, ant_2r=True)
    assert bb.regs[0x082C] == 0x75C97010            # cca_ifem_ccut[0][1], not 0x75DA8010
    assert 0x083C not in bb.regs


def test_ccapar_efem_writes_83c():
    """eFEM (rfe 1) selects cca_efem_ccut and seeds 0x83c on a non-B-cut part."""
    bb = _BB()
    chan._ccapar_by_rfe(bb, 1, True, rfe_type=1, cut=3, ant_2r=True)
    assert bb.regs[0x082C] == 0x75B76010            # cca_efem_ccut[0][1]
    assert bb.regs[0x083C] == 0x9194B2B9


def test_ccapar_hybrid_rfe2_bands():
    """rfe 2 is 2G iFEM / 5G eFEM: 2.4 GHz uses the plain-iFEM table (no 0x83c), 5 GHz uses eFEM."""
    bb2g = _BB()
    chan._ccapar_by_rfe(bb2g, 1, True, rfe_type=2, cut=3, ant_2r=True)
    assert bb2g.regs[0x082C] == 0x75C97010          # 2G iFEM
    assert 0x083C not in bb2g.regs
    bb5g = _BB()
    chan._ccapar_by_rfe(bb5g, 36, True, rfe_type=2, cut=3, ant_2r=True)   # col 3 (5G/2R)
    assert bb5g.regs[0x082C] == 0x75B76010          # 5G eFEM
    assert bb5g.regs[0x083C] == 0x9194B2B9


def test_ccapar_rfe16_bigjump():
    """rfe 16 (MS case) enlarges the 0x8c8 big-jump on 2.4 GHz."""
    bb = _BB()
    chan._ccapar_by_rfe(bb, 1, True, rfe_type=16, cut=3, ant_2r=True)
    assert _field(bb, 0x08C8, (1 << 3) | (1 << 2) | (1 << 1)) == 0x3


# --- _rfe_pinmux dispatch + eFEM / 4_11 pinmux ------------------------------

def test_rfe_efem_pinmux_bands():
    """phydm_rfe_efem (rfe 1, non-B-cut): 0xcb0 signal source differs 2.4 / 5 GHz; 2R antenna 0xa501."""
    bb2g = _BB()
    chan._rfe_efem(bb2g, 1, ant_2r=True, cut=3, rfe_type=1)
    assert bb2g.regs[0x0CB0] & 0xFFFFFF == 0x705770
    assert bb2g.regs[0x0CA0] & 0xFFFF == 0xA501
    bb5g = _BB()
    chan._rfe_efem(bb5g, 36, ant_2r=True, cut=3, rfe_type=1)
    assert bb5g.regs[0x0CB0] & 0xFFFFFF == 0x177517


def test_rfe_efem_bcut_early_arm():
    """phydm_rfe_efem B-cut + rfe<2 takes the 0x704570 / 0x810-PAPE / 0xa555 arm."""
    bb = _BB()
    chan._rfe_efem(bb, 1, ant_2r=True, cut=chan.ODM_CUT_B, rfe_type=0)
    assert bb.regs[0x0CB0] & 0xFFFFFF == 0x704570
    assert bb.regs[0x0CA0] & 0xFFFF == 0xA555
    assert _field(bb, 0x0810, 0xFFF00000) == 0x211


def test_rfe_4_11_pinmux():
    """phydm_rfe_4_11 (rfe 4/11): 2.4 GHz antenna 0xf050 (2R), 5 GHz 0xa501."""
    bb2g = _BB()
    chan._rfe_4_11(bb2g, 1, ant_2r=True)
    assert bb2g.regs[0x0CB0] & 0xFFFFFF == 0x745774
    assert bb2g.regs[0x0CA0] & 0xFFFF == 0xF050
    bb5g = _BB()
    chan._rfe_4_11(bb5g, 36, ant_2r=True)
    assert bb5g.regs[0x0CB0] & 0xFFFFFF == 0x477547
    assert bb5g.regs[0x0CA0] & 0xFFFF == 0xA501


def test_rfe_pinmux_dispatch():
    """_rfe_pinmux routes by rfe_type; 15/18 (unported) fall to the iFEM give-it-a-shot default."""
    def cb0(rfe):
        bb = _BB()
        chan._rfe_pinmux(bb, 1, rfe, ant_2r=True, cut=3)
        return bb.regs[0x0CB0] & 0xFFFFFF, bb.regs[0x0CA0] & 0xFFFF
    assert cb0(3) == (0x745774, 0xA501)             # iFEM (reference)
    assert cb0(1) == (0x705770, 0xA501)             # eFEM
    assert cb0(4) == (0x745774, 0xF050)             # 4_11 (same 0xcb0 as iFEM, different antenna)
    assert cb0(15) == (0x745774, 0xA501)            # unported -> iFEM fallback
    assert cb0(18) == (0x745774, 0xA501)            # unported -> iFEM fallback


# --- switch_band SoML RxHP arm + rfe 12/19 RF 0xb3 --------------------------

def test_switch_band_rxhp_reference():
    """rfe 3 SoML-on: 2.4 GHz keeps 0x08108492 / 0x8d8[27]=1, 5 GHz uses 0x08108000 / [27]=0."""
    bb2g = _BB()
    chan._switch_band_rxhp(bb2g, 1, soml_on=True, rfe_type=3)
    assert bb2g.regs[0x08CC] == 0x08108492
    assert _field(bb2g, 0x08D8, 1 << 27) == 1
    bb5g = _BB()
    chan._switch_band_rxhp(bb5g, 36, soml_on=True, rfe_type=3)
    assert bb5g.regs[0x08CC] == 0x08108000
    assert _field(bb5g, 0x08D8, 1 << 27) == 0


def test_switch_band_rxhp_efem_soml_on():
    """An eFEM card (rfe 1, not in {3,5,8,17}) at 2.4 GHz SoML-on takes 0x08108000 / [27]=0."""
    bb = _BB()
    chan._switch_band_rxhp(bb, 1, soml_on=True, rfe_type=1)
    assert bb.regs[0x08CC] == 0x08108000
    assert _field(bb, 0x08D8, 1 << 27) == 0


def test_switch_band_rxhp_rfe12_writes_rf_b3():
    """rfe 12/19 writes RF 0xb3 (band-dependent) to both paths (packed into 0xc90/0xe90)."""
    bb2g = _BB()
    chan._switch_band_rxhp(bb2g, 1, soml_on=True, rfe_type=12)
    assert bb2g.regs[0x0C90] == ((0xB3 << 20) | 0x3C360)    # path A, 2.4 GHz
    assert bb2g.regs[0x0E90] == ((0xB3 << 20) | 0x3C360)    # path B
    bb5g = _BB()
    chan._switch_band_rxhp(bb5g, 36, soml_on=True, rfe_type=12)
    assert bb5g.regs[0x0C90] == ((0xB3 << 20) | 0xFC760)    # 5 GHz value
