"""RTL8822C k-free: the per-board RF trim the factory burned into EFUSE.

Every value written here is decoded from an EFUSE byte, so a board with different fuses gets
different trim. [SRC hal/phydm/halrf/halrf_kfree.c]
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .efuse import EfuseInfo, one_byte_read
from .phy import set_rf_reg
from .transport import RTL8822CUTransport

RFREGOFFSETMASK = 0x000FFFFF
KFREE_BAND_NUM = 9

# [SRC halrf_kfree.h:31,40-48]
KFREE_FLAG_ON = 1 << 0
KFREE_FLAG_THERMAL_K_ON = 1 << 1
KFREE_FLAG_ON_2G = 1 << 2
KFREE_FLAG_ON_5G = 1 << 3
PA_BIAS_FLAG_ON = 1 << 4
TSSI_TRIM_FLAG_ON = 1 << 5

# EFUSE offsets [SRC hal/phydm/halrf/halrf_kfree.h:104-140]
PPG_THERMAL_A_OFFSET_22C = 0x1EF
PPG_THERMAL_B_OFFSET_22C = 0x1B0
PPG_2GL_TXAB_22C = 0x1D4
PPG_2GM_TXAB_22C = 0x1EE
PPG_2GH_TXAB_22C = 0x1D2
PPG_5GL1_TXA_22C, PPG_5GL1_TXB_22C = 0x1EC, 0x1EB
PPG_5GL2_TXA_22C, PPG_5GL2_TXB_22C = 0x1E8, 0x1E7
PPG_5GM1_TXA_22C, PPG_5GM1_TXB_22C = 0x1E4, 0x1E3
PPG_5GM2_TXA_22C, PPG_5GM2_TXB_22C = 0x1E0, 0x1DF
PPG_5GH1_TXA_22C, PPG_5GH1_TXB_22C = 0x1DC, 0x1DB
PPG_PABIAS_2GA_22C, PPG_PABIAS_2GB_22C = 0x1D6, 0x1D5
PPG_PABIAS_5GA_22C, PPG_PABIAS_5GB_22C = 0x1D8, 0x1D7
# TSSI_2GM/2GH/5GL1/5GL2/5GM1/5GM2/5GH1/5GH2, each TXA then TXB, descending from 0x1c0.
TSSI_TRIM_22C = tuple(range(0x1C0, 0x1B0, -1))


@dataclass
class PowerTrimState:
    """odm_power_trim_data: the decoded EFUSE trim, kept for the TX-power path.
    [SRC phydm/phydm.h]"""
    flag: int = 0
    pa_bias_flag: int = 0
    bb_gain: list = field(default_factory=lambda: [[0] * 2 for _ in range(KFREE_BAND_NUM)])
    tssi_trim: list = field(default_factory=lambda: [[0] * 2 for _ in range(KFREE_BAND_NUM)])


def get_set_thermal_trim_offset(t: RTL8822CUTransport, efuse: EfuseInfo,
                                trim: PowerTrimState) -> None:
    """phydm_get_set_thermal_trim_offset_8822c: the thermal-meter offset, stored in EFUSE with
    its low bit rotated to the top of the nibble. [SRC halrf_kfree.c:1075]"""
    pg_therm = one_byte_read(t, efuse.physical_map, PPG_THERMAL_A_OFFSET_22C)
    if pg_therm == 0xFF:
        return
    pg_therm &= 0x1F
    set_rf_reg(t, 0, 0x43, 0x000F0000, ((pg_therm & 0x1) << 3) | ((pg_therm >> 1) & 0x7))
    pg_therm = one_byte_read(t, efuse.physical_map, PPG_THERMAL_B_OFFSET_22C) & 0x1F
    set_rf_reg(t, 1, 0x43, 0x000F0000, ((pg_therm & 0x1) << 3) | ((pg_therm >> 1) & 0x7))
    trim.flag |= KFREE_FLAG_THERMAL_K_ON


# The 15 gain-table slots each path is programmed with, as bb_gain band indices. The 5 GHz
# bands repeat because the table has more slots than EFUSE bands. [SRC halrf_kfree.c:1125-1169]
_BB_GAIN_SLOTS = (0, 1, 2, 2, 3, 4, 5, 6, 7, 3, 4, 5, 6, 7, 7)


def set_power_trim_offset(t: RTL8822CUTransport, trim: PowerTrimState) -> None:
    """phydm_set_power_trim_offset_8822c: push the decoded per-band gain into the RF gain
    table, one slot per RF 0x33 index. [SRC halrf_kfree.c:1115]"""
    for path in (0, 1):
        set_rf_reg(t, path, 0xEE, 1 << 19, 1)
        for slot, band in enumerate(_BB_GAIN_SLOTS):
            set_rf_reg(t, path, 0x33, RFREGOFFSETMASK, slot)
            set_rf_reg(t, path, 0x3F, RFREGOFFSETMASK, trim.bb_gain[band][path])
        set_rf_reg(t, path, 0xEE, 1 << 19, 0)


def get_set_power_trim_offset(t: RTL8822CUTransport, efuse: EfuseInfo,
                              trim: PowerTrimState) -> None:
    """phydm_get_set_power_trim_offset_8822c: decode the per-band TX gain trim. The 2.4 GHz
    bands pack both paths into one byte; each 5 GHz band uses a byte per path. An unprogrammed
    byte reads 0xff and contributes no trim. [SRC halrf_kfree.c:1175]"""
    probe = [one_byte_read(t, efuse.physical_map, addr)      # all five are read, then tested
             for addr in (PPG_2GL_TXAB_22C, PPG_2GM_TXAB_22C, PPG_2GH_TXAB_22C,
                          PPG_5GL1_TXA_22C, PPG_5GL1_TXB_22C)]
    if all(value == 0xFF for value in probe):
        return

    def byte(addr: int) -> int:
        value = one_byte_read(t, efuse.physical_map, addr)
        return 0 if value == 0xFF else value

    for band, addr in enumerate((PPG_2GL_TXAB_22C, PPG_2GM_TXAB_22C, PPG_2GH_TXAB_22C)):
        packed = byte(addr)
        trim.bb_gain[band][0] = packed & 0xF
        trim.bb_gain[band][1] = (packed & 0xF0) >> 4
    for band, (addr_a, addr_b) in enumerate((
            (PPG_5GL1_TXA_22C, PPG_5GL1_TXB_22C), (PPG_5GL2_TXA_22C, PPG_5GL2_TXB_22C),
            (PPG_5GM1_TXA_22C, PPG_5GM1_TXB_22C), (PPG_5GM2_TXA_22C, PPG_5GM2_TXB_22C),
            (PPG_5GH1_TXA_22C, PPG_5GH1_TXB_22C)), start=3):
        trim.bb_gain[band][0] = byte(addr_a) & 0x1F
        trim.bb_gain[band][1] = byte(addr_b) & 0x1F
    trim.flag |= KFREE_FLAG_ON | KFREE_FLAG_ON_2G | KFREE_FLAG_ON_5G
    set_power_trim_offset(t, trim)


def get_set_pa_bias_offset(t: RTL8822CUTransport, efuse: EfuseInfo,
                           trim: PowerTrimState) -> None:
    """phydm_get_set_pa_bias_offset_8822c: the PA bias trim, per band and path, into the two
    nibbles of RF 0x60. [SRC halrf_kfree.c:1380]"""
    if one_byte_read(t, efuse.physical_map, PPG_PABIAS_2GA_22C) == 0xFF:
        return
    for address, path, mask in ((PPG_PABIAS_2GA_22C, 0, 0x0000F000),
                                (PPG_PABIAS_2GB_22C, 1, 0x0000F000),
                                (PPG_PABIAS_5GA_22C, 0, 0x000F0000),
                                (PPG_PABIAS_5GB_22C, 1, 0x000F0000)):
        set_rf_reg(t, path, 0x60, mask, one_byte_read(t, efuse.physical_map, address) & 0xF)
    trim.pa_bias_flag |= PA_BIAS_FLAG_ON


def get_tssi_trim_offset(t: RTL8822CUTransport, efuse: EfuseInfo,
                         trim: PowerTrimState) -> None:
    """phydm_get_tssi_trim_offset_8822c: read the signed TSSI de-offset per band and path. The
    2.4 GHz mid entry doubles as the low entry. Reads only. [SRC halrf_kfree.c:1276]"""
    pg_power = [one_byte_read(t, efuse.physical_map, address) for address in TSSI_TRIM_22C]
    if all(value == 0xFF for value in pg_power):
        return
    signed = [value - 0x100 if value >= 0x80 else value for value in pg_power]
    trim.tssi_trim[0] = [signed[0], signed[1]]
    for band in range(8):
        trim.tssi_trim[band + 1] = [signed[band * 2], signed[band * 2 + 1]]
    trim.flag |= TSSI_TRIM_FLAG_ON


def config_new_kfree(t: RTL8822CUTransport, efuse: EfuseInfo, trim: PowerTrimState) -> None:
    """phydm_config_new_kfree -> phydm_do_new_kfree for 8822C.
    [SRC halrf_kfree.c:4338, :4690]"""
    get_set_thermal_trim_offset(t, efuse, trim)
    get_set_power_trim_offset(t, efuse, trim)
    get_set_pa_bias_offset(t, efuse, trim)
    get_tssi_trim_offset(t, efuse, trim)
