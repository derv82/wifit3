"""Hardware-free oracle regression for the RTL8188EUS (DKMS) external-PA/LNA variant path.

Every expected value is re-derived from the 8188eu vendor C, NOT from any Python source:
  - efuse decode:   hal/rtl8188e/rtl8188e_hal_init.c  (Hal_ReadPAType_8188E / Hal_ReadAmplifierType_8188E)
  - board words:    hal/hal_dm.c + hal/phydm/rtl8188e/halhwimg8188e_mac.c (check_positive _board_type/driver2)
  - RFE gate:       hal/rtl8188e/rtl8188e_phycfg.c    (PHY_SetRFEReg_8188E)
  - init tables:    hal/phydm/rtl8188e/halhwimg8188e_{mac,bb,rf}.c (board-gated rows, A-cut walk)

A divergence of the Python port from the C is a HARD failure here (not xfail). xfail is used ONLY
for C branches that genuinely cannot be driven through the Python seam (no such parameter exists).
"""
import pytest

from wifit3.chips.rtl8188eus_dkms import bb, efuse, mac, phy_cond, rf
from wifit3.chips.rtl8188eus_dkms.bb_agc_tab_tbl import AGC_TAB
from wifit3.chips.rtl8188eus_dkms.bb_phy_reg_tbl import PHY_REG
from wifit3.chips.rtl8188eus_dkms.constants import REG_AFE_XTAL_CTRL, RF_LSSI_WRITE_A
from wifit3.chips.rtl8188eus_dkms.efuse import BoardOptions
from wifit3.chips.rtl8188eus_dkms.mac_reg_tbl import MAC_REG
from wifit3.chips.rtl8188eus_dkms.rf_radio_a_tbl import RADIO_A


# --------------------------------------------------------------------------- helpers / mocks

def _board(rfe_byte: int) -> BoardOptions:
    """read_board_options over a 512B efuse map whose only meaningful byte is 0xCA."""
    m = bytearray(b"\xFF" * 512)
    m[0xCA] = rfe_byte          # EEPROM_RFE_OPTION_8188E (include/hal_pg.h:88)
    return efuse.read_board_options(bytes(m))


def _walk(table, dw):
    """Run the phydm conditional walker with driver words dw=(driver1,driver2,driver4)."""
    out = []
    phy_cond.walk_table(table, lambda a, d: out.append((a, d)), *dw)
    return out


def _vals(walk, addr):
    return [d for a, d in walk if a == addr]


# The six board driver-word sets under test (build_driver_words(external_lna, external_pa, type_glna)).
def _dw(lna, pa, glna):
    return phy_cond.build_driver_words(lna, pa, glna)


class RegTx:
    """Stateful register fake: write32 updates the register so a later RMW reads it back."""

    def __init__(self, init=None):
        self.regs = dict(init or {})
        self.w32 = []

    def read32(self, a):
        return self.regs.get(a, 0)

    def write32(self, a, v):
        v &= 0xFFFFFFFF
        self.regs[a] = v
        self.w32.append((a, v))


class RecMac:
    """write8/16/32 recorder; reads return 0 (LLT poll reads NO_ACTIVE immediately)."""

    def __init__(self):
        self.w8, self.w16, self.w32 = [], [], []

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
        return 0x00000000


class BbTx:
    """read16/read32 served from a dict; write8/16/32 recorded."""

    def __init__(self, reads=None):
        self.w8, self.w16, self.w32 = [], [], []
        self._reads = dict(reads or {})

    def read16(self, a):
        return self._reads.get((a, 2), 0x0000)

    def read32(self, a):
        return self._reads.get((a, 4), 0x00000000)

    def write8(self, a, v):
        self.w8.append((a, v & 0xFF))

    def write16(self, a, v):
        self.w16.append((a, v & 0xFFFF))

    def write32(self, a, v):
        self.w32.append((a, v & 0xFFFFFFFF))


class RfTx:
    """read32 served from a dict; write32 recorded. Unknown reads -> 0 (SYS_CFG[15:12]=0 < 8)."""

    def __init__(self, reads=None):
        self.w32 = []
        self._reads = dict(reads or {})

    def read32(self, a):
        return self._reads.get(a, 0x00000000)

    def write32(self, a, v):
        self.w32.append((a, v & 0xFFFFFFFF))


