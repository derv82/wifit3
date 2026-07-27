"""RTL8922AU BB register init, ported from rtw89-7.2 phy.c / phy_be.c.

rtw89_phy_init_bb_reg applies the firmware's BB register table for PHY_0 and (DBCC) PHY_1. The
table is condition-coded: a headline block picks the (rfe_type, cv) branch, then if/elif/else
markers select which register writes apply under it. [SRC] phy.c:1940-1966.
"""
import struct
import time

from .constants import (
    CHIP_CAV, RTW89_BAND_2G,
    R_BK_FC0INV, B_BK_FC0INV, R_CCK_FC0INV, B_CCK_FC0INV, RTW89_FW_ELEMENT_ID_BB_GAIN,
    GAIN_OFFSET_2G_CCK, GAIN_OFFSET_2G_OFDM, R_MGAIN_BIAS, B_MGAIN_BIAS_BW20, B_MGAIN_BIAS_BW40,
    R_CCK_RPL_OFST, B_CCK_RPL_OFST,
    R_FC0, B_FC0, B_FC0_INV, R_PCOEFF01, B_PCOEFF, R_MAC_PIN_SEL, B_CH_IDX_SEG0,
    RTW89_CH_BASE_IDX_2G, RTW89_CH_BASE_IDX_MASK, RTW89_CH_OFFSET_MASK,
    RTW89_FW_ELEMENT_ID_BB_REG, CR_BASE_BE, BYPASS_CR_DATA,
    PHY_HEADLINE_VALID, PHY_COND_BRANCH_IF, PHY_COND_BRANCH_ELIF, PHY_COND_BRANCH_ELSE,
    PHY_COND_BRANCH_END, PHY_COND_CHECK, PHY_COND_DONT_CARE,
    R_BE_FEN_RST_ENABLE, B_BE_FEN_BBPLAT_RSTB, B_BE_FEN_BB1PLAT_RSTB,
    B_BE_BOOT_RDY0, B_BE_BOOT_RDY1, R_BBCLK, B_CLK_640M, R_TXSCALE, B_TXFCTR_EN,
    R_TXFCTR, B_TXFCTR_THD, R_SLOPE, B_EHT_RATE_TH, B_SLOPE_A, B_SLOPE_B,
    R_BEDGE, B_HE_RATE_TH, B_EHT_MCS14, R_BEDGE2, B_HT_VHT_TH, B_EHT_MCS15,
    R_BEDGE3, B_EHTTB_EN, B_HEERSU_EN, B_HEMU_EN, B_TB_EN, R_SU_PUNC, B_SU_PUNC_EN,
    R_BEDGE5, B_HWGEN_EN, B_PWROFST_COMP, R_MAG_AB, B_BY_SLOPE, B_MAG_AB,
    R_MAG_A, B_MGA_AEND, R_SC_CORNER, B_SC_CORNER, R_UDP_COEEF, B_UDP_COEEF,
    RTW89_FW_ELEMENT_ID_RADIO_A, RTW89_FW_ELEMENT_ID_RADIO_B, RF_PATH_A, RF_PATH_B,
    RTW89_RF_ADDR_ADSEL_MASK, RFREG_MASK, RF_BASE_ADDR, HWSI_IDLE_ADDR, HWSI_OFST_ADDR,
    B_HWSI_BUSY, B_HWSI_DATA_ADDR, B_HWSI_DATA_VAL,
    H2C_CAT_OUTSRC, H2C_CL_OUTSRC_RF_REG_A, H2C_CL_OUTSRC_RF_REG_B,
    RTW89_H2C_RF_PAGE_SIZE, RTW89_H2C_RF_PAGE_NUM,
    MASKDWORD, MAC_BAND1_OFFSET,
    R_EN_SND_WO_NDP, R_EN_SND_WO_NDP_C1, B_EN_SND_WO_NDP, R_BE_PWR_BOOST, B_BE_PWR_CTRL_SEL,
    R_DBCC, B_DBCC_EN, R_DBCC_FA, B_DBCC_FA, R_AFEDAC0, B_AFEDAC0, R_AFEDAC1, B_AFEDAC1,
    R_EMLSR, B_EMLSR_PARM,
    R_CCX, B_CCX_EN_MSK, B_CCX_TRIG_OPT_MSK, B_MEASUREMENT_TRIG_MSK, B_CCX_EDCCA_OPT_MSK_V1,
    R_IFS_COUNTER, B_IFS_COLLECT_EN, R_IFS_T, B_IFS_T_TH_LOW, B_IFS_T_TH_HIGH, B_IFS_T_EN,
    IFS_CLM_TH_LOW, IFS_CLM_TH_HIGH,
    R_PLCP_HISTOGRAM, B_STS_DIS_TRIG_BY_FAIL, B_STS_DIS_TRIG_BY_BRK,
    R_PHY_STS_BITMAP_START, R_PHY_STS_BITMAP_EHT, RTW89_PHYSTS_BITMAP_NUM, RTW89_RSVD_9,
    RTW89_HE_MU, RTW89_VHT_MU, RTW89_TRIG_BASE_PPDU, RTW89_CCK_PKT, RTW89_HT_PKT, RTW89_EHT_PKT,
    IE01_CMN_OFDM, IE04_07_EXT_PATH, IE13_DL_MU_DEF, IE20_DBG_OFDM,
    R_SEG0R_PD_V2, B_SEG0R_PD_LOWER_BOUND, B_SEG0R_PD_SR_EN,
    R_BMODE_PDTH_EN_V2, B_BMODE_PDTH_LIMIT_EN, R_BMODE_PDTH_V2, B_BMODE_PDTH_LOWER_BOUND,
    XTAL_SI_XTAL_SC_XO, XTAL_SI_XTAL_SC_XI, B_AX_XTAL_SC_MASK, XTAL_SC_MASK,
    R_DCFO_OPT_BE, B_DCFO_OPT_EN_BE, R_DCFO_WEIGHT_BE, B_DCFO_WEIGHT_MSK_BE,
    R_TX_COLLISION_T2R_ST_BE, B_TX_COLLISION_T2R_ST_BE_M,
    R_CHINFO_SEG, B_CHINFO_SEG_LEN, B_CHINFO_SEG, R_CHINFO_DATA, B_CHINFO_DATA_BITMAP,
    R_CHINFO_ELM_SRC, B_CHINFO_ELM_BITMAP, B_CHINFO_SRC, R_CHINFO_TYPE_SCAL, B_CHINFO_TYPE,
    B_CHINFO_SCAL,
    R_BE_PWR_MACID_PATH_BASE, R_BE_PWR_MACID_LMT_BASE, R_BE_PWR_BY_RATE, R_BE_PWR_BY_RATE_END,
    R_BE_PWR_RULMT_START, R_BE_PWR_RULMT_END, R_BE_PWR_RATE_OFST_CTRL, R_BE_PWR_RATE_OFST_END,
    R_BE_PWR_FTM_SS, B_BE_PWR_BY_RATE_DBW_ON, R_BE_PWR_REF_CTRL, B_BE_PWR_OFST_LMT_DB,
    R_BE_PWR_OFST_LMTBF, B_BE_PWR_OFST_LMTBF_DB, R_BE_PWR_RATE_CTRL, B_BE_PWR_OFST_BYRATE_DB,
    R_BE_PWR_OFST_RULMT, B_BE_PWR_OFST_RULMT_DB, R_BE_PWR_OFST_SW, B_BE_PWR_OFST_SW_DB,
    R_BE_PWR_FORCE_LMT, B_BE_PWR_FORCE_LMT_ON, B_BE_FORCE_PWR_BY_RATE_EN,
    B_BE_PWR_FORCE_RU_ENON, B_BE_PWR_FORCE_RU_ON, R_BE_PWR_FORCE_MACID, B_BE_PWR_FORCE_MACID_ALL,
    R_BE_PWR_COEX_CTRL, B_BE_PWR_FORCE_COEX_ON, B_BE_PWR_FORCE_RATE_ON,
    R_BE_PWR_FTM, PWR_FTM_VAL, R_BE_PWR_LISTEN_PATH, B_BE_PWR_LISTEN_PATH_EN,
    R_BE_PWR_RSSI_TARGET_LMT, R_BE_PWR_TH, PWR_RSSI_TARGET_LMT_VAL, PWR_TH_VAL,
    HWSI_ADD_ADDR, B_HWSI_ADD_CTL_MASK, B_HWSI_ADD_MASK, B_HWSI_ADD_RD, B_HWSI_VAL_RDONE,
    B_HWSI_ADD_POLL_MASK, RR_POW, RR_POW_SYN_V1, RR_MODOPT, RR_TXG_SEL,
    R_COEF_SEL, R_COEF_SEL_C1, B_COEF_SEL_EN, B_COEF_SEL_IQC_V1, B_COEF_SEL_MDPD_V1,
    R_CFIR_LUT, R_CFIR_LUT_C1, B_CFIR_LUT_G3, B_CFIR_LUT_G5,
    XTAL_SI_PLL_1, XTAL_SI_APBT, XTAL_SI_XTAL_PLL, RTW89_FW_ELEMENT_ID_RF_NCTL,
    R_GOTX_IQKDPK_C0, R_GOTX_IQKDPK_C1, B_GOTX_IQKDPK, R_IQKDPK_HC, B_IQKDPK_HC,
    R_CLK_GCK, B_CLK_GCK, R_IOQ_IQK_DPK, B_IOQ_IQK_DPK_CLKEN, R_IQK_DPK_RST, B_IQK_DPK_RST,
    R_IQK_DPK_PRST, B_IQK_DPK_PRST, R_IQK_DPK_PRST_C1, R_TXRFC, B_TXRFC_RST,
    R_IQK_DPK_RST_C1, R_TXRFC_C1,
    B_BE_PWR_REF_CTRL_OFDM, B_BE_PWR_REF_CTRL_CCK, RR_BIASA,
    RR_BIASA_TXG_V1, RR_BIASA_TXA_V1, RR_BIASD_TXG_V1, RR_BIASD_TXA_V1,
    R_ADC_FIFO_V1, B_ADC_FIFO_EN_V1, R_DFS_EN, B_DFS_EN, R_TSSI_PWR, B_TSSI_CONT_EN,
    R_TXPWR_RST, B_TXPWR_RST, R_RSTB_ASYNC, B_RSTB_ASYNC_ALL, R_RXCCA_BE1, B_RXCCA_BE1_DIS,
    R_PD_CTRL, B_PD_HIT_DIS, R_MAC_SEL, B_MAC_SEL, PATH_COM_CR_AB, R_ANT_CHBW, B_ANT_RX_SG0,
    R_FC0INV_SBW, B_RX_1RCCA, R_BRK_R, B_HTMCS_LMT, B_VHTMCS_LMT, R_BRK_HE, B_N_USR_MAX,
    B_NSS_MAX, B_TB_NSS_MAX, R_BRK_EHT, B_RXEHT_NSS_MAX, R_BRK_RXEHT, B_RXEHTTB_NSS_MAX,
    B_RXEHT_N_USER_MAX, HE_N_USER_MAX_8922A,
)
from . import firmware, mac


