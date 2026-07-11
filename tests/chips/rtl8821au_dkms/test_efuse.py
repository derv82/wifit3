"""Hardware-free regression for the EFUSE-derived branch selectors.

The reference AWUS036ACS burns blank amplifier bytes (0x00 -> all flags off), so the
generalized decode must reproduce the reference (board_type 0 / ext_lna_2g False) while
lighting the right ODM board bits for an external-PA/LNA card. Also pins that board_type
actually changes which phy_cond rows a card walks (the whole point of threading it).
"""
from types import SimpleNamespace

from wifit3.chips.rtl8821au_dkms import efuse
from wifit3.chips.rtl8821au_dkms.phy_cond import JaguarParams, apply_table


def _map(pa=0x00, lna2g=0x00, lna5g=0x00):
    """512 B logical efuse map with the amplifier bytes set (rest unburned = 0xFF)."""
    m = bytearray(b"\xFF" * 512)
    m[efuse.C.EEPROM_PA_TYPE_8821AU] = pa
    m[efuse.C.EEPROM_LNA_TYPE_2G_8821AU] = lna2g
    m[efuse.C.EEPROM_LNA_TYPE_5G_8821AU] = lna5g
    return bytes(m)


def test_reference_all_internal():
    # AWUS036ACS reads 0x00 across the amplifier bytes -> every external flag off.
    assert efuse._ext_amplifier_flags(_map(), autoload_fail=False) == (False, False, False, False)
    assert efuse._parse_board_type((False, False, False, False)) == 0


def test_autoload_fail_forces_internal():
    # An autoload-fail efuse takes the registry-AUTO else path -> all flags 0.
    assert efuse._ext_amplifier_flags(_map(pa=0xFF, lna2g=0xFF, lna5g=0xFF),
                                      autoload_fail=True) == (False, False, False, False)


def test_blank_bytes_default_zero():
    # 0xFF (unburned) bytes decode to 0, same as internal.
    assert efuse._ext_amplifier_flags(_map(pa=0xFF, lna2g=0xFF, lna5g=0xFF),
                                      autoload_fail=False) == (False, False, False, False)


def test_ext_lna_2g_single_bit():
    # ExternalLNA_2G keys on LNAType_2G[3] only.
    assert efuse._ext_amplifier_flags(_map(lna2g=0x08), autoload_fail=False)[1] is True
    assert efuse._ext_amplifier_flags(_map(lna2g=0x04), autoload_fail=False)[1] is False


def test_ext_flags_full_decode():
    # PA[4]=ext_pa_2g, PA[0]=ext_pa_5g (both from 0xBC); LNA2G[3]; LNA5G[3].
    flags = efuse._ext_amplifier_flags(_map(pa=0x11, lna2g=0x08, lna5g=0x08), autoload_fail=False)
    assert flags == (True, True, True, True)   # pa&0x10, lna2g&0x08, pa&0x01, lna5g&0x08


def test_board_type_bits():
    bt = efuse._parse_board_type((True, True, True, True))
    assert bt == (efuse.ODM_BOARD_EXT_PA_2G | efuse.ODM_BOARD_EXT_LNA_2G
                  | efuse.ODM_BOARD_EXT_PA_5G | efuse.ODM_BOARD_EXT_LNA_5G)
    assert efuse._parse_board_type((False, True, False, False)) == efuse.ODM_BOARD_EXT_LNA_2G


def test_build_jaguar_params_threads_board_type():
    jp = efuse.build_jaguar_params(SimpleNamespace(board_type=0x98))
    assert jp.board_type == 0x98
    assert jp.cut_version == 0          # 8821 tables carry no cut-gated rows
    assert (jp.support_interface, jp.support_platform) == (0x02, 0x04)   # USB / CE defaults
    assert (jp.type_glna, jp.type_gpa, jp.type_alna, jp.type_apa) == (0, 0, 0, 0)


# A synthetic phy_cond block gated exactly like the real 8821a AGC row 0x8000020c:
# taken only when the USB interface AND the ALNA|APA (5 GHz ext PA+LNA) board bits are set.
_BOARD_GATED = [
    0x8000020C, 0x00000000,   # IF: interface-USB + ALNA(bit2) + APA(bit3)
    0x40000000, 0x00000000,   # paired negative-condition row (type_* all 0)
    0x00000100, 0x0000AAAA,   # data row — only emitted when the IF matches
    0xB0000000, 0x00000000,   # ENDIF
]


def _collect(table, params):
    out = []
    apply_table(table, lambda a, v: out.append((a, v)), params)
    return out


def test_board_type_gates_walker_row():
    # Reference (board_type 0): the ALNA|APA-gated row is skipped.
    assert _collect(_BOARD_GATED, JaguarParams(board_type=0)) == []
    # Ext 5 GHz PA+LNA card (APA|ALNA): the same row is now walked.
    variant = efuse.ODM_BOARD_EXT_PA_5G | efuse.ODM_BOARD_EXT_LNA_5G
    assert _collect(_BOARD_GATED, JaguarParams(board_type=variant)) == [(0x100, 0xAAAA)]