# =========================================================================================
# read_board_options  --  PA/LNA select  (0xCA[3:2])   branches 1-4
# C: rtl8188e_hal_init.c Hal_ReadPAType_8188E, PA_LNAType_2G = 0xCA[3:2] (LE_BITS_TO_1BYTE :3058)
# =========================================================================================

def test_pa_lna_select_0_epa_elna():
    b = _board(0x00)                      # 0xCA[3:2] == 0
    # C rtl8188e_hal_init.c:3063-3066  case 0: ExternalPA_2G=1, ExternalLNA_2G=1
    assert (b.external_pa_2g, b.external_lna_2g) == (True, True)


def test_pa_lna_select_1_epa_ilna():
    b = _board(0x04)                      # 0xCA[3:2] == 1  (1<<2)
    # C rtl8188e_hal_init.c:3067-3070  case 1: ExternalPA_2G=1, ExternalLNA_2G=0
    assert (b.external_pa_2g, b.external_lna_2g) == (True, False)


def test_pa_lna_select_2_ipa_elna():
    b = _board(0x08)                      # 0xCA[3:2] == 2  (2<<2)
    # C rtl8188e_hal_init.c:3071-3074  case 2: ExternalPA_2G=0, ExternalLNA_2G=1
    assert (b.external_pa_2g, b.external_lna_2g) == (False, True)


def test_pa_lna_select_3_and_default_ipa_ilna():
    # C rtl8188e_hal_init.c:3075-3080  case 3/default: ExternalPA_2G=0, ExternalLNA_2G=0
    for rfe in (0x0C, 0x0F, 0xFF):        # [3:2]==3 (programmed 0x0C/0x0F, or blank 0xFF)
        b = _board(rfe)
        assert (b.external_pa_2g, b.external_lna_2g) == (False, False)


# =========================================================================================
# read_board_options  --  GLNA gain select  (0xCA[6:4])   branches 5-7
# C: rtl8188e_hal_init.c Hal_ReadAmplifierType_8188E, GLNA_type = 0xCA[6:4] (:3134)
# =========================================================================================

def test_glna_select_0_type_1():
    b = _board(0x08)                      # [6:4]==0, [3:2]==2 (realistic ext-LNA burn)
    # C rtl8188e_hal_init.c:3147-3149  case 0: TypeGLNA = 0x1 (10dB)  -- NOTE 0x1 not 0x0
    assert b.type_glna == 0x1


def test_glna_select_2_type_2():
    b = _board(0x28)                      # [6:4]==2 (2<<4=0x20), [3:2]==2
    # C rtl8188e_hal_init.c:3150-3152  case 2: TypeGLNA = 0x2 (14dB)
    assert b.type_glna == 0x2


def test_glna_select_other_type_0():
    # C rtl8188e_hal_init.c:3153-3155  default: TypeGLNA = 0x0 (others not supported)
    assert _board(0x18).type_glna == 0x0          # [6:4]==1
    assert _board(0x38).type_glna == 0x0          # [6:4]==3
    assert _board(0xFF).type_glna == 0x0          # [6:4]==7 (blank card)


def test_blank_efuse_is_internal_reference():
    # 0xCA==0xFF: [3:2]==3 -> (0,0); [6:4]==7 -> TypeGLNA 0x0.  The internal-reference walk.
    # C rtl8188e_hal_init.c:3075-3080 (PA/LNA) + :3153-3155 (GLNA default)
    b = _board(0xFF)
    assert (b.external_pa_2g, b.external_lna_2g, b.type_glna) == (False, False, 0x0)


def test_pa_lna_and_glna_decode_independently():
    # Single wire byte 0xCA carries both fields; independent decoders.
    # byte 0x28 -> [3:2]==2 (iPA+eLNA) and [6:4]==2 (TypeGLNA 0x2).
    # C rtl8188e_hal_init.c:3071-3074 + :3150-3152
    b = _board(0x28)
    assert (b.external_pa_2g, b.external_lna_2g, b.type_glna) == (False, True, 0x2)


# =========================================================================================
# read_board_options  --  UNREACHABLE C branches (no such Python parameter)   branches 8,9
# =========================================================================================