def _phy0_phy1_offset(addr: int) -> int:
    """rtw89_phy0_phy1_offset_be: the PHY_0->PHY_1 register address delta. [SRC] phy_be.c:228-244."""
    pg = addr >> 8
    if ((0x4 <= pg <= 0xF) or (0x20 <= pg <= 0x2B) or (0x40 <= pg <= 0x4F)
            or (0x60 <= pg <= 0x6F) or (0xE4 <= pg <= 0xE5) or (0xE8 <= pg <= 0xED)):
        return 0x1000
    return 0


_DELAYS = {0xFE: 0.050, 0xFD: 0.005, 0xFC: 0.001, 0xFB: 50e-6, 0xFA: 5e-6, 0xF9: 1e-6}


def _config_bb_reg(t, addr: int, data: int, phy1: bool) -> None:
    """rtw89_phy_config_bb_reg: the flow-control delay opcodes and CR bypass, else a BB register
    write at addr + cr_base (with the PHY_1 offset). [SRC] phy.c:1402-1431."""
    if addr in _DELAYS:
        time.sleep(_DELAYS[addr])
    elif data == BYPASS_CR_DATA:
        return
    else:
        if phy1:
            addr += _phy0_phy1_offset(addr)
        t.write32(addr + CR_BASE_BE, data)


def _sel_headline(regs: list, rfe: int, cv: int) -> tuple:
    """rtw89_phy_sel_headline: choose the headline index whose (rfe, cv) target best matches.
    [SRC] phy.c:1785-1868."""
    hs = 0
    for a, _ in regs:
        if (a >> 28) != PHY_HEADLINE_VALID:
            break
        hs += 1
    if hs == 0:
        return 0, 0

    def target(a):
        return a & 0x0FFFFFFF

    def compare(r, c):
        return ((r & 0xFF) << 16) | (c & 0xFF)

    for want in (compare(rfe, cv), compare(rfe, PHY_COND_DONT_CARE)):   # case 1, case 2
        for i in range(hs):
            if target(regs[i][0]) == want:
                return hs, i
    for want_rfe in (rfe, PHY_COND_DONT_CARE):                          # case 3, case 4
        cv_max = 0
        idx = None
        for i in range(hs):
            if ((regs[i][0] >> 16) & 0xFF) == want_rfe and (regs[i][0] & 0xFF) >= cv_max:
                cv_max = regs[i][0] & 0xFF
                idx = i
        if idx is not None:
            return hs, idx
    return hs, 0


def _init_reg(regs: list, hs: int, hidx: int, config) -> None:
    """rtw89_phy_init_reg's conditional walk: call config(addr, data) for each register write
    under the branch selected by cfg_target. [SRC] phy.c:1896-1936."""
    cfg_target = regs[hidx][0] & 0x0FFFFFFF
    is_matched = True
    target_found = False
    target = 0
    for i in range(hs, len(regs)):
        a, d = regs[i]
        cond = a >> 28
        if cond in (PHY_COND_BRANCH_IF, PHY_COND_BRANCH_ELIF):
            target = a & 0x0FFFFFFF
        elif cond == PHY_COND_BRANCH_ELSE:
            is_matched = False
            if not target_found:
                return                       # malformed table: the kernel warns and bails
        elif cond == PHY_COND_BRANCH_END:
            is_matched = True
            target_found = False
        elif cond == PHY_COND_CHECK:
            if target_found:
                is_matched = False
            elif target == cfg_target:
                is_matched = True
                target_found = True
            else:
                is_matched = False
                target_found = False
        elif is_matched:
            config(a, d)


def init_bb_reg(t, cv: int) -> None:
    """rtw89_phy_init_bb_reg for the 8922A: apply the firmware BB register table for PHY_0 then
    (DBCC) PHY_1. init_txpwr_unit and bb_reset are no-ops on this chip, and bb_gain populates
    software gain arrays only. [SRC] phy.c:1940-1966, rtw8922a.c:3101,1923."""
    regs = firmware.element_regs(RTW89_FW_ELEMENT_ID_BB_REG)
    hs, hidx = _sel_headline(regs, t.rfe_type, cv)
    _init_reg(regs, hs, hidx, lambda a, d: _config_bb_reg(t, a, d, False))
    _init_reg(regs, hs, hidx, lambda a, d: _config_bb_reg(t, a, d, True))   # dbcc always on


def _phy_write32_mask(t, addr: int, mask: int, data: int) -> None:
    """rtw89_phy_write32_mask: masked BB register RMW at addr + cr_base. [SRC] phy.h:775."""
    t.write32_mask(addr + CR_BASE_BE, mask, data)


def _phy_write32_idx(t, addr: int, mask: int, data: int, phy_idx: int) -> None:
    """rtw89_phy_write32_idx: masked BB RMW, PHY_1 shifted by the phy0/phy1 offset. [SRC] phy.c:2170."""
    if phy_idx == 1:
        addr += _phy0_phy1_offset(addr)
    _phy_write32_mask(t, addr, mask, data)


def _set_phy_regs(t, addr: int, mask: int, val: int) -> None:
    """rtw89_phy_set_phy_regs: write the field on both PHY_0 and PHY_1 (DBCC). [SRC] phy.c:2206."""
    _phy_write32_idx(t, addr, mask, val, 0)
    _phy_write32_idx(t, addr, mask, val, 1)


_BBRST_MASK = (B_BE_FEN_BBPLAT_RSTB, B_BE_FEN_BB1PLAT_RSTB)     # rtw8922a.c:1798
_MCU_BOOTRDY_MASK = (B_BE_BOOT_RDY0, B_BE_BOOT_RDY1)           # rtw8922a.c:1800


def _bb_postinit(t, phy_idx: int) -> None:
    """rtw8922a_bb_postinit: FEN resets (MCU boot-ready on PHY_0, BB reset per phy), then the BB
    rate-edge / slope / magnitude register block written on both phys. [SRC] rtw8922a.c:1820-1849."""
    if phy_idx == 0:
        t.write32_set(R_BE_FEN_RST_ENABLE, _MCU_BOOTRDY_MASK[phy_idx])
    t.write32_set(R_BE_FEN_RST_ENABLE, _BBRST_MASK[phy_idx])

    t.write32_set(R_BBCLK + CR_BASE_BE, B_CLK_640M)
    t.write32_clr(R_TXSCALE + CR_BASE_BE, B_TXFCTR_EN)
    _set_phy_regs(t, R_TXFCTR, B_TXFCTR_THD, 0x200)
    _set_phy_regs(t, R_SLOPE, B_EHT_RATE_TH, 0xA)
    _set_phy_regs(t, R_BEDGE, B_HE_RATE_TH, 0xA)
    _set_phy_regs(t, R_BEDGE2, B_HT_VHT_TH, 0xAAA)
    _set_phy_regs(t, R_BEDGE, B_EHT_MCS14, 0x1)
    _set_phy_regs(t, R_BEDGE2, B_EHT_MCS15, 0x1)
    _set_phy_regs(t, R_BEDGE3, B_EHTTB_EN, 0x0)
    _set_phy_regs(t, R_BEDGE3, B_HEERSU_EN, 0x0)
    _set_phy_regs(t, R_BEDGE3, B_HEMU_EN, 0x0)
    _set_phy_regs(t, R_BEDGE3, B_TB_EN, 0x0)
    _set_phy_regs(t, R_SU_PUNC, B_SU_PUNC_EN, 0x1)
    _set_phy_regs(t, R_BEDGE5, B_HWGEN_EN, 0x1)
    _set_phy_regs(t, R_BEDGE5, B_PWROFST_COMP, 0x1)
    _set_phy_regs(t, R_MAG_AB, B_BY_SLOPE, 0x1)
    _set_phy_regs(t, R_MAG_A, B_MGA_AEND, 0xE0)
    _set_phy_regs(t, R_MAG_AB, B_MAG_AB, 0xE0C000)
    _set_phy_regs(t, R_SLOPE, B_SLOPE_A, 0x3FE0)
    _set_phy_regs(t, R_SLOPE, B_SLOPE_B, 0x3FE0)
    _set_phy_regs(t, R_SC_CORNER, B_SC_CORNER, 0x200)
    _phy_write32_idx(t, R_UDP_COEEF, B_UDP_COEEF, 0x0, phy_idx)
    _phy_write32_idx(t, R_UDP_COEEF, B_UDP_COEEF, 0x1, phy_idx)


