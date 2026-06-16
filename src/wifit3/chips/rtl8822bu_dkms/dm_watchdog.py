"""RTL8822BU runtime PHYDM watchdog — the per-hop + ~2 s loop the vendor runs during RX.

Ports `phydm_watchdog` (phydm.c:2384) for the jaguar2 (11AC) path. The loop reads the BB
false-alarm / CCA / CRC32 counters, then DIG adapts the RX IGI (0xC50/0xE50) from those counts.
Without it the IGI is frozen at the `dig_init` seed and the RX gain never tracks the channel's
false-alarm rate — measured as a far lower beacon rate than the vendor capture sustains.

Every `odm_get_bb_reg` is a full 32-bit BB read (a control read of the BB address); the field mask
is applied in software. So the read SEQUENCE is what the byte-for-byte gate checks.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FaCnt:
    """[SRC] phydm_fa_struct — the false-alarm / CCA / CRC32 counter snapshot DIG consumes."""
    cck_fail: int = 0
    ofdm_fail: int = 0
    cnt_all: int = 0
    cck_cca: int = 0
    ofdm_cca: int = 0
    cnt_cca_all: int = 0


def fa_cnt_statistics_ac(t) -> FaCnt:
    """[SRC] phydm_fa_cnt_statistics_ac (phydm_dig.c:1801) — read the jaguar2 FA/CCA/CRC32 counters.

    The 20-read sequence (0xF50, 0xFCC, 0xFC8, 0xFCC, 0xFD0, 0xFBC, 0xFC0, 0xFC4, 0xFC8, 0xF48, 0xA5C,
    0xF08, 0xF04, 0xF14, 0xF1C, 0xF10, 0xF18, 0xF0C, 0xF54, 0x808) is byte-identical to the watchdog's
    wire. Only `cnt_all` (total FA) and `cnt_cca_all` (total CCA) feed DIG; the rest are read (the HW
    requires the read to advance the counter latch) but kept only where DIG needs them."""
    fa = FaCnt()
    t.read32(0xF50)                                # {cck,ofdm}_txen — unused by DIG
    t.read32(0xFCC)                                # cck_txon (LWORD)
    t.read32(0xFC8)                                # ofdm_txon (HWORD)
    t.read32(0xFCC)                                # TYPE1: fast_fsync (HWORD)
    t.read32(0xFD0)                                # TYPE2: sb_search_fail
    t.read32(0xFBC)                                # TYPE3: parity_fail / rate_illegal
    t.read32(0xFC0)                                # TYPE4: crc8_fail / mcs_fail
    t.read32(0xFC4)                                # TYPE5: vht crc8
    t.read32(0xFC8)                                # TYPE6: mcs_fail_vht (LWORD)
    fa.ofdm_fail = t.read32(0xF48) & 0xFFFF        # OFDM FA counter
    fa.cck_fail = t.read32(0xA5C) & 0xFFFF         # CCK FA counter
    v = t.read32(0xF08)                            # CCK/OFDM CCA
    fa.ofdm_cca = (v >> 16) & 0xFFFF
    fa.cck_cca = v & 0xFFFF
    t.read32(0xF04)                                # CCK CRC32
    t.read32(0xF14)                                # OFDM CRC32
    t.read32(0xF1C)                                # OFDM2 CRC32
    t.read32(0xF10)                                # HT CRC32
    t.read32(0xF18)                                # HT2 CRC32
    t.read32(0xF0C)                                # VHT CRC32
    t.read32(0xF54)                                # VHT2 CRC32
    cck_enable = t.read32(0x808) & (1 << 28)       # ODM_REG_BB_RX_PATH[28]: CCK block on (2.4 GHz)
    if cck_enable:
        fa.cnt_all = fa.ofdm_fail + fa.cck_fail
        fa.cnt_cca_all = fa.cck_cca + fa.ofdm_cca
    else:
        fa.cnt_all = fa.ofdm_fail
        fa.cnt_cca_all = fa.ofdm_cca
    return fa