@pytest.mark.xfail(reason="read_board_options takes no autoload_fail flag; the C autoload-FAIL+AUTO "
                          "path forces GLNA_type=0 -> TypeGLNA=0x1 (rtl8188e_hal_init.c:3138-3139,"
                          ":3147-3148), unreachable from the pure-fn seam", strict=False)
def test_autoload_fail_forces_type_glna_one_UNREACHABLE():
    # On a blank (0xFF) map an autoload-FAIL board would yield TypeGLNA=0x1 in C;
    # the AUTO-only Python decodes the 0xCA[6:4]==7 field to 0x0 instead -> divergence, but
    # unreachable because there is no autoload_fail argument.
    b = _board(0xFF)
    assert b.type_glna == 0x1


@pytest.mark.xfail(reason="read_board_options models only the AUTO path (registry AmplifierType_2G==0); "
                          "the C registry-override path uses rtw_amplifier_type_2g&ODM_BOARD_EXT_PA "
                          "instead of efuse 0xCA (rtl8188e_hal_init.c:3082-3083), unreachable from the seam",
                   strict=False)
def test_registry_override_external_pa_UNREACHABLE():
    # If rtw_amplifier_type_2g had ODM_BOARD_EXT_PA(BIT3) set, C would force ExternalPA_2G=1
    # regardless of the (0xFF -> AUTO -> ext_pa=0) efuse byte. Python cannot express this.
    b = _board(0xFF)
    assert b.external_pa_2g is True


# =========================================================================================
# build_driver_words  --  board-type / type_glna packing   branches 10,11,12
# C: hal_dm.c:228-247 (odm_board_type) -> halhwimg8188e_mac.c:30-54 (_board_type + driver2/driver4)
#    ODM_BOARD_EXT_PA=BIT3, ODM_BOARD_EXT_LNA=BIT4 (phydm_pre_define.h:627-628)
# =========================================================================================

def test_driver_words_external_lna_sets_glna_bit0():
    d1, d2, d4 = _dw(True, False, 0x0)
    # C: ExternalLNA_2G -> board_type|=BIT4 -> _board_type bit0 (GLNA); driver1 low nibble bit0.
    # halhwimg8188e_mac.c:30 & hal_dm.c:228-229
    assert d1 & 0x1F == 0b00001
    # type_glna 0 -> driver2[7:0]==0, driver4==0 (halhwimg8188e_mac.c:44,51)
    assert (d2, d4) == (0x0, 0x0)


def test_driver_words_external_pa_sets_gpa_bit1():
    d1, d2, d4 = _dw(False, True, 0x0)
    # C: ExternalPA_2G -> board_type|=BIT3 -> _board_type bit1 (GPA); driver1 low nibble bit1.
    # halhwimg8188e_mac.c:31 & hal_dm.c:236-237
    assert d1 & 0x1F == 0b00010
    assert (d2, d4) == (0x0, 0x0)


def test_driver_words_both_external_sets_bits01():
    d1, _, _ = _dw(True, True, 0x1)
    # C: both -> _board_type bits 0 and 1 set (halhwimg8188e_mac.c:30-31)
    assert d1 & 0x1F == 0b00011


def test_driver_words_type_glna_lands_in_driver2_lowbyte():
    # C halhwimg8188e_mac.c:44  driver2 = (type_glna & 0xFF) << 0 ; halhwimg8188e_mac.c:51 driver4 high.
    for glna in (0x1, 0x2):
        _, d2, d4 = _dw(True, False, glna)
        assert d2 & 0xFF == glna
        assert d4 == 0x0            # type_glna < 0x100 -> driver4 == 0


def test_driver_words_preserve_nonboard_bits_across_internal_external():
    # build_driver_words only rewrites the low 5 board bits; the cut/interface/platform/package
    # bits (driver1 & ~0x1F) are the same reference word for internal and external.
    ref = _dw(False, False, 0x0)[0]
    for lna, pa, glna in ((True, False, 0x1), (False, True, 0x0), (True, True, 0x2)):
        assert _dw(lna, pa, glna)[0] & ~0x1F == ref & ~0x1F


@pytest.mark.xfail(reason="build_driver_words has no EEPROMBluetoothCoexist parameter; the C BT bit "
                          "(board_type&BIT2 -> _board_type bit4 -> driver1 bit4, hal_dm.c:244-245 & "
                          "halhwimg8188e_mac.c:34) cannot be driven from the seam", strict=False)