def chip_bb_postinit(t) -> None:
    """rtw89_chip_bb_postinit: run bb_postinit for PHY_0 then (DBCC) PHY_1. [SRC] core.h/phy.c."""
    _bb_postinit(t, 0)
    _bb_postinit(t, 1)


def _write_full_rf_v2_a(t, rf_path: int, addr: int, data: int) -> None:
    """rtw89_phy_write_full_rf_v2_a: poll the HWSI idle bit, then write the addr/data-encoded word.
    [SRC] phy.c write_full_rf_v2_a."""
    for _ in range(3800):
        if not (t.read32(HWSI_IDLE_ADDR[rf_path] + CR_BASE_BE) & B_HWSI_BUSY):
            break
    val = (addr & B_HWSI_DATA_ADDR) | ((data << 8) & B_HWSI_DATA_VAL)
    t.write32(HWSI_OFST_ADDR[rf_path] + CR_BASE_BE, val)


def _read_full_rf_v2_a(t, rf_path: int, addr: int) -> int:
    """rtw89_phy_read_full_rf_v2_a: HWSI RF register read (set addr, poll, read data). [SRC] phy.c."""
    add_reg = HWSI_ADD_ADDR[rf_path] + CR_BASE_BE
    val_reg = HWSI_IDLE_ADDR[rf_path] + CR_BASE_BE
    t.write32_mask(add_reg, B_HWSI_ADD_CTL_MASK, 0x1)
    for _ in range(3800):
        if not (t.read32(val_reg) & B_HWSI_BUSY):
            break
    t.write32_mask(add_reg, B_HWSI_ADD_MASK, addr)
    t.write32_mask(add_reg, B_HWSI_ADD_RD, 0x1)
    for _ in range(3800):
        if t.read32(val_reg) & B_HWSI_VAL_RDONE:
            break
    val = t.read32(val_reg) & RFREG_MASK
    t.write32_mask(add_reg, B_HWSI_ADD_POLL_MASK, 0)
    return val


def write_rf(t, rf_path: int, addr: int, mask: int, data: int) -> None:
    """rtw89_phy_write_rf_v2: the ad_sel (DAV) direct-address RMW, else the HWSI (DDIE) write. A
    full-mask write skips the read-back; a partial mask reads the RF register first. [SRC] phy.c:
    write_rf_v2 / write_rf_a_v2 / write_rf."""
    if addr & RTW89_RF_ADDR_ADSEL_MASK:
        direct = RF_BASE_ADDR[rf_path] + ((addr & 0xFF) << 2)
        t.write32_mask(direct + CR_BASE_BE, mask & RFREG_MASK, data)
        return
    if mask == RFREG_MASK:
        val = data
    else:
        shift = (mask & -mask).bit_length() - 1
        val = (_read_full_rf_v2_a(t, rf_path, addr) & ~mask & 0xFFFFFFFF) | ((data << shift) & mask)
    _write_full_rf_v2_a(t, rf_path, addr, val)


def _config_rf_reg(t, rf_path: int, addr: int, data: int, store: list) -> None:
    """rtw89_phy_config_rf_reg_v1: the 0xfe flow-control delay, else the RF write and (for addr
    >= 0x100) the fw-H2C store entry (addr << 20 | data). [SRC] phy.c:1768-1786, 8321-8340."""
    if addr == 0xFE:
        time.sleep(0.050)
        return
    write_rf(t, rf_path, addr, RFREG_MASK, data)
    if addr < 0x100:
        return
    store.append(((addr << 20) | data) & 0xFFFFFFFF)   # rf_reg store. phy.c cofig_rf_reg_store


def _config_rf_reg_fw(t, h2c_ep: int, rf_path: int, store: list) -> None:
    """rtw89_phy_config_rf_reg_fw: send the stored RF entries as OUTSRC H2C(s), one per page of
    RTW89_H2C_RF_PAGE_SIZE, the page index as the H2C func. [SRC] phy.c config_rf_reg_fw, fw.c."""
    cls = H2C_CL_OUTSRC_RF_REG_A if rf_path == RF_PATH_A else H2C_CL_OUTSRC_RF_REG_B
    remain = len(store)
    if remain > RTW89_H2C_RF_PAGE_NUM * RTW89_H2C_RF_PAGE_SIZE:
        raise RuntimeError("rtl8922au: rf reg h2c exceeds page budget")
    page = 0
    while remain:
        n = min(remain, RTW89_H2C_RF_PAGE_SIZE)
        base = page * RTW89_H2C_RF_PAGE_SIZE
        payload = b"".join(struct.pack("<I", store[base + i]) for i in range(n))
        firmware.h2c_command(t, h2c_ep, H2C_CAT_OUTSRC, cls, page, payload, rack=False, dack=False)
        remain -= n
        page += 1


def init_rf_reg(t, h2c_ep: int, cv: int) -> None:
    """rtw89_phy_init_rf_reg(noio=false): for each radio slot, apply the firmware RF register
    table (config_rf_reg_v1) then hand the fw the stored entries. The 8922A stores RADIO_A at
    slot 1 (rf_path A) and RADIO_B at slot 0 (rf_path B), so slot 0 (path B) runs first.
    [SRC] phy.c:2060-2098, fw.c:1081-1112."""
    rf_radio = {}
    for eid, rf_path in ((RTW89_FW_ELEMENT_ID_RADIO_A, RF_PATH_A),
                         (RTW89_FW_ELEMENT_ID_RADIO_B, RF_PATH_B)):
        idx, regs = firmware.element_regs_with_idx(eid)
        rf_radio[idx] = (regs, rf_path)
    for slot in range(len(rf_radio)):
        regs, rf_path = rf_radio[slot]
        hs, hidx = _sel_headline(regs, t.rfe_type, cv)
        store = []
        _init_reg(regs, hs, hidx, lambda a, d, p=rf_path, s=store: _config_rf_reg(t, p, a, d, s))
        _config_rf_reg_fw(t, h2c_ep, rf_path, store)


# --- rtw89_phy_dm_init BB inits (pre-RFK). [SRC] phy.c:8236, phy_be.c, rtw8922a.c. ---

def _phy_write32_clr(t, addr: int, bits: int) -> None:
    """rtw89_phy_write32_clr: masked BB register RMW clearing bits. [SRC] phy.h:759."""
    t.write32_clr(addr + CR_BASE_BE, bits)


def _phy_write32_idx_set(t, addr: int, bits: int, phy_idx: int) -> None:
    """rtw89_phy_write32_idx_set: PHY_1 shifted set. [SRC] phy.c:2179."""
    if phy_idx == 1:
        addr += _phy0_phy1_offset(addr)
    t.write32_set(addr + CR_BASE_BE, bits)


def _phy_read32_idx(t, addr: int, phy_idx: int) -> int:
    """rtw89_phy_read32_idx (MASKDWORD): full BB register read, PHY_1 shifted. [SRC] phy.c:2198."""
    if phy_idx == 1:
        addr += _phy0_phy1_offset(addr)
    return t.read32(addr + CR_BASE_BE)


def _ctrl_afe_dac(t, path: int) -> None:
    """rtw8922a_ctrl_afe_dac for the default BW20 chan. [SRC] rtw8922a.c:1727."""
    ofst = 0x100 if path == RF_PATH_B else 0
    _phy_write32_mask(t, R_AFEDAC0 + ofst, B_AFEDAC0, 0xE)
    _phy_write32_mask(t, R_AFEDAC1 + ofst, B_AFEDAC1, 0x7)


def _ctrl_mlo(t) -> None:
    """rtw8922a_ctrl_mlo(MLO_1_PLUS_1_1RF): DBCC enable, per-path AFE-DAC, EMLSR params.
    [SRC] rtw8922a.c:2103."""
    _phy_write32_mask(t, R_DBCC, B_DBCC_EN, 0x1)
    _phy_write32_mask(t, R_DBCC_FA, B_DBCC_FA, 0x0)
    _ctrl_afe_dac(t, RF_PATH_A)
    _ctrl_afe_dac(t, RF_PATH_B)
    for parm in (0x6180, 0x7BAB, 0x3BAB, 0x3AAB):
        _phy_write32_mask(t, R_EMLSR, B_EMLSR_PARM, parm)


def _bb_sethw(t) -> None:
    """rtw8922a_bb_sethw. [SRC] rtw8922a.c:2147."""
    _phy_write32_clr(t, R_EN_SND_WO_NDP, B_EN_SND_WO_NDP)
    _phy_write32_clr(t, R_EN_SND_WO_NDP_C1, B_EN_SND_WO_NDP)
    t.write32_mask(R_BE_PWR_BOOST, B_BE_PWR_CTRL_SEL, 0)
    t.write32_mask(R_BE_PWR_BOOST + MAC_BAND1_OFFSET, B_BE_PWR_CTRL_SEL, 0)   # dbcc
    _ctrl_mlo(t)


