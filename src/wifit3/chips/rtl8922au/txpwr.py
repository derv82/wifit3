"""RTL8922AU tx-power tables (rtw8922a_set_txpwr).

Parses the firmware TXPWR_BYRATE element into the by-rate table, then writes the per-channel
by-rate and rate-offset registers. tx-shape, limit, limit_ru, and the 8922a diff/ref/sar steps
are still TODO. [SRC] rtw8922a.c:2545, phy_be.c:1222-1305.
"""
from . import firmware
from .constants import (
    RTW89_FW_ELEMENT_ID_TXPWR_BYRATE, RTW89_BAND_2G, RTW89_BAND_NUM, RTW89_BYR_BW_NUM,
    RTW89_RS_CCK, RTW89_RS_OFDM, RTW89_RS_MCS, RTW89_RS_HEDCM, RTW89_RS_OFFSET,
    RTW89_RATE_CCK_NUM, RTW89_RATE_OFDM_NUM, RTW89_RATE_HEDCM_NUM, RTW89_RATE_MCS_NUM,
    RTW89_RATE_OFFSET_NUM, RTW89_RATE_OFFSET_NUM_BE, RTW89_NON_OFDMA, RTW89_OFDMA, RTW89_OFDMA_NUM,
    RTW89_NSS_NUM, RTW89_NSS_HEDCM_NUM, RTW89_CHANNEL_WIDTH_40, RTW89_CHANNEL_WIDTH_320,
    RTW89_RATE_OFFSET_CCK, RTW89_RATE_OFFSET_OFDM, RTW89_RATE_OFFSET_HT, RTW89_RATE_OFFSET_VHT,
    RTW89_RATE_OFFSET_HE, RTW89_RATE_OFFSET_EHT, RTW89_RATE_OFFSET_DLRU_HE,
    RTW89_RATE_OFFSET_DLRU_EHT, TXPWR_FACTOR_RF, TXPWR_FACTOR_MAC,
    R_BE_PWR_BY_RATE, R_BE_PWR_RATE_OFST_CTRL,
)


def _s8(v: int) -> int:
    return v - 256 if v & 0x80 else v


def _new_byr() -> dict:
    """One rtw89_txpwr_byrate: per-rate-section arrays. [SRC] core.h:990."""
    return {
        "cck": [0] * RTW89_RATE_CCK_NUM,
        "ofdm": [0] * RTW89_RATE_OFDM_NUM,
        "mcs": [[[0] * RTW89_RATE_MCS_NUM for _ in range(RTW89_NSS_NUM)]
                for _ in range(RTW89_OFDMA_NUM)],
        "hedcm": [[[0] * RTW89_RATE_HEDCM_NUM for _ in range(RTW89_NSS_HEDCM_NUM)]
                  for _ in range(RTW89_OFDMA_NUM)],
        "offset": [0] * RTW89_RATE_OFFSET_NUM,
    }


def _byr_seek(byr: dict, rs: int, idx: int, nss: int, ofdma: int) -> list:
    """rtw89_phy_raw_byr_seek: the array a rate lands in. [SRC] phy.c raw_byr_seek."""
    if rs == RTW89_RS_CCK:
        return byr["cck"]
    if rs == RTW89_RS_OFDM:
        return byr["ofdm"]
    if rs == RTW89_RS_MCS:
        return byr["mcs"][ofdma][nss]
    if rs == RTW89_RS_HEDCM:
        return byr["hedcm"][ofdma][nss]
    return byr["offset"]                 # RTW89_RS_OFFSET


def _byrate_entry_valid(band: int, bw: int, rs: int, nss: int, ofdma: int, shf: int, blen: int,
                        content: bytes, base: int, ent_sz: int) -> bool:
    """fw_txpwr_byrate_entry_valid: bounds-check a byrate entry (and zero-extension if ent_sz grew).
    [SRC] fw.c:11079."""
    if ent_sz > 11 and any(content[base + 11:base + ent_sz]):
        return False
    if band >= RTW89_BAND_NUM or bw >= RTW89_BYR_BW_NUM:
        return False
    if rs == RTW89_RS_CCK:
        return shf + blen <= RTW89_RATE_CCK_NUM
    if rs == RTW89_RS_OFDM:
        return shf + blen <= RTW89_RATE_OFDM_NUM
    if rs == RTW89_RS_MCS:
        return shf + blen <= RTW89_RATE_MCS_NUM and nss < RTW89_NSS_NUM and ofdma < RTW89_OFDMA_NUM
    if rs == RTW89_RS_HEDCM:
        return (shf + blen <= RTW89_RATE_HEDCM_NUM and nss < RTW89_NSS_HEDCM_NUM
                and ofdma < RTW89_OFDMA_NUM)
    if rs == RTW89_RS_OFFSET:
        return shf + blen <= RTW89_RATE_OFFSET_NUM
    return False


def _load_byr(t) -> dict:
    """rtw89_fw_load_txpwr_byrate: parse the TXPWR_BYRATE fw element into byr[band][bw], cached on
    the transport. [SRC] fw.c:11122."""
    if t.byr is not None:
        return t.byr
    ent_sz, num_ents, content = firmware.txpwr_conf(RTW89_FW_ELEMENT_ID_TXPWR_BYRATE, t.rfe_type)
    byr = {b: {bw: _new_byr() for bw in range(RTW89_BYR_BW_NUM)} for b in range(RTW89_BAND_NUM)}
    for i in range(num_ents):
        base = i * ent_sz
        band, nss, rs, shf, blen = content[base:base + 5]
        dword = int.from_bytes(content[base + 5:base + 9], "little")
        bw = content[base + 9]
        ofdma = content[base + 10]
        if not _byrate_entry_valid(band, bw, rs, nss, ofdma, shf, blen, content, base, ent_sz):
            continue
        arr = _byr_seek(byr[band][bw], rs, shf, nss, ofdma)
        for k in range(blen):
            arr[shf + k] = (dword >> (8 * k)) & 0xFF
    t.byr = byr
    return byr