def test_bt_coexist_bit_UNREACHABLE():
    # C would set driver1 bit4 when EEPROMBluetoothCoexist!=0; build_driver_words clears it via &~0x1F.
    d1, _, _ = _dw(True, True, 0x2)
    assert d1 & 0x10 != 0            # BT bit; never settable -> xfail


# =========================================================================================
# PHY_SetRFEReg_8188E  --  RFE register gate   branches 14,15
# C: rtl8188e_phycfg.c:2000-2010
# =========================================================================================

def test_rfe_reg_internal_board_no_writes():
    # C rtl8188e_phycfg.c:2000-2001  (ExternalPA_2G==0 && ExternalLNA_2G==0) -> return (no writes).
    t = RegTx({})
    bb.phy_set_rfe_reg(t, BoardOptions(external_pa_2g=False, external_lna_2g=False, type_glna=0x0))
    assert t.w32 == []


def test_rfe_reg_internal_board_with_glna_still_no_writes():
    # Gate is on PA/LNA only, not TypeGLNA. type_glna=0x2 but both externals false -> still returns.
    # C rtl8188e_phycfg.c:2000-2001
    t = RegTx({})
    bb.phy_set_rfe_reg(t, BoardOptions(external_pa_2g=False, external_lna_2g=False, type_glna=0x2))
    assert t.w32 == []


def test_rfe_reg_external_lna_writes_three_rmw():
    # External board (LNA) -> rfe_type 0/default path, three RMW writes.
    # C rtl8188e_phycfg.c:2007  0x40 (BIT2|BIT3)=0x3<<2 -> mask 0x0C set; preserves other bits.
    # C rtl8188e_phycfg.c:2008  0xEE8 BIT28=1 ; C:2009 0x87C BIT0=0.
    t = RegTx({0x40: 0xF0, 0xEE8: 0x00000000, 0x87C: 0x00000001})
    bb.phy_set_rfe_reg(t, BoardOptions(external_pa_2g=False, external_lna_2g=True, type_glna=0x1))
    assert t.w32 == [(0x40, 0xF0 & ~0x0C | 0x0C),   # 0xFC : [3:2] set, upper nibble preserved
                     (0xEE8, 0x10000000),
                     (0x87C, 0x00000000)]


def test_rfe_reg_external_pa_opens_gate():
    # PA-only board also opens the gate (either external flag).  C rtl8188e_phycfg.c:2000.
    t = RegTx({0x40: 0x00000000, 0xEE8: 0x00000000, 0x87C: 0x00000001})
    bb.phy_set_rfe_reg(t, BoardOptions(external_pa_2g=True, external_lna_2g=False, type_glna=0x0))
    assert t.w32 == [(0x40, 0x0000000C), (0xEE8, 0x10000000), (0x87C, 0x00000000)]


# =========================================================================================
# MAC_REG walk  --  board-gated reg 0x040   branch 16
# C: halhwimg8188e_mac.c:120-129  external -> 0x0C ; internal(ELSE) -> 0x00
# =========================================================================================

def test_mac_reg_040_internal_is_zero():
    d = dict(_walk(MAC_REG, _dw(False, False, 0x0)))
    # C halhwimg8188e_mac.c:128-129  ELSE (board_type=0): 0x040 = 0x00
    assert d[0x040] == 0x00


def test_mac_reg_040_external_is_0c():
    # C halhwimg8188e_mac.c:120-127  any external board -> 0x040 = 0x0C
    for dw in (_dw(False, True, 0x0), _dw(True, False, 0x0), _dw(True, False, 0x2), _dw(True, True, 0x1)):
        d = dict(_walk(MAC_REG, dw))
        assert d[0x040] == 0x0C


# =========================================================================================
# PHY_REG walk  --  board-gated rows   branches 17-21
# C: halhwimg8188e_bb.c array_mp_8188e_phy_reg (A-cut walk; 0x88*/0x98* rows are I-cut, inert)
# =========================================================================================