def _env_monitor_one(t, phy_idx: int) -> None:
    """ccx_top_setting_init + ifs_clm_setting_init. [SRC] phy.c:6285, 6479."""
    _phy_write32_idx(t, R_CCX, B_CCX_EN_MSK, 1, phy_idx)
    _phy_write32_idx(t, R_CCX, B_CCX_TRIG_OPT_MSK, 1, phy_idx)
    _phy_write32_idx(t, R_CCX, B_MEASUREMENT_TRIG_MSK, 1, phy_idx)
    _phy_write32_idx(t, R_CCX, B_CCX_EDCCA_OPT_MSK_V1, 0, phy_idx)   # BW20_0 = 0
    for i in range(4):
        _phy_write32_idx(t, R_IFS_T[i], B_IFS_T_TH_LOW, IFS_CLM_TH_LOW[i], phy_idx)
    for i in range(4):
        _phy_write32_idx(t, R_IFS_T[i], B_IFS_T_TH_HIGH, IFS_CLM_TH_HIGH[i], phy_idx)
    _phy_write32_idx(t, R_IFS_COUNTER, B_IFS_COLLECT_EN, 1, phy_idx)
    for i in range(4):
        _phy_write32_idx(t, R_IFS_T[i], B_IFS_T_EN, 1, phy_idx)


def _ie_bitmap_addr(i: int) -> int:
    """rtw89_phy_get_ie_bitmap_addr with the page-9 collapse. [SRC] phy.c:7066-7095."""
    if i == RTW89_EHT_PKT:
        return R_PHY_STS_BITMAP_EHT
    page = i - 1 if i > RTW89_RSVD_9 else i
    return R_PHY_STS_BITMAP_START + (page << 2)


def _physts_one(t, phy_idx: int) -> None:
    """__rtw89_physts_parsing_init: disable fail/brk report, then the per-packet IE bitmap loop
    (monitor mode off, so no MU/SU monitor IEs). [SRC] phy.c:7157-7207."""
    _phy_write32_idx_set(t, R_PLCP_HISTOGRAM, B_STS_DIS_TRIG_BY_FAIL, phy_idx)
    _phy_write32_idx_set(t, R_PLCP_HISTOGRAM, B_STS_DIS_TRIG_BY_BRK, phy_idx)
    for i in range(RTW89_PHYSTS_BITMAP_NUM):
        if i == RTW89_RSVD_9:
            continue
        addr = _ie_bitmap_addr(i)
        val = _phy_read32_idx(t, addr, phy_idx)                  # get_ie_bitmap
        if i in (RTW89_HE_MU, RTW89_VHT_MU):
            val |= IE13_DL_MU_DEF
        elif i == RTW89_TRIG_BASE_PPDU:
            val |= IE13_DL_MU_DEF | IE01_CMN_OFDM
        elif i >= RTW89_CCK_PKT:
            val &= ~IE04_07_EXT_PATH
            if i == RTW89_CCK_PKT:
                val |= IE01_CMN_OFDM
            elif i >= RTW89_HT_PKT:
                val |= IE20_DBG_OFDM
        _phy_write32_idx(t, addr, MASKDWORD, val & 0xFFFFFFFF, phy_idx)   # set_ie_bitmap


def _dig_one(t, phy_idx: int) -> None:
    """rtw89_phy_dig_dyn_pd_th(enable=false): pd bounds cleared, CCK lower bound = 0x82 (from
    igi_rssi 0 / BW20 at cold init). [SRC] phy.c:7652, phy.h:132-141."""
    _phy_write32_idx(t, R_SEG0R_PD_V2, B_SEG0R_PD_LOWER_BOUND, 0, phy_idx)
    _phy_write32_idx(t, R_SEG0R_PD_V2, B_SEG0R_PD_SR_EN, 0, phy_idx)
    _phy_write32_idx(t, R_BMODE_PDTH_EN_V2, B_BMODE_PDTH_LIMIT_EN, 0, phy_idx)
    _phy_write32_idx(t, R_BMODE_PDTH_V2, B_BMODE_PDTH_LOWER_BOUND, 0x82, phy_idx)


def _cfo_init(t) -> None:
    """rtw89_phy_cfo_init: set the crystal cap (XO/XI write + read-back over xtal_si) then the
    DCFO comp enable/weight. [SRC] phy.c:5007-5102."""
    cap = t.xtal_cap & B_AX_XTAL_SC_MASK
    mac.write_xtal_si(t, XTAL_SI_XTAL_SC_XO, cap, XTAL_SC_MASK)
    mac.write_xtal_si(t, XTAL_SI_XTAL_SC_XI, cap, XTAL_SC_MASK)
    mac.read_xtal_si(t, XTAL_SI_XTAL_SC_XO)
    mac.read_xtal_si(t, XTAL_SI_XTAL_SC_XI)
    _set_phy_regs(t, R_DCFO_OPT_BE, B_DCFO_OPT_EN_BE, 1)
    _set_phy_regs(t, R_DCFO_WEIGHT_BE, B_DCFO_WEIGHT_MSK_BE, 8)


def _edcca_one(t, phy_idx: int) -> None:
    """__rtw89_phy_edcca_init (cv != CAV, so only the T2R state write). [SRC] phy.c:8202."""
    _phy_write32_idx(t, R_TX_COLLISION_T2R_ST_BE, B_TX_COLLISION_T2R_ST_BE_M, 0x29, phy_idx)


def _ch_info_init(t) -> None:
    """rtw89_phy_ch_info_init_be. [SRC] phy_be.c:1155."""
    _phy_write32_mask(t, R_CHINFO_SEG, B_CHINFO_SEG_LEN, 0)
    _phy_write32_mask(t, R_CHINFO_SEG, B_CHINFO_SEG, 0xF)
    _phy_write32_mask(t, R_CHINFO_DATA, B_CHINFO_DATA_BITMAP, 1)
    _set_phy_regs(t, R_CHINFO_ELM_SRC, B_CHINFO_ELM_BITMAP, 0x40303)
    _set_phy_regs(t, R_CHINFO_ELM_SRC, B_CHINFO_SRC, 0)
    _set_phy_regs(t, R_CHINFO_TYPE_SCAL, B_CHINFO_TYPE, 3)
    _set_phy_regs(t, R_CHINFO_TYPE_SCAL, B_CHINFO_SCAL, 0)


def _bb_wrap_one(t, mac_idx: int) -> None:
    """__rtw89_phy_bb_wrap_init_be for one MAC. tx_path/pwr-by-macid/listen-path/ul-pwr are not
    mac-scoped (repeat both passes); tpu/force-cr/ftm take the band-1 offset. [SRC] phy_be.c:1135."""
    band = MAC_BAND1_OFFSET if mac_idx else 0
    for i in range(32):                                          # tx_path_by_macid_init
        t.write32(R_BE_PWR_MACID_PATH_BASE + i * 4, 0)
    for i in range(0, 4 * 32, 4):                                # pwr_by_macid_init
        t.write32(R_BE_PWR_MACID_LMT_BASE + i, 0)
    # tpu_set_all(mac_idx)
    t.write32_mask(R_BE_PWR_FTM_SS + band, B_BE_PWR_BY_RATE_DBW_ON, 0x3)
    for a in range(R_BE_PWR_BY_RATE, R_BE_PWR_BY_RATE_END + 4, 4):
        t.write32(a + band, 0)
    for a in range(R_BE_PWR_RULMT_START, R_BE_PWR_RULMT_END + 4, 4):
        t.write32(a + band, 0)
    for a in range(R_BE_PWR_RATE_OFST_CTRL, R_BE_PWR_RATE_OFST_END + 4, 4):
        t.write32(a + band, 0)
    t.write32_mask(R_BE_PWR_REF_CTRL + band, B_BE_PWR_OFST_LMT_DB, 0)
    t.write32_mask(R_BE_PWR_OFST_LMTBF + band, B_BE_PWR_OFST_LMTBF_DB, 0)
    t.write32_mask(R_BE_PWR_RATE_CTRL + band, B_BE_PWR_OFST_BYRATE_DB, 0)
    t.write32_mask(R_BE_PWR_OFST_RULMT + band, B_BE_PWR_OFST_RULMT_DB, 0)
    t.write32_mask(R_BE_PWR_OFST_SW + band, B_BE_PWR_OFST_SW_DB, 0)
    # tx_rfsi_ctrl_init: no-op (not RTL8922D)
    # force_cr_init(mac_idx)
    t.write32_mask(R_BE_PWR_FORCE_LMT + band, B_BE_PWR_FORCE_LMT_ON, 0)
    t.write32_mask(R_BE_PWR_RATE_CTRL + band, B_BE_FORCE_PWR_BY_RATE_EN, 0)
    t.write32_mask(R_BE_PWR_OFST_RULMT + band, B_BE_PWR_FORCE_RU_ENON, 0)
    t.write32_mask(R_BE_PWR_OFST_RULMT + band, B_BE_PWR_FORCE_RU_ON, 0)
    t.write32_mask(R_BE_PWR_FORCE_MACID + band, B_BE_PWR_FORCE_MACID_ALL, 0)
    t.write32_mask(R_BE_PWR_COEX_CTRL + band, B_BE_PWR_FORCE_COEX_ON, 0)
    t.write32_mask(R_BE_PWR_BOOST + band, B_BE_PWR_FORCE_RATE_ON, 0)
    # ftm_init(mac_idx)
    t.write32(R_BE_PWR_FTM + band, PWR_FTM_VAL)
    t.write32_mask(R_BE_PWR_FTM_SS + band, 0x7, 0)
    # listen_path_en_init: always MAC_1
    t.write32_mask(R_BE_PWR_LISTEN_PATH + MAC_BAND1_OFFSET, B_BE_PWR_LISTEN_PATH_EN, 0x2)
    # ul_pwr: both MACs, not scoped by mac_idx
    for m in (0, MAC_BAND1_OFFSET):
        t.write32(R_BE_PWR_RSSI_TARGET_LMT + m, PWR_RSSI_TARGET_LMT_VAL)
        t.write32(R_BE_PWR_TH + m, PWR_TH_VAL)


