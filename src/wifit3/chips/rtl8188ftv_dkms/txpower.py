"""RTL8188FTV DKMS TX power index (M5g).

Ported from ``PHY_SetTxPowerLevel8188F`` /
``PHY_SetTxPowerLevelByPath`` / ``PHY_SetTxPowerIndexByRateSection`` /
``PHY_SetTxPowerIndex_8188F`` / ``PHY_GetTxPowerIndex_8188F`` /
``PHY_GetTxPowerIndexBase`` (hal/rtl8188f/rtl8188f_phycfg.c:909-930,
hal/hal_com_phycfg.c:1114-,1231-,2240-,2288-) plus the by-rate load chain
``PHY_StoreTxPowerByRateNew`` / ``phy_ConvertTxPowerByRateInDbmToRelativeValues``
/ ``_PHY_GetTxPowerByRate`` / ``PHY_GetTxPowerIndex`` PG walk
(hal_com_phycfg.c:769-,1000-,1540-,1577-).
``RegEnableTxPowerLimit`` is 0 in this build (Makefile
CONFIG_CALIBRATE_TX_POWER_TO_MAX=y → os_intfs.c), so
``PHY_GetTxPowerLimit`` (hal_com_phycfg.c) returns MAX_POWER_INDEX before
its table lookup and the ``min(byRate, limit)`` clamp in
``PHY_GetTxPowerIndex_8188F`` is a no-op; the limit call's
``CurrentChannel`` quirk is likewise moot because
``PHY_HandleSwChnlAndSetBW8188F`` stores ``CurrentChannel = ChannelNum``
before ``phy_SwChnlAndSetBwMode8188F`` runs the level. u8/s8 wrap matches
the C.
"""
from __future__ import annotations

from . import bb

MGN_1M, MGN_2M, MGN_5_5M, MGN_11M = 0x02, 0x04, 0x0B, 0x16
MGN_6M, MGN_9M, MGN_12M, MGN_18M = 0x0C, 0x12, 0x18, 0x24
MGN_24M, MGN_36M, MGN_48M, MGN_54M = 0x30, 0x48, 0x60, 0x6C
MGN_MCS0, MGN_MCS1, MGN_MCS2, MGN_MCS3 = 0x80, 0x81, 0x82, 0x83
MGN_MCS4, MGN_MCS5, MGN_MCS6, MGN_MCS7 = 0x84, 0x85, 0x86, 0x87

CCK_RATES = (MGN_1M, MGN_2M, MGN_5_5M, MGN_11M)
OFDM_RATES = (MGN_6M, MGN_9M, MGN_12M, MGN_18M, MGN_24M, MGN_36M, MGN_48M, MGN_54M)
MCS07_RATES = tuple(MGN_MCS0 + i for i in range(8))

BAND_2G = 0
SEC_CCK, SEC_OFDM, SEC_MCS07 = 0, 1, 2
MAX_POWER_INDEX = 0x3F

TXAGC_CCK1 = 0xE08
TXAGC_CCK211 = 0x86C
TXAGC_RATE18_06 = 0xE00
TXAGC_RATE54_24 = 0xE04
TXAGC_MCS03_00 = 0xE10
TXAGC_MCS07_04 = 0xE14

RATE_POSITIONS = {
    MGN_1M: (TXAGC_CCK1, 0xFF00),
    MGN_2M: (TXAGC_CCK211, 0xFF00),
    MGN_5_5M: (TXAGC_CCK211, 0xFF0000),
    MGN_11M: (TXAGC_CCK211, 0xFF000000),
    MGN_6M: (TXAGC_RATE18_06, 0xFF),
    MGN_9M: (TXAGC_RATE18_06, 0xFF00),
    MGN_12M: (TXAGC_RATE18_06, 0xFF0000),
    MGN_18M: (TXAGC_RATE18_06, 0xFF000000),
    MGN_24M: (TXAGC_RATE54_24, 0xFF),
    MGN_36M: (TXAGC_RATE54_24, 0xFF00),
    MGN_48M: (TXAGC_RATE54_24, 0xFF0000),
    MGN_54M: (TXAGC_RATE54_24, 0xFF000000),
    MGN_MCS0: (TXAGC_MCS03_00, 0xFF),
    MGN_MCS1: (TXAGC_MCS03_00, 0xFF00),
    MGN_MCS2: (TXAGC_MCS03_00, 0xFF0000),
    MGN_MCS3: (TXAGC_MCS03_00, 0xFF000000),
    MGN_MCS4: (TXAGC_MCS07_04, 0xFF),
    MGN_MCS5: (TXAGC_MCS07_04, 0xFF00),
    MGN_MCS6: (TXAGC_MCS07_04, 0xFF0000),
    MGN_MCS7: (TXAGC_MCS07_04, 0xFF000000),
}


def _s8(value: int) -> int:
    value &= 0xFF
    return value - 256 if value > 127 else value