def test_phy_reg_870_external_vs_internal():
    # C halhwimg8188e_bb.c:1199 ELSE 0x07000760 ; :1173-1193 any external 0x07000300
    assert dict(_walk(PHY_REG, _dw(False, False, 0x0)))[0x870] == 0x07000760
    for dw in (_dw(False, True, 0x0), _dw(True, False, 0x0), _dw(True, False, 0x2)):
        assert dict(_walk(PHY_REG, dw))[0x870] == 0x07000300


def test_phy_reg_824_type_glna2_only():
    # C halhwimg8188e_bb.c:1152 ELSE 0x00390204 ; :1146 ext-LNA glna2 (0x90000001 c2=2) 0x00390004
    assert dict(_walk(PHY_REG, _dw(False, False, 0x0)))[0x824] == 0x00390204   # internal
    assert dict(_walk(PHY_REG, _dw(True, False, 0x2)))[0x824] == 0x00390004    # ext-LNA glna2
    assert dict(_walk(PHY_REG, _dw(True, False, 0x1)))[0x824] == 0x00390204    # ext-LNA glna1 (:1144)
    assert dict(_walk(PHY_REG, _dw(False, True, 0x0)))[0x824] == 0x00390204    # ext-PA (:1140 GPA row)


def test_phy_reg_extpa_gain_rows():
    # ext-PA (GPA) rows differ; ext-LNA-only leaves them at the internal values.
    internal = dict(_walk(PHY_REG, _dw(False, False, 0x0)))
    ext_pa = dict(_walk(PHY_REG, _dw(False, True, 0x0)))
    # C halhwimg8188e_bb.c:1307-1309 (ELSE) vs :1283-1285 (0x90000002 GPA)
    assert (internal[0xA20], internal[0xA24], internal[0xA28]) == (0x1A1B0000, 0x090E1317, 0x00000204)
    assert (ext_pa[0xA20], ext_pa[0xA24], ext_pa[0xA28]) == (0x13130000, 0x060A0D10, 0x00000103)
    # C halhwimg8188e_bb.c:1489 (ELSE) 0x390000E4 vs :1477 (GPA) 0x2D4000B5
    assert internal[0xC80] == 0x390000E4
    assert ext_pa[0xC80] == 0x2D4000B5
    # ext-LNA-only does NOT move the GPA rows (still internal values).
    ext_lna = dict(_walk(PHY_REG, _dw(True, False, 0x2)))
    assert (ext_lna[0xA20], ext_lna[0xC80]) == (0x1A1B0000, 0x390000E4)


def test_phy_reg_any_external_rows():
    # C halhwimg8188e_bb.c:1372 ELSE 0xB2C=0x80000000 ; :1358-1366 external 0x00000000
    # C halhwimg8188e_bb.c:1636 ELSE 0xEE8=0x21555448 ; :1610-1630 external 0x32555448
    internal = dict(_walk(PHY_REG, _dw(False, False, 0x0)))
    assert (internal[0xB2C], internal[0xEE8]) == (0x80000000, 0x21555448)
    for dw in (_dw(False, True, 0x0), _dw(True, False, 0x0), _dw(True, False, 0x2)):
        d = dict(_walk(PHY_REG, dw))
        assert (d[0xB2C], d[0xEE8]) == (0x00000000, 0x32555448)


def test_phy_reg_glna2_rows():
    # C halhwimg8188e_bb.c:1248 ELSE 0xA0C=0x2E7F120F ; :1242 glna2 0x2D38120F
    # C halhwimg8188e_bb.c:1343 ELSE 0xA80=0x218075B1 ; :1337 glna2 0x21807530
    internal = dict(_walk(PHY_REG, _dw(False, False, 0x0)))
    assert (internal[0xA0C], internal[0xA80]) == (0x2E7F120F, 0x218075B1)
    g2 = dict(_walk(PHY_REG, _dw(True, False, 0x2)))
    assert (g2[0xA0C], g2[0xA80]) == (0x2D38120F, 0x21807530)
    g1 = dict(_walk(PHY_REG, _dw(True, False, 0x1)))
    assert (g1[0xA0C], g1[0xA80]) == (0x2E7F120F, 0x218075B1)   # glna1 unchanged


# =========================================================================================
# AGC_TAB walk  --  0xC78 GLNA gain table   branch 22  (gated on GLNA, not GPA)
# C: halhwimg8188e_bb.c array_mp_8188e_agc_tab first IF/ENDIF block (:118-154)
# =========================================================================================