def dm_init(t, cv: int) -> None:
    """rtw89_phy_dm_init up to (not including) the RFK calibration: bb_sethw, env-monitor, physts,
    dig, cfo, bb-wrap, edcca, ch-info. stat/diag/nhm/ul-tb/antdiv/rfe-gpio are no-ops on this chip.
    [SRC] phy.c:8236-8262."""
    _bb_sethw(t)
    _env_monitor_one(t, 0)
    _env_monitor_one(t, 1)
    _physts_one(t, 0)
    _physts_one(t, 1)
    _dig_one(t, 0)
    _dig_one(t, 1)
    _cfo_init(t)
    _bb_wrap_one(t, 0)
    _bb_wrap_one(t, 1)
    _edcca_one(t, 0)
    _edcca_one(t, 1)
    _ch_info_init(t)


# --- rfk_hw_init + init_rf_nctl. [SRC] rtw8922a_rfk.c, phy.c:2100-2135, phy_be.c:443. ---

RF_SYN_ON_OFF, RF_SYN_OFF_ON, RF_SYN_ALLON, RF_SYN_ALLOFF = 0, 1, 2, 3   # rtw8922a_rfk.c:110
_SYN01_CBV = {RF_SYN_ON_OFF: (0xF, 0x0), RF_SYN_OFF_ON: (0x0, 0xF),
              RF_SYN_ALLON: (0xF, 0xF), RF_SYN_ALLOFF: (0x0, 0x0)}


def _set_syn01_cbv(t, syn: int) -> None:
    """rtw8922a_set_syn01_cbv: per-path synthesizer power (RR_POW_SYN_V1). cbv (non-A-cut) sets
    each path in one write. [SRC] rtw8922a_rfk.c:145."""
    pa, pb = _SYN01_CBV[syn]
    write_rf(t, RF_PATH_A, RR_POW, RR_POW_SYN_V1, pa)
    write_rf(t, RF_PATH_B, RR_POW, RR_POW_SYN_V1, pb)


def _chlk_ktbl_sel(t, rf_path: int, idx: int) -> None:
    """rtw8922a_chlk_ktbl_sel: select the per-path calibration coefficient tables (idx 0 cold).
    [SRC] rtw8922a_rfk.c:174."""
    coef = R_COEF_SEL_C1 if rf_path == RF_PATH_B else R_COEF_SEL
    cfir = R_CFIR_LUT_C1 if rf_path == RF_PATH_B else R_CFIR_LUT
    _phy_write32_mask(t, coef, B_COEF_SEL_EN, 0x1)
    _phy_write32_mask(t, coef, B_COEF_SEL_IQC_V1, idx)
    _phy_write32_mask(t, coef, B_COEF_SEL_MDPD_V1, idx)
    write_rf(t, rf_path, RR_MODOPT, RR_TXG_SEL, 0x4 | idx)
    g3 = _phy_read32_idx(t, coef, 0) & 0x1                  # read R_COEF_SEL BIT(0)
    _phy_write32_mask(t, cfir, B_CFIR_LUT_G3, g3)
    g5 = (_phy_read32_idx(t, coef, 0) >> 1) & 0x1           # read R_COEF_SEL BIT(1)
    _phy_write32_mask(t, cfir, B_CFIR_LUT_G5, g5)


def _rfk_pll_init(t) -> None:
    """rtw8922a_rfk_pll_init: three xtal_si read-modify-writes. [SRC] rtw8922a_rfk.c:325."""
    mac.write_xtal_si(t, XTAL_SI_PLL_1, mac.read_xtal_si(t, XTAL_SI_PLL_1) | 0xF8, 0xFF)
    mac.write_xtal_si(t, XTAL_SI_APBT, mac.read_xtal_si(t, XTAL_SI_APBT) & ~0x60 & 0xFF, 0xFF)
    mac.write_xtal_si(t, XTAL_SI_XTAL_PLL, mac.read_xtal_si(t, XTAL_SI_XTAL_PLL) | 0x38, 0xFF)


def rfk_hw_init(t) -> None:
    """rtw8922a_rfk_hw_init: syn power (DBCC MLO), the per-path coefficient-table select, and the
    RFK PLL init. [SRC] rtw8922a_rfk.c:352."""
    _set_syn01_cbv(t, RF_SYN_ALLON)                         # rfk_mlo_ctrl -> set_syn01
    _chlk_ktbl_sel(t, RF_PATH_A, 0)                          # chlk_reload -> ktbl_sel (idx 0 cold)
    _chlk_ktbl_sel(t, RF_PATH_B, 0)
    _rfk_pll_init(t)


def _preinit_rf_nctl(t) -> None:
    """rtw89_phy_preinit_rf_nctl_be: IQK/DPK clock + reset before the NCTL table. [SRC] phy_be.c:443."""
    _phy_write32_mask(t, R_GOTX_IQKDPK_C0, B_GOTX_IQKDPK, 0x3)
    _phy_write32_mask(t, R_GOTX_IQKDPK_C1, B_GOTX_IQKDPK, 0x3)
    _phy_write32_mask(t, R_IQKDPK_HC, B_IQKDPK_HC, 0x1)
    _phy_write32_mask(t, R_CLK_GCK, B_CLK_GCK, 0xFFFFF)
    _phy_write32_mask(t, R_IOQ_IQK_DPK, B_IOQ_IQK_DPK_CLKEN, 0x3)
    _phy_write32_mask(t, R_IQK_DPK_RST, B_IQK_DPK_RST, 0x1)
    _phy_write32_mask(t, R_IQK_DPK_PRST, B_IQK_DPK_PRST, 0x1)
    _phy_write32_mask(t, R_IQK_DPK_PRST_C1, B_IQK_DPK_PRST, 0x1)
    _phy_write32_mask(t, R_TXRFC, B_TXRFC_RST, 0x1)
    _phy_write32_mask(t, R_IQK_DPK_RST_C1, B_IQK_DPK_RST, 0x1)          # dbcc
    _phy_write32_mask(t, R_TXRFC_C1, B_TXRFC_RST, 0x1)                 # dbcc


def init_rf_nctl(t, cv: int) -> None:
    """rtw89_phy_init_rf_nctl: the preinit clock/reset then the RF_NCTL fw-element register table
    (config_bb_reg, single pass, rfe/cv-filtered). nctl_post_table is NULL on the 8922A. [SRC]
    phy.c:2123-2135."""
    _preinit_rf_nctl(t)
    regs = firmware.element_regs(RTW89_FW_ELEMENT_ID_RF_NCTL)
    hs, hidx = _sel_headline(regs, t.rfe_type, cv)
    _init_reg(regs, hs, hidx, lambda a, d: _config_bb_reg(t, a, d, False))


# --- set_txpwr_ctrl + power_trim. [SRC] rtw8922a.c:2429,942-1041. ---

def set_txpwr_ctrl(t) -> None:
    """rtw8922a_set_txpwr_ctrl -> set_txpwr_ref: clear the OFDM then CCK power reference on both
    bands (get_txpwr_cr adds +0x4000 for band 1). [SRC] rtw8922a.c:2429-2445, 2559."""
    for band in (0, MAC_BAND1_OFFSET):
        t.write32_mask(R_BE_PWR_REF_CTRL + band, B_BE_PWR_REF_CTRL_OFDM, 0)
        t.write32_mask(R_BE_PWR_REF_CTRL + band, B_BE_PWR_REF_CTRL_CCK, 0)


def power_trim(t) -> None:
    """rtw8922a_power_trim: pa_bias then pad_bias, per path, from the phycap trim nibbles.
    [SRC] rtw8922a.c:965-1041."""
    if not t.pg_pa_bias_trim:
        return
    for i in (RF_PATH_A, RF_PATH_B):
        write_rf(t, i, RR_BIASA, RR_BIASA_TXG_V1, t.pa_bias_trim[i] & 0xF)
        write_rf(t, i, RR_BIASA, RR_BIASA_TXA_V1, (t.pa_bias_trim[i] >> 4) & 0xF)
    for i in (RF_PATH_A, RF_PATH_B):
        write_rf(t, i, RR_BIASA, RR_BIASD_TXG_V1, t.pad_bias_trim[i] & 0xF)
        write_rf(t, i, RR_BIASA, RR_BIASD_TXA_V1, (t.pad_bias_trim[i] >> 4) & 0xF)


# --- bb_cfg_txrx_path (hal_reset + ctrl_trx_path). [SRC] rtw8922a.c:2298-2626. ---

