"""The TX-AGC values the vendor rtl88x2cu driver put on the wire for the recorded D-Link AC13U.

These four tables were the frozen constants in ``phy.py`` before the TX power port replaced them
with the real computation. They are kept here, owned by the tests, as the regression oracle for
``txpower.py`` (the PG bases), ``txpwr_tables.py`` (the power by rate offsets) and
``txpwr_index.py`` (the composed index). Read off the capture, not from the port, so a change in
the port cannot move them.

``BYRATE_OFFSET`` carries the vendor's 7 bit wire encoding: the four VHT MCS8/MCS9 entries 0x7C
and 0x78 are -4 and -8, so a comparison against a computed TX gain index must sign extend them.
"""
from __future__ import annotations

# Power by rate offset of each rate from its section reference (11M for CCK, MCS7 for OFDM/HT/VHT),
# as config_phydm_set_txagc_to_hw_8822c writes it: 4 rates per dword to 0x3A00 + (rate & 0xfc)
# [SRC phydm_hal_api8822c.c:522-580].
DIFF_GROUP_DWORD = {
    0x00: 0x0004080C, 0x04: 0x10140808, 0x08: 0x0004080C, 0x0C: 0x10140C0C,
    0x10: 0x0004080C, 0x14: 0x10140C0C, 0x18: 0x0004080C, 0x2C: 0x10140C0C,
    0x30: 0x0004080C, 0x34: 0x0C0C787C, 0x38: 0x080C1014, 0x3C: 0x787C0004,
}
BYRATE_OFFSET = {base + k: (dword >> (8 * k)) & 0xFF
                 for base, dword in DIFF_GROUP_DWORD.items() for k in range(4)}

# Per channel section reference power index, the output of hal_com_get_txpwr_idx at each section's
# reference rate. 2.4 GHz: (cck_ref_A, cck_ref_B, ofdm_ref_A, ofdm_ref_B) per channel.
SECTION_REF_2G = {1: (0x49, 0x56, 0x3F, 0x46), 2: (0x49, 0x56, 0x3F, 0x46), 3: (0x48, 0x58, 0x3F, 0x47),
           4: (0x48, 0x58, 0x3F, 0x47), 5: (0x48, 0x58, 0x3F, 0x47), 6: (0x4B, 0x58, 0x42, 0x48),
           7: (0x4B, 0x58, 0x42, 0x48), 8: (0x4B, 0x58, 0x42, 0x48), 9: (0x49, 0x58, 0x42, 0x49),
           10: (0x49, 0x58, 0x42, 0x49), 11: (0x49, 0x58, 0x42, 0x49), 12: (0x4C, 0x58, 0x43, 0x4A),
           13: (0x4C, 0x58, 0x43, 0x4A), 14: (0x48, 0x56, 0x43, 0x4A)}
# 5 GHz has no CCK section [SRC rtl8822c_phy.c:662], so only (ofdm_ref_A, ofdm_ref_B) is recorded.
SECTION_REF_5G_OFDM = {36: (0x48, 0x49), 40: (0x48, 0x49), 44: (0x48, 0x4A), 48: (0x48, 0x4A),
           52: (0x49, 0x4B), 56: (0x49, 0x4B), 60: (0x49, 0x4A), 64: (0x49, 0x4A),
           100: (0x4A, 0x48), 104: (0x4A, 0x48), 108: (0x4B, 0x48), 112: (0x4B, 0x48),
           116: (0x4A, 0x47), 120: (0x4A, 0x47), 124: (0x47, 0x44), 128: (0x47, 0x44),
           132: (0x44, 0x43), 136: (0x44, 0x43), 140: (0x44, 0x42), 144: (0x44, 0x42),
           149: (0x45, 0x42), 153: (0x45, 0x42), 157: (0x46, 0x45), 161: (0x46, 0x45),
           165: (0x46, 0x43)}


# Tamper check. These are recorded observations, not derivable values: an edit here silently
# redefines "correct" for the thousands of comparisons in test_txpower_pg, test_txpwr_tables,
# test_txpwr_index and test_txagc. ENTRY_COUNTS and DIGEST are asserted by
# test_txagc.test_the_recorded_oracle_is_unedited, so an edit fails loudly instead.
ENTRY_COUNTS = {"DIFF_GROUP_DWORD": 12, "BYRATE_OFFSET": 48,
                "SECTION_REF_2G": 14, "SECTION_REF_5G_OFDM": 25}
DIGEST = "79f4f1389e3c45f15cb742ad05e2976c2b3061a551559620741c5762ad894729"


def digest() -> str:
    """sha256 over the four tables in a canonical, order independent form."""
    import hashlib

    parts = []
    for name, table in (("DIFF_GROUP_DWORD", DIFF_GROUP_DWORD), ("BYRATE_OFFSET", BYRATE_OFFSET),
                        ("SECTION_REF_2G", SECTION_REF_2G),
                        ("SECTION_REF_5G_OFDM", SECTION_REF_5G_OFDM)):
        for key in sorted(table):
            parts.append(f"{name}[{key}]={table[key]}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()