def test_agc_c78_internal_block1():
    c78 = _vals(_walk(AGC_TAB, _dw(False, False, 0x0)), 0xC78)
    # C halhwimg8188e_bb.c:149-153  ELSE (internal) block-1 gain rows
    assert c78[:5] == [0xFB000001, 0xFB010001, 0xFB020001, 0xFB030001, 0xFB040001]


def test_agc_c78_ext_lna_glna1_block1():
    c78 = _vals(_walk(AGC_TAB, _dw(True, False, 0x1)), 0xC78)
    # C halhwimg8188e_bb.c:131-135  0x90000001 c2=1 (ext-LNA, glna1) block-1 gain rows
    assert c78[:5] == [0xF9000001, 0xF8010001, 0xF7020001, 0xF6030001, 0xF5040001]


def test_agc_c78_ext_lna_glna2_block1():
    c78 = _vals(_walk(AGC_TAB, _dw(True, False, 0x2)), 0xC78)
    # C halhwimg8188e_bb.c:143-147  0x90000001 c2=2 (ext-LNA, glna2) block-1 gain rows
    assert c78[:5] == [0xFF000001, 0xFF010001, 0xFE020001, 0xFD030001, 0xFC040001]


def test_agc_c78_ext_lna_glna0_block1():
    c78 = _vals(_walk(AGC_TAB, _dw(True, False, 0x0)), 0xC78)
    # C halhwimg8188e_bb.c:125-129  0x90000001 c2=0 (ext-LNA, glna0) block-1 gain rows
    assert c78[:5] == [0xF7000001, 0xF6010001, 0xF5020001, 0xF4030001, 0xF3040001]


def test_agc_c78_ext_pa_only_matches_internal():
    # GLNA-only gate: an ext-PA-only board takes the ELSE (internal) AGC rows -- no diff.
    # C halhwimg8188e_bb.c:124 (0x90000001 is GLNA bit0) -> unmatched by GPA-only board -> :149 ELSE
    internal = _vals(_walk(AGC_TAB, _dw(False, False, 0x0)), 0xC78)
    ext_pa = _vals(_walk(AGC_TAB, _dw(False, True, 0x0)), 0xC78)
    assert ext_pa[:5] == internal[:5] == [0xFB000001, 0xFB010001, 0xFB020001, 0xFB030001, 0xFB040001]


# =========================================================================================
# RADIO_A walk  --  board-gated RF rows   branches 23,24
# C: halhwimg8188e_rf.c array_mp_8188e_radioa (A-cut walk)
# =========================================================================================

def test_rf_034_extpa_gain_table():
    # C halhwimg8188e_rf.c:313 ELSE 0x034 first word 0x0000ADF3 ; :301 (0x90000002 GPA) 0x0000A095
    assert _vals(_walk(RADIO_A, _dw(False, False, 0x0)), 0x034)[0] == 0x0000ADF3   # internal
    assert _vals(_walk(RADIO_A, _dw(False, True, 0x0)), 0x034)[0] == 0x0000A095    # ext-PA
    # ext-LNA-only leaves 0x034 at the internal table (GPA-gated only).
    assert _vals(_walk(RADIO_A, _dw(True, False, 0x2)), 0x034)[0] == 0x0000ADF3


def test_rf_087_glna2():
    # C halhwimg8188e_rf.c:371 ELSE 0x087=0x00048A00 ; :362 (0x90000001 c2=2) 0x00079F80
    assert _vals(_walk(RADIO_A, _dw(False, False, 0x0)), 0x087)[0] == 0x00048A00    # internal
    assert _vals(_walk(RADIO_A, _dw(True, False, 0x2)), 0x087)[0] == 0x00079F80     # ext-LNA glna2
    assert _vals(_walk(RADIO_A, _dw(True, False, 0x1)), 0x087)[0] == 0x00048A00     # glna1 unchanged
    # both-external glna2 also fires the glna2 value via the 0x90000003 row (:338).
    assert _vals(_walk(RADIO_A, _dw(True, True, 0x2)), 0x087)[0] == 0x00079F80