def _adc_en_path(t, path: int, en: bool) -> None:
    """rtw8922a_adc_en_path: RMW the path's enable bit in the shared R_ADC_FIFO_V1 field. [SRC]
    rtw8922a.c:2263."""
    val = (t.read32(R_ADC_FIFO_V1 + CR_BASE_BE) >> 24) & 0xFF
    bit = 0x1 if path == RF_PATH_A else 0x2
    val = (val & ~bit) if en else (val | bit)
    _phy_write32_mask(t, R_ADC_FIFO_V1, B_ADC_FIFO_EN_V1, val & 0xFF)


def _adc_en(t, phy_idx: int, en: bool) -> None:
    """rtw8922a_adc_en: MLO 1+1 does one path by phy_idx, else both paths. [SRC] rtw8922a.c:2285."""
    if t.mlo_1_1:
        _adc_en_path(t, RF_PATH_A if phy_idx == 0 else RF_PATH_B, en)
    else:
        _adc_en_path(t, RF_PATH_A, en)
        _adc_en_path(t, RF_PATH_B, en)


def _dfs_en(t, phy_idx: int, en: bool) -> None:
    """rtw8922a_dfs_en: both paths. [SRC] rtw8922a.c:2229-2246."""
    for ofst in (0, 0x100):
        _phy_write32_idx(t, R_DFS_EN + ofst, B_DFS_EN, 1 if en else 0, phy_idx)


def _tssi_cont_en(t, phy_idx: int, en: bool) -> None:
    """rtw8922a_tssi_cont_en_phyidx: MLO 1+1 does one path by phy_idx, else both paths. [SRC]
    rtw8922a_rfk.c:21-40."""
    val = 0 if en else 1
    if t.mlo_1_1:
        path = RF_PATH_A if phy_idx == 0 else RF_PATH_B
        _phy_write32_mask(t, R_TSSI_PWR[path], B_TSSI_CONT_EN, val)
    else:
        _phy_write32_mask(t, R_TSSI_PWR[RF_PATH_A], B_TSSI_CONT_EN, val)
        _phy_write32_mask(t, R_TSSI_PWR[RF_PATH_B], B_TSSI_CONT_EN, val)


def _tssi_reset(t, phy_idx: int) -> None:
    """rtw8922a_tssi_reset: MLO 1+1 resets RSTA for phy0, RSTB for phy1. [SRC] rtw8922a.c:2016."""
    reg = R_TXPWR_RST[phy_idx]
    _phy_write32_mask(t, reg, B_TXPWR_RST, 0)
    _phy_write32_mask(t, reg, B_TXPWR_RST, 1)


def _bb_reset_en(t, phy_idx: int, band: int, en: bool) -> None:
    """rtw8922a_bb_reset_en: the RXCCA re-enable only on the 2G band. [SRC] rtw8922a.c:1851."""
    if en:
        _phy_write32_idx(t, R_RSTB_ASYNC, B_RSTB_ASYNC_ALL, 1, phy_idx)
        if band == RTW89_BAND_2G:
            _phy_write32_idx(t, R_RXCCA_BE1, B_RXCCA_BE1_DIS, 0, phy_idx)
        _phy_write32_idx(t, R_PD_CTRL, B_PD_HIT_DIS, 0, phy_idx)
    else:
        _phy_write32_idx(t, R_RXCCA_BE1, B_RXCCA_BE1_DIS, 1, phy_idx)
        _phy_write32_idx(t, R_PD_CTRL, B_PD_HIT_DIS, 1, phy_idx)
        _phy_write32_idx(t, R_RSTB_ASYNC, B_RSTB_ASYNC_ALL, 0, phy_idx)


def _hal_reset(t, phy_idx: int, mac_idx: int, band: int, enter: bool, tx_en: int) -> int:
    """rtw8922a_hal_reset: quiesce (enter) or re-enable (leave) TX/RX around a channel/path change.
    [SRC] rtw8922a.c:2299."""
    if enter:
        tx_en = mac.stop_sch_tx(t, mac_idx)
        mac.cfg_ppdu_status(t, mac_idx, False)
        _dfs_en(t, phy_idx, False)
        _tssi_cont_en(t, phy_idx, False)
        _adc_en(t, phy_idx, False)
        _bb_reset_en(t, phy_idx, band, False)
        return tx_en
    mac.cfg_ppdu_status(t, mac_idx, True)
    _adc_en(t, phy_idx, True)
    _dfs_en(t, phy_idx, True)
    _tssi_cont_en(t, phy_idx, True)
    _bb_reset_en(t, phy_idx, band, True)
    mac.resume_sch_tx(t, mac_idx, tx_en)
    return tx_en


def _cfg_rx_nss_limit(t, phy_idx: int) -> None:
    """rtw8922a_cfg_rx_nss_limit(rx_nss=2). [SRC] rtw8922a.c:1927."""
    _phy_write32_idx(t, R_BRK_R, B_HTMCS_LMT, 1, phy_idx)
    _phy_write32_idx(t, R_BRK_R, B_VHTMCS_LMT, 1, phy_idx)
    _phy_write32_idx(t, R_BRK_HE, B_N_USR_MAX, HE_N_USER_MAX_8922A, phy_idx)
    _phy_write32_idx(t, R_BRK_HE, B_NSS_MAX, 1, phy_idx)
    _phy_write32_idx(t, R_BRK_HE, B_TB_NSS_MAX, 1, phy_idx)
    _phy_write32_idx(t, R_BRK_EHT, B_RXEHT_NSS_MAX, 1, phy_idx)
    _phy_write32_idx(t, R_BRK_RXEHT, B_RXEHTTB_NSS_MAX, 1, phy_idx)
    _phy_write32_idx(t, R_BRK_RXEHT, B_RXEHT_N_USER_MAX, HE_N_USER_MAX_8922A, phy_idx)


def _ctrl_tx_path_tmac(t, phy_idx: int) -> None:
    """rtw8922a_ctrl_tx_path_tmac(RF_PATH_AB). [SRC] rtw8922a.c:1867."""
    _phy_write32_idx(t, R_MAC_SEL, B_MAC_SEL, 0, phy_idx)
    for addr, data in PATH_COM_CR_AB:
        t.write32(mac._reg_by_idx(addr, phy_idx), data)


def _ctrl_rx_path_tmac(t, phy_idx: int) -> None:
    """rtw8922a_ctrl_rx_path_tmac(RF_PATH_AB): clear SG0, set AB, nss-limit, tssi reset. [SRC]
    rtw8922a.c:1981."""
    _phy_write32_idx(t, R_ANT_CHBW, B_ANT_RX_SG0, 0, phy_idx)
    _phy_write32_idx(t, R_ANT_CHBW, B_ANT_RX_SG0, 3, phy_idx)
    _phy_write32_idx(t, R_FC0INV_SBW, B_RX_1RCCA, 3, phy_idx)
    _cfg_rx_nss_limit(t, phy_idx)
    _tssi_reset(t, phy_idx)


def bb_cfg_txrx_path(t) -> None:
    """rtw8922a_bb_cfg_txrx_path: quiesce both bands, set the AB tx/rx paths + rx-nss, re-enable.
    [SRC] rtw8922a.c:2565-2626."""
    tx_en = [0, 0]
    tx_en[0] = _hal_reset(t, 0, 0, RTW89_BAND_2G, True, 0)
    tx_en[1] = _hal_reset(t, 1, 1, RTW89_BAND_2G, True, 0)
    for phy_idx in (0, 1):                       # ctrl_trx_path(AB, 2, AB, 2)
        _ctrl_tx_path_tmac(t, phy_idx)
        _ctrl_rx_path_tmac(t, phy_idx)
        _cfg_rx_nss_limit(t, phy_idx)
    _hal_reset(t, 0, 0, RTW89_BAND_2G, False, tx_en[0])
    _hal_reset(t, 1, 1, RTW89_BAND_2G, False, tx_en[1])


def pre_set_channel_bb(t, phy_idx: int = 0) -> None:
    """rtw8922a_pre_set_channel_bb (dbcc_en): clear DBCC_EN, then load the per-PHY EMLSR parm
    table. Runs at the head of the per-channel set_channel. [SRC] rtw8922a.c:2199."""
    _phy_write32_mask(t, R_DBCC, B_DBCC_EN, 0x0)
    parms = (0x6180, 0xBBAB, 0xABA9, 0xEBA9, 0xEAA9) if phy_idx == 0 \
        else (0xBBAB, 0xAFFF, 0xEFFF, 0xEEFF)
    for parm in parms:
        _phy_write32_mask(t, R_EMLSR, B_EMLSR_PARM, parm)


# rtw8922a_sco_barker_threshold / rtw8922a_sco_cck_threshold, ch 1-14. [SRC] rtw8922a.c:1139.
_SCO_BARKER = (0x1FE4F, 0x1FF5E, 0x2006C, 0x2017B, 0x2028A, 0x20399, 0x204A8,
               0x205B6, 0x206C5, 0x207D4, 0x208E3, 0x209F2, 0x20B00, 0x20D8A)
_SCO_CCK = (0x2BDAC, 0x2BF21, 0x2C095, 0x2C209, 0x2C37E, 0x2C4F2, 0x2C666,
            0x2C7DB, 0x2C94F, 0x2CAC3, 0x2CC38, 0x2CDAC, 0x2CF21, 0x2D29E)