def rate_values(addr: int, mask: int, data: int) -> list[tuple[int, int]]:
    def bcd(byte: int) -> int:
        return ((byte >> 4) & 0xF) * 10 + (byte & 0xF)

    out: list[tuple[int, int]] = []

    def quad(rates) -> None:
        for i, rate in enumerate(rates):
            out.append((rate, bcd((data >> (i * 8)) & 0xFF)))

    if addr in (0xE00,):
        quad((MGN_6M, MGN_9M, MGN_12M, MGN_18M))
    elif addr in (0xE04,):
        quad((MGN_24M, MGN_36M, MGN_48M, MGN_54M))
    elif addr == 0xE08:
        out.append((MGN_1M, bcd((data >> 8) & 0xFF)))
    elif addr == 0x86C:
        if mask == 0xFFFFFF00:
            for i in range(1, 4):
                out.append(([MGN_2M, MGN_5_5M, MGN_11M][i - 1],
                            bcd((data >> (i * 8)) & 0xFF)))
        elif mask == 0x000000FF:
            out.append((MGN_11M, bcd(data & 0xFF)))
    elif addr == 0xE10:
        quad((MGN_MCS0, MGN_MCS0 + 1, MGN_MCS0 + 2, MGN_MCS0 + 3))
    elif addr == 0xE14:
        quad((MGN_MCS0 + 4, MGN_MCS0 + 5, MGN_MCS0 + 6, MGN_MCS0 + 7))
    return out


def _txnum(rate: int) -> int | None:
    if _is_1t(rate):
        return 0
    if MGN_MCS0 + 8 <= rate <= MGN_MCS0 + 15 or 0xAA <= rate <= 0xB3:
        return 1
    if MGN_MCS0 + 16 <= rate <= MGN_MCS0 + 23 or 0xB4 <= rate <= 0xBD:
        return 2
    if MGN_MCS0 + 24 <= rate <= MGN_MCS0 + 31 or 0xBE <= rate <= 0xC7:
        return 3
    return None


def _is_1t(rate: int) -> bool:
    return (rate in CCK_RATES or MGN_6M <= rate <= MGN_54M and rate != MGN_11M
            or MGN_MCS0 <= rate <= MGN_MCS0 + 7
            or 0xA0 <= rate <= 0xA9)


class ByRateTables:
    def __init__(self):
        self.tbl: dict = {}

    def store(self, band: int, path: int, txnum: int, addr: int,
              mask: int, data: int) -> None:
        if band not in (0, 1) or path > 3 or txnum > 3:
            return
        for rate, dbm in rate_values(addr, mask, data):
            target = _txnum(rate)
            if target is not None:
                self.tbl[(band, path, target, rate)] = dbm

    def convert(self) -> None:
        for band in (0, 1):
            for path in range(4):
                for txnum in range(4):
                    for rates, anchor in ((CCK_RATES, MGN_11M),
                                          (OFDM_RATES, MGN_54M),
                                          (MCS07_RATES, MGN_MCS0 + 7)):
                        base = self.tbl.get((band, path, txnum, anchor), 0)
                        for rate in rates:
                            key = (band, path, txnum, rate)
                            self.tbl[key] = (self.tbl.get(key, 0) - base) & 0xFF

    def get(self, band: int, path: int, txnum: int, rate: int) -> int:
        return _s8(self.tbl.get((band, path, txnum, rate), 0))


def load_pg_tables(groups: list[tuple]) -> ByRateTables:
    tables = ByRateTables()
    for band, path, txnum, addr, mask, data in groups:
        if addr in (0xFE, 0xFFE):
            continue
        tables.store(band, path, txnum, addr, mask, data)
    tables.convert()
    return tables


def load_default_pg_tables() -> ByRateTables:
    from . import bb_phy_reg_pg_tbl
    words = bb_phy_reg_pg_tbl.TABLE
    return load_pg_tables([tuple(words[i:i + 6]) for i in range(0, len(words), 6)])


def index_base(params, path: int, rate: int, bw20: bool, channel: int) -> int:
    if not 1 <= channel <= 14:
        raise ValueError(f"5 GHz unreachable on this silicon: ch{channel}")
    idx = channel - 1
    if rate in CCK_RATES:
        power = params.cck_base_ch[path][idx]
    else:
        power = params.bw40_base_ch[path][idx]
    if MGN_6M <= rate <= MGN_54M and rate not in CCK_RATES:
        power = (power + params.txpower.ofdm_diff[path][0]) & 0xFF
    if bw20 and MGN_MCS0 <= rate <= MGN_MCS0 + 7:
        power = (power + params.txpower.bw20_diff[path][0]) & 0xFF
    return power


def get_index(params, tables: ByRateTables, path: int, rate: int,
              channel: int, reg_pwr_tbl_sel: int = 0,
              rem_cck: int = 0, rem_ofdm: int = 0) -> int:
    power = _s8(index_base(params, path, rate, True, channel))
    by_rate = tables.get(BAND_2G, path, 0, rate)
    if reg_pwr_tbl_sel != 0:
        # TODO: verify, untested here, needs RegPwrTblSel != 0
        raise ValueError("TX power limit path untested here")
    power += by_rate
    power += rem_cck if rate in CCK_RATES else rem_ofdm
    if power > MAX_POWER_INDEX:
        power = MAX_POWER_INDEX
    return power & 0xFF


def set_index(t, power: int, rate: int) -> None:
    addr, mask = RATE_POSITIONS[rate]
    bb.set_bb_reg(t, addr, mask, power)


def set_section(t, channel: int, path: int, params, tables: ByRateTables,
                rates, rem_cck: int = 0, rem_ofdm: int = 0) -> None:
    for rate in rates:
        set_index(t, get_index(params, tables, path, rate, channel,
                               rem_cck=rem_cck, rem_ofdm=rem_ofdm), rate)


def set_level(t, channel: int, path: int, params, tables: ByRateTables,
              rem_cck: int = 0, rem_ofdm: int = 0) -> None:
    set_section(t, channel, path, params, tables, CCK_RATES, rem_cck,
                rem_ofdm)
    set_section(t, channel, path, params, tables, OFDM_RATES, rem_cck,
                rem_ofdm)
    set_section(t, channel, path, params, tables, MCS07_RATES, rem_cck,
                rem_ofdm)