def _rf_to_mac(v: int) -> int:
    """rtw89_phy_txpwr_rf_to_mac: RF units to MAC units (>> factor delta). [SRC] phy.h:1124."""
    return _s8(v) >> (TXPWR_FACTOR_RF - TXPWR_FACTOR_MAC)


def _read_byrate(byr: dict, band: int, bw: int, rs: int, idx: int, nss: int, ofdma: int) -> int:
    """rtw89_phy_read_txpwr_byrate: CCK always reads the 2G table. [SRC] phy.c:2556."""
    b = RTW89_BAND_2G if rs == RTW89_RS_CCK else band
    return _rf_to_mac(_byr_seek(byr[b][bw], rs, idx, nss, ofdma)[idx])


def _mac_txpwr_write32(t, phy_idx: int, reg_base: int, val: int) -> None:
    """rtw89_mac_txpwr_write32 for PHY_0: reg maps to itself (no CMAC-1 shift). [SRC] mac.h:1559."""
    if phy_idx != 0:
        raise NotImplementedError("txpwr write on PHY_1 not needed for monitor hops")
    t.write32(reg_base, val & 0xFFFFFFFF)


# rtw89_byr_spec_be: (rs, init_idx, ofdma, num_of_idx, no_over_bw40, no_multi_nss). [SRC] phy_be.c:1181.
_BYR_SPEC = (
    (RTW89_RS_CCK, 0, RTW89_NON_OFDMA, RTW89_RATE_CCK_NUM, True, True),
    (RTW89_RS_OFDM, 0, RTW89_NON_OFDMA, RTW89_RATE_OFDM_NUM, False, True),
    (RTW89_RS_MCS, 14, RTW89_NON_OFDMA, 2, False, True),
    (RTW89_RS_MCS, 14, RTW89_OFDMA, 2, False, True),
    (RTW89_RS_MCS, 0, RTW89_NON_OFDMA, 14, False, False),
    (RTW89_RS_HEDCM, 0, RTW89_NON_OFDMA, RTW89_RATE_HEDCM_NUM, False, False),
    (RTW89_RS_MCS, 0, RTW89_OFDMA, 14, False, False),
    (RTW89_RS_HEDCM, 0, RTW89_OFDMA, RTW89_RATE_HEDCM_NUM, False, False),
)


def _set_txpwr_byrate(t, chan: dict, phy_idx: int) -> None:
    """rtw89_phy_set_txpwr_byrate_be: pack 4 rates/word into R_BE_PWR_BY_RATE, for every bw 0..320
    and nss 0..2. [SRC] phy_be.c:1222, 1260."""
    byr = _load_byr(t)
    band = chan["band_type"]
    addr = R_BE_PWR_BY_RATE
    for bw in range(RTW89_CHANNEL_WIDTH_320 + 1):
        for nss in range(2):                 # RTW89_NSS_1..RTW89_NSS_2
            v = [0, 0, 0, 0]
            pos = 0
            for rs, i0, ofdma, num, no40, nomulti in _BYR_SPEC:
                if bw > RTW89_CHANNEL_WIDTH_40 and no40:
                    continue
                if nss > 0 and nomulti:
                    continue
                idx = i0
                for _ in range(num):
                    v[pos] = _read_byrate(byr, band, bw, rs, idx, nss, ofdma) & 0xFF
                    pos = (pos + 1) % 4
                    if pos == 0:
                        _mac_txpwr_write32(t, phy_idx, addr,
                                           v[0] | (v[1] << 8) | (v[2] << 16) | (v[3] << 24))
                        addr += 4
                    idx += 1


def _set_txpwr_offset(t, chan: dict, phy_idx: int) -> None:
    """rtw89_phy_set_txpwr_offset_be: pack the 8 per-mode rate offsets into
    R_BE_PWR_RATE_OFST_CTRL (4 bits each). [SRC] phy_be.c:1277."""
    byr = _load_byr(t)
    band = chan["band_type"]
    v = [_read_byrate(byr, band, 0, RTW89_RS_OFFSET, i, 0, RTW89_NON_OFDMA)
         for i in range(RTW89_RATE_OFFSET_NUM_BE)]
    val = ((v[RTW89_RATE_OFFSET_CCK] & 0xF)
           | ((v[RTW89_RATE_OFFSET_OFDM] & 0xF) << 4)
           | ((v[RTW89_RATE_OFFSET_HT] & 0xF) << 8)
           | ((v[RTW89_RATE_OFFSET_VHT] & 0xF) << 12)
           | ((v[RTW89_RATE_OFFSET_HE] & 0xF) << 16)
           | ((v[RTW89_RATE_OFFSET_EHT] & 0xF) << 20)
           | ((v[RTW89_RATE_OFFSET_DLRU_HE] & 0xF) << 24)
           | ((v[RTW89_RATE_OFFSET_DLRU_EHT] & 0xF) << 28))
    _mac_txpwr_write32(t, phy_idx, R_BE_PWR_RATE_OFST_CTRL, val)


def set_txpwr(t, chan: dict, phy_idx: int = 0) -> None:
    """rtw8922a_set_txpwr: byrate + offset ported so far; tx_shape, limit, limit_ru, and the 8922a
    diff/ref/sar steps are still TODO. [SRC] rtw8922a.c:2545."""
    _set_txpwr_byrate(t, chan, phy_idx)
    _set_txpwr_offset(t, chan, phy_idx)