def _ctrl_sco_cck(t, primary_ch: int, phy_idx: int) -> None:
    """rtw8922a_ctrl_sco_cck: the per-2G-channel Barker/CCK FC0-inverse thresholds. [SRC]
    rtw8922a.c:1149."""
    ch_element = primary_ch - 1                  # primary_ch < 14 for 2G
    _phy_write32_idx(t, R_BK_FC0INV, B_BK_FC0INV, _SCO_BARKER[ch_element], phy_idx)
    _phy_write32_idx(t, R_CCK_FC0INV, B_CCK_FC0INV, _SCO_CCK[ch_element], phy_idx)


# set_gain reg-def tables: (gain_g[path A], gain_g[path B], gain_g_mask). 2G uses the gain_g
# columns. [SRC] rtw8922a.c:1204-1262.
_BB_GAIN_LNA = (
    (0x409C, 0x449C, 0xFF00), (0x409C, 0x449C, 0xFF000000),
    (0x40A0, 0x44A0, 0xFF00), (0x40A0, 0x44A0, 0xFF000000),
    (0x40A4, 0x44A4, 0xFF00), (0x40A4, 0x44A4, 0xFF000000),
    (0x40A8, 0x44A8, 0xFF00),
)
_BB_GAIN_TIA = ((0x4054, 0x4454, 0x7FC0000), (0x4058, 0x4458, 0x1FF))
_BB_OP1DB_LNA = (
    (0x40AC, 0x44AC, 0xFF00), (0x40AC, 0x44AC, 0xFF0000), (0x40AC, 0x44AC, 0xFF000000),
    (0x40B0, 0x44B0, 0xFF), (0x40B0, 0x44B0, 0xFF00), (0x40B0, 0x44B0, 0xFF0000),
    (0x40B0, 0x44B0, 0xFF000000),
)
_BB_OP1DB_TIA_LNA = (
    (0x40B4, 0x44B4, 0xFF0000), (0x40B4, 0x44B4, 0xFF000000),
    (0x40B8, 0x44B8, 0xFF), (0x40B8, 0x44B8, 0xFF00), (0x40B8, 0x44B8, 0xFF0000),
    (0x40B8, 0x44B8, 0xFF000000), (0x40BC, 0x44BC, 0xFF), (0x40BC, 0x44BC, 0xFF00),
)
# RPL compensation tables: (addr, mask); path B ORs 0x400. [SRC] rtw8922a.c:1177-1202.
_RPL_BW160 = ((0x41E8, 0xFF00), (0x41E8, 0xFF0000), (0x41E8, 0xFF000000), (0x41EC, 0xFF),
              (0x41EC, 0xFF00), (0x41EC, 0xFF0000), (0x41EC, 0xFF000000), (0x41F0, 0xFF))
_RPL_BW80 = ((0x41F4, 0xFF), (0x41F4, 0xFF00), (0x41F4, 0xFF0000), (0x41F4, 0xFF000000))
_RPL_BW40 = ((0x41F0, 0xFF0000), (0x41F0, 0xFF000000))
_RPL_BW20 = ((0x41F0, 0xFF00),)


def _s8(b: int) -> int:
    return b - 256 if b >= 128 else b


def _le_bytes(data: int, n: int) -> list:
    return [(data >> (8 * i)) & 0xFF for i in range(n)]


def _decode_bb_gain(t) -> dict:
    """rtw89_phy_config_bb_gain_be: decode the BB-gain FW element (reg2 pairs) into the be gain
    arrays, cached on the transport. Each pair's addr packs type/path/bw/gain_band/cfg_type; the
    data bytes fill consecutive array slots. [SRC] phy_be.c:265-441, fw.c:1099."""
    if t.bb_gain is not None:
        return t.bb_gain
    g = {k: {} for k in ("lna_gain", "tia_gain", "lna_op1db", "tia_lna_op1db",
                         "rpl_20", "rpl_40", "rpl_80", "rpl_160")}
    for addr, data in firmware.element_regs(RTW89_FW_ELEMENT_ID_BB_GAIN):
        typ = addr & 0xFF
        path = (addr >> 8) & 0xF
        bw = (addr >> 12) & 0xF
        gband = (addr >> 16) & 0xFF
        cfg = (addr >> 24) & 0xFF
        if bw >= 2 or gband >= 12 or path >= 2 or 0xF9 <= addr <= 0xFE:
            continue
        if cfg == 0:                                       # gain_error (lna/tia)
            if typ == 0:
                for i, b in enumerate(_le_bytes(data, 4)):
                    g["lna_gain"][(gband, bw, path, i)] = b
            elif typ == 1:
                for k, b in enumerate(_le_bytes(data, 3)):
                    g["lna_gain"][(gband, bw, path, 4 + k)] = b
            elif typ == 2:
                for i, b in enumerate(_le_bytes(data, 2)):
                    g["tia_gain"][(gband, bw, path, i)] = b
        elif cfg == 1:                                     # rpl_ofst
            sub0, sub1 = typ & 0xF, (typ >> 4) & 0xF
            if sub1 == 0:
                g["rpl_20"][(gband, path, 0)] = data & 0xFF
            elif sub1 == 1:
                for i, b in enumerate(_le_bytes(data, 2)):
                    g["rpl_40"][(gband, path, i)] = b
            elif sub1 == 2:
                for i, b in enumerate(_le_bytes(data, 4)):
                    g["rpl_80"][(gband, path, i)] = b
            elif sub1 == 3:
                ofst = 0 if sub0 == 0 else 4
                for k, b in enumerate(_le_bytes(data, 4)):
                    g["rpl_160"][(gband, path, k + ofst)] = b
        elif cfg == 3:                                     # op1db
            if typ == 0:
                for i, b in enumerate(_le_bytes(data, 4)):
                    g["lna_op1db"][(gband, bw, path, i)] = b
            elif typ == 1:
                for k, b in enumerate(_le_bytes(data, 3)):
                    g["lna_op1db"][(gband, bw, path, 4 + k)] = b
            elif typ == 2:
                for i, b in enumerate(_le_bytes(data, 4)):
                    g["tia_lna_op1db"][(gband, bw, path, i)] = b
            elif typ == 3:
                for k, b in enumerate(_le_bytes(data, 4)):
                    g["tia_lna_op1db"][(gband, bw, path, 4 + k)] = b
    t.bb_gain = g
    return g


def _set_lna_tia_gain(t, g: dict, gband: int, bw: int, path: int, phy_idx: int) -> None:
    """rtw8922a_set_lna_tia_gain: LNA, TIA, op1db-LNA, op1db-TIA-LNA gains for one path (2G gain_g).
    [SRC] rtw8922a.c:1316."""
    col = 0 if path == RF_PATH_A else 1
    for i, e in enumerate(_BB_GAIN_LNA):
        _phy_write32_idx(t, e[col], e[2], _s8(g["lna_gain"].get((gband, bw, path, i), 0)), phy_idx)
    for i, e in enumerate(_BB_GAIN_TIA):
        _phy_write32_idx(t, e[col], e[2], _s8(g["tia_gain"].get((gband, bw, path, i), 0)), phy_idx)
    for i, e in enumerate(_BB_OP1DB_LNA):
        _phy_write32_idx(t, e[col], e[2], _s8(g["lna_op1db"].get((gband, bw, path, i), 0)), phy_idx)
    for i, e in enumerate(_BB_OP1DB_TIA_LNA):
        _phy_write32_idx(t, e[col], e[2], _s8(g["tia_lna_op1db"].get((gband, bw, path, i), 0)), phy_idx)


def _set_rpl_gain(t, g: dict, gband: int, path: int, phy_idx: int) -> None:
    """rtw8922a_set_rpl_gain: RPL compensation offsets for one path (path B +0x400). [SRC]
    rtw8922a.c:1271."""
    pofs = 0x400 if path == RF_PATH_B else 0
    for i, (a, mask) in enumerate(_RPL_BW160):
        _phy_write32_idx(t, a | pofs, mask, _s8(g["rpl_160"].get((gband, path, i), 0)), phy_idx)
    for i, (a, mask) in enumerate(_RPL_BW80):
        _phy_write32_idx(t, a | pofs, mask, _s8(g["rpl_80"].get((gband, path, i), 0)), phy_idx)
    for i, (a, mask) in enumerate(_RPL_BW40):
        _phy_write32_idx(t, a | pofs, mask, _s8(g["rpl_40"].get((gband, path, i), 0)), phy_idx)
    for i, (a, mask) in enumerate(_RPL_BW20):
        _phy_write32_idx(t, a | pofs, mask, _s8(g["rpl_20"].get((gband, path, i), 0)), phy_idx)


def _set_gain(t, chan: dict, path: int, phy_idx: int) -> None:
    """rtw8922a_set_gain: LNA/TIA/op1db then RPL for one path, from the decoded BB-gain element.
    2G only for now (5/6G needs the gain_a tables + 5G gain_band). [SRC] rtw8922a.c:1381."""
    if chan["band_type"] != RTW89_BAND_2G:
        raise NotImplementedError("set_gain 5G/6G not ported yet")
    gband, bw = 0, 0                    # RTW89_BB_GAIN_BAND_2G_BE, RTW89_BB_BW_20_40 (<=40 MHz)
    g = _decode_bb_gain(t)
    _set_lna_tia_gain(t, g, gband, bw, path, phy_idx)
    _set_rpl_gain(t, g, gband, path, phy_idx)