def test_rf_03b_glna2_first_word():
    # C halhwimg8188e_rf.c:629 ELSE 0x03B first word 0x000F02B0 ; :575 (0x90000001 c2=2) 0x000F07B0
    assert _vals(_walk(RADIO_A, _dw(False, False, 0x0)), 0x03B)[0] == 0x000F02B0    # internal
    assert _vals(_walk(RADIO_A, _dw(True, False, 0x2)), 0x03B)[0] == 0x000F07B0     # ext-LNA glna2
    assert _vals(_walk(RADIO_A, _dw(True, False, 0x1)), 0x03B)[0] == 0x000F02B0     # glna1 unchanged


# =========================================================================================
# I-cut condition rows are inert on the A-cut walk   branch 25
# C: check_positive value-check on cut[27:24]; 0x88*/0x98* need ODM_CUT_I(8) (phydm_pre_define.h:532).
#    build_driver_words bakes ODM_CUT_A(0) so those rows never fire.
# =========================================================================================

def test_icut_only_value_never_emitted_on_a_cut():
    # 0xA80 value 0x21807531 comes ONLY from the I-cut header 0x98000000 (halhwimg8188e_bb.c:1340-1341).
    # On any A-cut board it must never be written (value-check rejects cut nibble 8).
    for dw in (_dw(False, False, 0x0), _dw(True, False, 0x2), _dw(False, True, 0x0), _dw(True, True, 0x2)):
        assert 0x21807531 not in _vals(_walk(PHY_REG, dw), 0xA80)


# =========================================================================================
# Integration: board gating threads through the real config entry points (Rec-mock)
# =========================================================================================

def test_phy_mac_config_threads_board_gating():
    # C: PHY_MACConfig8188E walks the MAC table with the composed driver words.
    internal = RecMac()
    mac.phy_mac_config(internal)                                   # default -> internal reference
    # C halhwimg8188e_mac.c:129  0x040 = 0x00
    assert [v for a, v in internal.w8 if a == 0x040] == [0x00]

    ext = RecMac()
    mac.phy_mac_config(ext, driver_words=_dw(False, True, 0x0))    # ext-PA driver words
    # C halhwimg8188e_mac.c:121  0x040 = 0x0C
    assert [v for a, v in ext.w8 if a == 0x040] == [0x0C]


def test_phy_bb_config_threads_board_gating():
    reads = {(0x0002, 2): 0xFC1C, (REG_AFE_XTAL_CTRL, 4): 0x350007FF}
    internal = BbTx(reads=reads)
    bb.phy_bb_config(internal, crystal_cap=0x20, driver_words=_dw(False, False, 0x0))
    # C halhwimg8188e_bb.c:1199  ELSE 0x870 = 0x07000760
    assert (0x870, 0x07000760) in internal.w32
    assert (0x870, 0x07000300) not in internal.w32

    ext = BbTx(reads=reads)
    bb.phy_bb_config(ext, crystal_cap=0x20, driver_words=_dw(True, False, 0x0))
    # C halhwimg8188e_bb.c:1188  external 0x870 = 0x07000300
    assert (0x870, 0x07000300) in ext.w32
    assert (0x870, 0x07000760) not in ext.w32


def test_phy_rf_config_threads_glna2_into_lssi_write():
    # RF rows are emitted as LSSI DataAndAddr: 0x840 <- (rf_addr<<20)|(data & 0xFFFFF).
    reads = {0x0870: 0x07000760, 0x0860: 0x66F60110, 0x0824: 0x00390204}
    internal_word = (0x87 << 20) | (0x00048A00 & 0xFFFFF)          # C halhwimg8188e_rf.c:371
    glna2_word = (0x87 << 20) | (0x00079F80 & 0xFFFFF)            # C halhwimg8188e_rf.c:362

    internal = RfTx(reads=reads)
    rf.phy_rf_config(internal, driver_words=_dw(False, False, 0x0))
    assert (RF_LSSI_WRITE_A, internal_word) in internal.w32
    assert (RF_LSSI_WRITE_A, glna2_word) not in internal.w32

    ext = RfTx(reads=reads)
    rf.phy_rf_config(ext, driver_words=_dw(True, False, 0x2))
    assert (RF_LSSI_WRITE_A, glna2_word) in ext.w32
    assert (RF_LSSI_WRITE_A, internal_word) not in ext.w32
