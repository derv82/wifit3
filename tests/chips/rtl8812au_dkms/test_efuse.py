"""Hardware-free regression for the EFUSE discriminator decode generalized for any-card support.

Hal_ReadRFEType_8812A's BIT7 external-PA/LNA decode + the type-4 workaround, and the C-cut
silicon test, were previously hardcoded to the ALFA's values. These pin the ported vendor
branches (the captured card is byte-diffed by `scripts/chips/rtl8812au_dkms/verify_pcap.py`).
"""
from wifit3.chips.rtl8812au_dkms import efuse

# ext tuple order: (ext_pa_2g, ext_lna_2g, ext_pa_5g, ext_lna_5g)
_NO_EXT = (False, False, False, False)


def _map(**kv) -> bytes:
    """Build a 256-byte logical map; kwargs are `bNN=value` where NN is the hex offset."""
    m = bytearray(0x100)
    for name, val in kv.items():
        m[int(name[1:], 16)] = val
    return bytes(m)


# --- Hal_ReadRFEType_8812A ----------------------------------------------------------------

def test_rfe_type_reference_burn_is_3():
    # The captured AWUS036ACH: 0xCA[5:0] = 3, BIT7 clear.
    assert efuse._parse_rfe_type(_map(bCA=0x03), _NO_EXT, False) == 3


def test_rfe_type_bit7_full_external_is_3():
    assert efuse._parse_rfe_type(_map(bCA=0x80), (True, True, True, True), False) == 3


def test_rfe_type_bit7_5g_extpa_extlna_but_not_2g_is_0():
    # ext_lna_5g && ext_pa_5g but 2G not both ext -> 0.
    assert efuse._parse_rfe_type(_map(bCA=0x80), (False, False, True, True), False) == 0


def test_rfe_type_bit7_5g_extlna_no_extpa_is_2():
    assert efuse._parse_rfe_type(_map(bCA=0x80), (False, False, False, True), False) == 2


def test_rfe_type_bit7_no_5g_extlna_is_4():
    assert efuse._parse_rfe_type(_map(bCA=0x80), (True, True, True, False), False) == 4


def test_rfe_type_4_with_external_amp_workaround_is_0():
    # BIT7 clear, 0xCA[5:0] == 4 AND some ext amp present -> 8812AU forces 0.
    assert efuse._parse_rfe_type(_map(bCA=0x04), (True, False, False, False), False) == 0
    # ...but a plain type-4 burn with no ext amp stays 4.
    assert efuse._parse_rfe_type(_map(bCA=0x04), _NO_EXT, False) == 4


def test_rfe_type_blank_or_autoload_fail_is_0():
    assert efuse._parse_rfe_type(bytes(b"\xff" * 0x100), _NO_EXT, False) == 0   # 0xFF blank
    assert efuse._parse_rfe_type(_map(bCA=0x03), _NO_EXT, True) == 0            # autoload fail


# --- external-PA/LNA flag decode ----------------------------------------------------------

def test_ext_amplifier_flags_reference_all_off():
    assert efuse._ext_amplifier_flags(_map(bBC=0x00, bBD=0x00, bBF=0x00)) == _NO_EXT


def test_ext_amplifier_flags_2g_pa_needs_both_bits():
    assert efuse._ext_amplifier_flags(_map(bBC=(1 << 5) | (1 << 4)))[0] is True   # ext_pa_2g
    assert efuse._ext_amplifier_flags(_map(bBC=(1 << 5)))[0] is False              # only one bit


# --- IS_VENDOR_8812A_C_CUT (CUTVersion = REG_SYS_CFG[15:12] + 1) ---------------------------

def test_is_c_cut_from_sys_cfg():
    assert efuse._parse_is_c_cut(0x04411137) is True    # [15:12]=1 -> CUTVersion 2 = C_CUT (ALFA)
    assert efuse._parse_is_c_cut(0x04410137) is False   # [15:12]=0 -> CUTVersion 1 (B_CUT)
    assert efuse._parse_is_c_cut(0x04412137) is False   # [15:12]=2 -> CUTVersion 3