_BAND_SEL = (0x4160, 0x4560)             # rtw8922a_ctrl_ch band_sel[path]. rtw8922a.c:1494
_B_BAND_SEL = 1 << 26                    # BIT(26)
# set_rx_gain_normal_ofdm per-path reg tables. [SRC] rtw8922a.c:1420-1424.
_RSSI_OFST = (0x40C8, 0x44C8)
_RPL_BIAS_COMP = (0x41E8, 0x45E8)
_RPL_EXT_COMP = (0x41F8, 0x45F8)
_RSSI_TB_BIAS_COMP = (0x41F8, 0x45F8)
_RSSI_TB_EXT_COMP = (0x4208, 0x4608)


def _clamp_s8(v: int) -> int:
    return -128 if v < -128 else (127 if v > 127 else v)


def _set_rx_gain_normal_cck(t, path: int, phy_idx: int) -> None:
    """rtw8922a_set_rx_gain_normal_cck: MGAIN bias (bw20/40) + CCK RPL offset from the 2G-CCK
    gain-offset. [SRC] rtw8922a.c:1391."""
    value = _s8(t.gain_offset[path][GAIN_OFFSET_2G_CCK])
    value = -value
    fraction = value & 0x3
    if fraction:
        _phy_write32_mask(t, R_MGAIN_BIAS, B_MGAIN_BIAS_BW20, (0x4 - fraction) << 1)
        _phy_write32_mask(t, R_MGAIN_BIAS, B_MGAIN_BIAS_BW40, (0x4 - fraction) << 1)
        value >>= 2
        _phy_write32_mask(t, R_CCK_RPL_OFST, B_CCK_RPL_OFST, value + 1 + 0xDC)
    else:
        _phy_write32_mask(t, R_MGAIN_BIAS, B_MGAIN_BIAS_BW20, 0)
        _phy_write32_mask(t, R_MGAIN_BIAS, B_MGAIN_BIAS_BW40, 0)
        value >>= 2
        _phy_write32_mask(t, R_CCK_RPL_OFST, B_CCK_RPL_OFST, value + 0xDC)


def _set_rx_gain_normal_ofdm(t, gain_band: int, path: int, phy_idx: int) -> None:
    """rtw8922a_set_rx_gain_normal_ofdm: RSSI offset + the RPL/TB bias/ext compensation triplet
    (value*-4 split into three clamped s8 parts). [SRC] rtw8922a.c:1416."""
    value = _s8(t.gain_offset[path][gain_band])
    _phy_write32_mask(t, _RSSI_OFST[path], 0xFF000000, value + 0xF8)
    value *= -4
    v1 = _clamp_s8(value)
    value -= v1
    v2 = _clamp_s8(value)
    value -= v2
    v3 = _clamp_s8(value)
    _phy_write32_mask(t, _RPL_BIAS_COMP[path], 0xFF, v1)
    _phy_write32_mask(t, _RPL_EXT_COMP[path], 0xFF, v2)
    _phy_write32_mask(t, _RPL_EXT_COMP[path], 0xFF00, v3)
    _phy_write32_mask(t, _RSSI_TB_BIAS_COMP[path], 0xFF0000, v1)
    _phy_write32_mask(t, _RSSI_TB_EXT_COMP[path], 0xFF0000, v2)
    _phy_write32_mask(t, _RSSI_TB_EXT_COMP[path], 0xFF000000, v3)


def _set_rx_gain_normal(t, chan: dict, path: int, phy_idx: int) -> None:
    """rtw8922a_set_rx_gain_normal: on 2G the CCK path plus the OFDM path (2G_OFDM band). Nothing
    if the efuse gain offset is invalid. [SRC] rtw8922a.c:1448."""
    if not t.gain_offset_valid:
        return
    if chan["band_type"] == RTW89_BAND_2G:
        _set_rx_gain_normal_cck(t, path, phy_idx)
        _set_rx_gain_normal_ofdm(t, GAIN_OFFSET_2G_OFDM, path, phy_idx)
    else:
        raise NotImplementedError("set_rx_gain_normal 5/6G gain-offset band not ported yet")


# rtw8922a_set_cck_parameters R_PCOEFF tables (ch 14 vs the rest). [SRC] rtw8922a.c:1466.
_PCOEFF_CH14 = (0x3B13FF, 0x1C42DE, 0xFDB0AD, 0xF60F6E, 0xFD8F92, 0x02D011, 0x01C02C, 0xFFF00A)
_PCOEFF_OTHER = (0x3A63CA, 0x2A833F, 0x1491F8, 0x03C0B0, 0xFCCFF1, 0xFCCFC3, 0xFEBFDC, 0xFFDFF7)


def _set_cck_parameters(t, central_ch: int, phy_idx: int) -> None:
    """rtw8922a_set_cck_parameters: the 8 CCK phase-coefficient registers (R_PCOEFF01..EF), one
    table for ch 14 and one for every other 2G channel. [SRC] rtw8922a.c:1466."""
    vals = _PCOEFF_CH14 if central_ch == 14 else _PCOEFF_OTHER
    for i, v in enumerate(vals):
        _phy_write32_idx(t, R_PCOEFF01 + i * 4, B_PCOEFF, v, phy_idx)


def _encode_chan_idx(central_ch: int, band: int) -> int:
    """rtw89_encode_chan_idx (2G): base-idx 2G plus the channel in the offset field. [SRC]
    phy.c:8574-8593."""
    if band != RTW89_BAND_2G:
        raise NotImplementedError("encode_chan_idx 5/6G (base-idx scan) not ported yet")
    return ((RTW89_CH_BASE_IDX_2G << 4) & RTW89_CH_BASE_IDX_MASK) | (central_ch & RTW89_CH_OFFSET_MASK)


def _ctrl_ch(t, chan: dict, phy_idx: int) -> None:
    """rtw8922a_ctrl_ch: per-path gain, band-sel, rx-gain, center-freq, sco, cck-params, chan-idx.
    set_gain + band_sel + set_rx_gain_normal are ported so far. [SRC] rtw8922a.c:1490."""
    _set_gain(t, chan, RF_PATH_A, phy_idx)
    _set_gain(t, chan, RF_PATH_B, phy_idx)
    is_2g = 1 if chan["band_type"] == RTW89_BAND_2G else 0
    for path in (RF_PATH_A, RF_PATH_B):
        _phy_write32_idx(t, _BAND_SEL[path], _B_BAND_SEL, is_2g, phy_idx)
    _set_rx_gain_normal(t, chan, RF_PATH_A, phy_idx)
    _set_rx_gain_normal(t, chan, RF_PATH_B, phy_idx)
    freq = chan["freq"]
    _phy_write32_idx(t, R_FC0, B_FC0, freq, phy_idx)
    sco = (262144 + freq // 2) // freq             # DIV_ROUND_CLOSEST(1 << 18, central_freq)
    _phy_write32_idx(t, R_FC0INV_SBW, B_FC0_INV, sco, phy_idx)
    if chan["band_type"] == RTW89_BAND_2G:
        _set_cck_parameters(t, chan["channel"], phy_idx)
    chan_idx = _encode_chan_idx(chan["primary_channel"], chan["band_type"])
    _phy_write32_idx(t, R_MAC_PIN_SEL, B_CH_IDX_SEG0, chan_idx, phy_idx)


def set_channel_bb(t, chan: dict, phy_idx: int = 0) -> None:
    """rtw8922a_set_channel_bb: 2G CCK sco thresholds, then ctrl_ch (gain + freq tables), ctrl_bw,
    ctrl_cck_en, spur elimination. ctrl_sco_cck + ctrl_ch's set_gain are ported. [SRC]
    rtw8922a.c:2179."""
    cck_en = chan["band_type"] == RTW89_BAND_2G
    if cck_en:
        _ctrl_sco_cck(t, chan["primary_channel"], phy_idx)
    _ctrl_ch(t, chan, phy_idx)
    # TODO: ctrl_bw(pri_sb, bw); ctrl_cck_en(cck_en); spur_elimination(chan); R_RSTB_ASYNC;
    #       tssi_reset(AB).


def pre_set_channel_rf(t, cv: int, phy_idx: int = 0) -> None:
    """rtw8922a_pre_set_channel_rf (dbcc_en): set_syn01 power per MLO mode. mlo_1_1 powers both;
    else PHY_0 -> ON_OFF (syn A on, B off), PHY_1 -> OFF_ON. [SRC] rtw8922a_rfk.c:360."""
    if cv == CHIP_CAV:
        raise NotImplementedError("set_syn01 A-cut path not needed on this card")
    if t.mlo_1_1:
        syn = RF_SYN_ALLON
    else:
        syn = RF_SYN_ON_OFF if phy_idx == 0 else RF_SYN_OFF_ON
    _set_syn01_cbv(t, syn)


def set_channel_help(t, cv: int, band: int, enter: bool, phy_idx: int = 0, mac_idx: int = 0,
                     tx_en: int = 0) -> int:
    """rtw8922a_set_channel_help: on enter, pre_set_channel bb/rf then hal_reset quiesce; on leave,
    hal_reset re-enable then post_set_channel bb/rf. Returns tx_en (needed for the leave call).
    Only the enter arm is ported so far. [SRC] rtw8922a.c:2321."""
    if enter:
        pre_set_channel_bb(t, phy_idx)
        pre_set_channel_rf(t, cv, phy_idx)
    tx_en = _hal_reset(t, phy_idx, mac_idx, band, enter, tx_en)
    if not enter:
        raise NotImplementedError("set_channel_help leave (post_set_channel bb/rf) not ported yet")
    return tx_en
