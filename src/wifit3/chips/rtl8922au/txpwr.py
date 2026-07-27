"""RTL8922AU tx-power tables (rtw8922a_set_txpwr).

Parses the firmware TXPWR_BYRATE element into the by-rate table, then writes the per-channel
by-rate and rate-offset registers. tx-shape, limit, limit_ru, and the 8922a diff/ref/sar steps
are still TODO. [SRC] rtw8922a.c:2545, phy_be.c:1222-1305.
"""
from . import firmware, mac, phy
from .constants import (
    RTW89_FW_ELEMENT_ID_TXPWR_BYRATE, RTW89_BAND_2G, RTW89_BAND_NUM, RTW89_BYR_BW_NUM,
    RTW89_RS_CCK, RTW89_RS_OFDM, RTW89_RS_MCS, RTW89_RS_HEDCM, RTW89_RS_OFFSET,
    RTW89_RATE_CCK_NUM, RTW89_RATE_OFDM_NUM, RTW89_RATE_HEDCM_NUM, RTW89_RATE_MCS_NUM,
    RTW89_RATE_OFFSET_NUM, RTW89_RATE_OFFSET_NUM_BE, RTW89_NON_OFDMA, RTW89_OFDMA, RTW89_OFDMA_NUM,
    RTW89_NSS_NUM, RTW89_NSS_HEDCM_NUM, RTW89_CHANNEL_WIDTH_20, RTW89_CHANNEL_WIDTH_40,
    RTW89_CHANNEL_WIDTH_320,
    RTW89_RATE_OFFSET_CCK, RTW89_RATE_OFFSET_OFDM, RTW89_RATE_OFFSET_HT, RTW89_RATE_OFFSET_VHT,
    RTW89_RATE_OFFSET_HE, RTW89_RATE_OFFSET_EHT, RTW89_RATE_OFFSET_DLRU_HE,
    RTW89_RATE_OFFSET_DLRU_EHT, TXPWR_FACTOR_RF, TXPWR_FACTOR_MAC,
    R_BE_PWR_BY_RATE, R_BE_PWR_RATE_OFST_CTRL,
    RTW89_FW_ELEMENT_ID_TXPWR_LMT_2GHZ, RTW89_FW_ELEMENT_ID_TXPWR_LMT_RU_2GHZ,
    RTW89_FW_ELEMENT_ID_TX_SHAPE_LMT, RTW89_RS_LMT_NUM, RTW89_RS_TX_SHAPE_NUM, RTW89_BF_NUM,
    RTW89_NTX_NUM, RTW89_REGD_NUM, RTW89_WW, RTW89_2G_CH_NUM, RTW89_2G_BW_NUM, RTW89_RU_NUM,
    RTW89_RU26, RTW89_RU52, RTW89_RU106, RTW89_RU52_26, RTW89_RU106_26, RTW89_RU_SEC_NUM_BE,
    RTW89_TXPWR_LMT_PAGE_SIZE_BE, RTW89_TXPWR_LMT_RU_PAGE_SIZE_BE,
    R_BE_PWR_LMT, R_BE_PWR_RU_LMT, R_BEDGE3, B_BEDGE_CFG, CR_BASE_BE,
    CHIP_CAV, TXPWR_FACTOR_BB, TSSI_K_BASE, TXPWR_DIFF_PATH_OFST, RTW89_SAR_TXPWR_MAC_MAX,
    R_TXAGC_REF_DBM_P0, B_TXAGC_OFDM_REF_DBM_P0, B_TXAGC_CCK_REF_DBM_P0,
    R_TSSI_K_P0, B_TSSI_K_OFDM_P0, R_P0_TXPWRB_BE, R_P1_TXPWRB_BE, B_TXPWRB_MAX_BE,
    R_BE_PWR_REF_CTRL, B_BE_PWR_REF_CTRL_OFDM, B_BE_PWR_REF_CTRL_CCK,
    R_TXAGC_REF_DBM_RF1_P0, B_TXAGC_OFDM_REF_DBM_RF1_P0, B_TXAGC_CCK_REF_DBM_RF1_P0,
    R_TSSI_K_RF1_P0, B_TSSI_K_OFDM_RF1_P0,
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
    """rtw89_mac_txpwr_write32: the txpwr register, shifted +0x4000 for the MAC_1 (PHY_1) band.
    [SRC] mac.h:1559, mac_be.c get_txpwr_cr_be."""
    t.write32(mac._reg_by_idx(reg_base, phy_idx), val & 0xFFFFFFFF)


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


def _regd_get(band: int) -> int:
    """rtw89_regd_get: this card's efuse country code is "00" (worldwide roaming), so every band
    resolves to RTW89_WW. [SRC] regd.c rtw89_regd_init_hint / rtw89_regd_get."""
    return RTW89_WW


def _load_lmt(t, band: int) -> list:
    """rtw89_fw_load_txpwr_lmt_2ghz: parse the TXPWR_LMT_2GHZ element into
    v[bw][ntx][rs][bf][regd][ch], cached. 5/6G tables are not needed for 2.4 GHz hops. [SRC]
    fw.c:11176, core.h:4523."""
    if band != RTW89_BAND_2G:
        raise NotImplementedError("txpwr limit 5/6G table not ported yet")
    if t.lmt_2g is not None:
        return t.lmt_2g
    ent_sz, num, content = firmware.txpwr_conf(RTW89_FW_ELEMENT_ID_TXPWR_LMT_2GHZ, t.rfe_type)
    v = [[[[[[0] * RTW89_2G_CH_NUM for _ in range(RTW89_REGD_NUM)] for _ in range(RTW89_BF_NUM)]
           for _ in range(RTW89_RS_LMT_NUM)] for _ in range(RTW89_NTX_NUM)]
         for _ in range(RTW89_2G_BW_NUM)]
    for i in range(num):
        b = i * ent_sz
        bw, nt, rs, bf, regd, ch_idx = content[b:b + 6]
        if ent_sz > 7 and any(content[b + 7:b + ent_sz]):
            continue
        if (bw >= RTW89_2G_BW_NUM or nt >= RTW89_NTX_NUM or rs >= RTW89_RS_LMT_NUM
                or bf >= RTW89_BF_NUM or regd >= RTW89_REGD_NUM or ch_idx >= RTW89_2G_CH_NUM):
            continue
        v[bw][nt][rs][bf][regd][ch_idx] = _s8(content[b + 6])
    t.lmt_2g = v
    return v


def _read_limit(v: list, bw: int, ntx: int, rs: int, bf: int, ch: int, regd: int) -> int:
    """rtw89_phy_read_txpwr_limit (2G, no ant-gain/SAR): the regd cell, else the WW fallback.
    [SRC] phy.c:2631."""
    ch_idx = ch - 1                          # rtw89_channel_to_idx 2G
    lmt = v[bw][ntx][rs][bf][regd][ch_idx] or v[bw][ntx][rs][bf][RTW89_WW][ch_idx]
    return _rf_to_mac(lmt) & 0xFF


def _fill_limit_20m(v: list, ntx: int, ch: int, regd: int) -> bytearray:
    """phy_fill_limit_20m_be packed into the rtw89_txpwr_limit_be byte layout (cck_20m, cck_40m,
    ofdm, mcs_20m[0], each [bf]); the rest stay 0. [SRC] phy_be.c:1333, phy.h:534."""
    buf = bytearray(RTW89_TXPWR_LMT_PAGE_SIZE_BE)
    for bf in range(RTW89_BF_NUM):
        buf[0 + bf] = _read_limit(v, RTW89_CHANNEL_WIDTH_20, ntx, RTW89_RS_CCK, bf, ch, regd)
        buf[2 + bf] = _read_limit(v, RTW89_CHANNEL_WIDTH_40, ntx, RTW89_RS_CCK, bf, ch, regd)
        buf[4 + bf] = _read_limit(v, RTW89_CHANNEL_WIDTH_20, ntx, RTW89_RS_OFDM, bf, ch, regd)
        buf[6 + bf] = _read_limit(v, RTW89_CHANNEL_WIDTH_20, ntx, RTW89_RS_MCS, bf, ch, regd)
    return buf


def _write_page(t, phy_idx: int, addr: int, buf: bytearray) -> int:
    """Write a filled txpwr page 4 bytes/word, returning the next address. [SRC] phy_be.c:1582."""
    for j in range(0, len(buf), 4):
        _mac_txpwr_write32(t, phy_idx, addr,
                           buf[j] | (buf[j + 1] << 8) | (buf[j + 2] << 16) | (buf[j + 3] << 24))
        addr += 4
    return addr


def _set_txpwr_limit(t, chan: dict, phy_idx: int) -> None:
    """rtw89_phy_set_txpwr_limit_be: fill + write the limit page for ntx 0..1 from R_BE_PWR_LMT.
    [SRC] phy_be.c:1562."""
    if chan["band_width"] != RTW89_CHANNEL_WIDTH_20:
        raise NotImplementedError("txpwr limit >20MHz not needed for monitor hops")
    v = _load_lmt(t, chan["band_type"])
    regd = _regd_get(chan["band_type"])
    addr = R_BE_PWR_LMT
    for ntx in range(RTW89_NTX_NUM):
        addr = _write_page(t, phy_idx, addr, _fill_limit_20m(v, ntx, chan["channel"], regd))


def _load_lmt_ru(t, band: int) -> list:
    """rtw89_fw_load_txpwr_lmt_ru_2ghz: parse TXPWR_LMT_RU_2GHZ into v[ru][ntx][regd][ch], cached.
    [SRC] fw.c, core.h:4545."""
    if band != RTW89_BAND_2G:
        raise NotImplementedError("txpwr limit_ru 5/6G table not ported yet")
    if t.lmt_ru_2g is not None:
        return t.lmt_ru_2g
    ent_sz, num, content = firmware.txpwr_conf(RTW89_FW_ELEMENT_ID_TXPWR_LMT_RU_2GHZ, t.rfe_type)
    v = [[[[0] * RTW89_2G_CH_NUM for _ in range(RTW89_REGD_NUM)] for _ in range(RTW89_NTX_NUM)]
         for _ in range(RTW89_RU_NUM)]
    for i in range(num):
        b = i * ent_sz
        ru, nt, regd, ch_idx = content[b:b + 4]
        if ent_sz > 5 and any(content[b + 5:b + ent_sz]):
            continue
        if (ru >= RTW89_RU_NUM or nt >= RTW89_NTX_NUM or regd >= RTW89_REGD_NUM
                or ch_idx >= RTW89_2G_CH_NUM):
            continue
        v[ru][nt][regd][ch_idx] = _s8(content[b + 4])
    t.lmt_ru_2g = v
    return v


def _read_limit_ru(v: list, ru: int, ntx: int, ch: int, regd: int) -> int:
    """rtw89_phy_read_txpwr_limit_ru (2G, no ant-gain/SAR): the regd cell, else the WW fallback.
    [SRC] phy.c read_txpwr_limit_ru."""
    ch_idx = ch - 1
    lmt = v[ru][ntx][regd][ch_idx] or v[ru][ntx][RTW89_WW][ch_idx]
    return _rf_to_mac(lmt) & 0xFF


def _fill_limit_ru_20m(v: list, ntx: int, ch: int, regd: int) -> bytearray:
    """phy_fill_limit_ru_20m_be: index 0 of each 16-wide RU section (ru26/52/106/52_26/106_26).
    [SRC] phy_be.c:1611, phy.h:563."""
    buf = bytearray(RTW89_TXPWR_LMT_RU_PAGE_SIZE_BE)
    for n, ru in enumerate((RTW89_RU26, RTW89_RU52, RTW89_RU106, RTW89_RU52_26, RTW89_RU106_26)):
        buf[n * RTW89_RU_SEC_NUM_BE] = _read_limit_ru(v, ru, ntx, ch, regd)
    return buf


def _set_txpwr_limit_ru(t, chan: dict, phy_idx: int) -> None:
    """rtw89_phy_set_txpwr_limit_ru_be: fill + write the limit-RU page for ntx 0..1 from
    R_BE_PWR_RU_LMT (8922A takes no large-MRU tail). [SRC] phy_be.c:1857."""
    if chan["band_width"] != RTW89_CHANNEL_WIDTH_20:
        raise NotImplementedError("txpwr limit_ru >20MHz not needed for monitor hops")
    v = _load_lmt_ru(t, chan["band_type"])
    regd = _regd_get(chan["band_type"])
    addr = R_BE_PWR_RU_LMT
    for ntx in range(RTW89_NTX_NUM):
        addr = _write_page(t, phy_idx, addr, _fill_limit_ru_20m(v, ntx, chan["channel"], regd))


def _load_tx_shape(t) -> list:
    """rtw89_fw_load_tx_shape_lmt: parse TX_SHAPE_LMT into v[band][tx_shape_rs][regd], cached.
    [SRC] fw.c, core.h:4564."""
    if t.tx_shape_lmt is not None:
        return t.tx_shape_lmt
    ent_sz, num, content = firmware.txpwr_conf(RTW89_FW_ELEMENT_ID_TX_SHAPE_LMT, t.rfe_type)
    v = [[[0] * RTW89_REGD_NUM for _ in range(RTW89_RS_TX_SHAPE_NUM)] for _ in range(RTW89_BAND_NUM)]
    for i in range(num):
        b = i * ent_sz
        band, rs, regd = content[b:b + 3]
        if ent_sz > 4 and any(content[b + 4:b + ent_sz]):
            continue
        if band >= RTW89_BAND_NUM or rs >= RTW89_RS_TX_SHAPE_NUM or regd >= RTW89_REGD_NUM:
            continue
        v[band][rs][regd] = content[b + 3]
    t.tx_shape_lmt = v
    return v


def _set_tx_shape(t, chan: dict, phy_idx: int) -> None:
    """rtw8922a_set_tx_shape: tx_shape_lmt[band][OFDM][regd] selects bb_tx_triangular on/off
    (R_BEDGE3 B_BEDGE_CFG, PHY_1 shifted). [SRC] rtw8922a.c:2501, 2493."""
    band = chan["band_type"]
    idx = _load_tx_shape(t)[band][RTW89_RS_OFDM][_regd_get(band)]
    phy._phy_write32_idx(t, R_BEDGE3, B_BEDGE_CFG, 0 if idx == 0 else 1, phy_idx)


# rtw8922a_txpwr_ref[phy_idx]: (ofdm ref, cck ref, tssi-k) reg/mask triples; RF1 set for PHY_1.
# [SRC] rtw8922a.c:2443.
_TXPWR_REF = (
    ((R_TXAGC_REF_DBM_P0, B_TXAGC_OFDM_REF_DBM_P0),
     (R_TXAGC_REF_DBM_P0, B_TXAGC_CCK_REF_DBM_P0),
     (R_TSSI_K_P0, B_TSSI_K_OFDM_P0)),
    ((R_TXAGC_REF_DBM_RF1_P0, B_TXAGC_OFDM_REF_DBM_RF1_P0),
     (R_TXAGC_REF_DBM_RF1_P0, B_TXAGC_CCK_REF_DBM_RF1_P0),
     (R_TSSI_K_RF1_P0, B_TSSI_K_OFDM_RF1_P0)),
)


def _set_txpwr_diff(t, chan: dict, phy_idx: int) -> None:
    """rtw8922a_set_txpwr_diff: per-path OFDM/CCK reference (ofst_dec) and TSSI-K. Without antenna
    gain (WW) pwr_ofst is 0, and this CBV cut keeps pwr_ref 0, so ofst_dec is 0 and tssi_k the base.
    [SRC] rtw8922a.c:2454."""
    ref = _TXPWR_REF[phy_idx]
    pwr_ofst = 0                                   # ant_gain_pwr_offset: 0 without antenna gain
    tssi_k_ofst = abs(pwr_ofst) + TSSI_K_BASE
    pwr_ref = (16 if t.cv == CHIP_CAV else 0) << TXPWR_FACTOR_RF
    pwr_ref_ofst = pwr_ref - (abs(pwr_ofst) >> (TXPWR_FACTOR_BB - TXPWR_FACTOR_RF))
    ofst_dec = (pwr_ref if pwr_ofst > 0 else pwr_ref_ofst,
                pwr_ref_ofst if pwr_ofst > 0 else pwr_ref)
    tssi_k = (TSSI_K_BASE if pwr_ofst > 0 else tssi_k_ofst,
              tssi_k_ofst if pwr_ofst > 0 else TSSI_K_BASE)
    for i in range(RTW89_NTX_NUM):
        po = TXPWR_DIFF_PATH_OFST[i]
        t.write32_mask(ref[0][0] + po + CR_BASE_BE, ref[0][1], ofst_dec[i] & 0xFFFFFFFF)
        t.write32_mask(ref[1][0] + po + CR_BASE_BE, ref[1][1], ofst_dec[i] & 0xFFFFFFFF)
        t.write32_mask(ref[2][0] + po + CR_BASE_BE, ref[2][1], tssi_k[i] & 0xFFFFFFFF)


def _set_txpwr_ref(t, phy_idx: int) -> None:
    """rtw8922a_set_txpwr_ref: zero the OFDM and CCK power references (PHY_1 shifted +0x4000).
    [SRC] rtw8922a.c:2429."""
    reg = mac._reg_by_idx(R_BE_PWR_REF_CTRL, phy_idx)
    t.write32_mask(reg, B_BE_PWR_REF_CTRL_OFDM, 0)
    t.write32_mask(reg, B_BE_PWR_REF_CTRL_CCK, 0)


def _set_txpwr_sar_diff(t, chan: dict, phy_idx: int) -> None:
    """rtw8922a_set_txpwr_sar_diff: the SAR max per path (PHY_0 only). No SAR source, so the query
    returns the MAC max, converted to RF units. [SRC] rtw8922a.c:2520."""
    if phy_idx != 0:
        return
    sar_rf = RTW89_SAR_TXPWR_MAC_MAX << (TXPWR_FACTOR_RF - TXPWR_FACTOR_MAC)
    t.write32_mask(R_P0_TXPWRB_BE + CR_BASE_BE, B_TXPWRB_MAX_BE, sar_rf)
    t.write32_mask(R_P1_TXPWRB_BE + CR_BASE_BE, B_TXPWRB_MAX_BE, sar_rf)


def set_txpwr(t, chan: dict, phy_idx: int = 0) -> None:
    """rtw8922a_set_txpwr: byrate, offset, tx_shape, limit, limit_ru, then the 8922a per-path
    diff/ref/sar steps. [SRC] rtw8922a.c:2545."""
    _set_txpwr_byrate(t, chan, phy_idx)
    _set_txpwr_offset(t, chan, phy_idx)
    _set_tx_shape(t, chan, phy_idx)
    _set_txpwr_limit(t, chan, phy_idx)
    _set_txpwr_limit_ru(t, chan, phy_idx)
    _set_txpwr_diff(t, chan, phy_idx)
    _set_txpwr_ref(t, phy_idx)
    _set_txpwr_sar_diff(t, chan, phy_idx)
