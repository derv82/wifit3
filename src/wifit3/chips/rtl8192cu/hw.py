from __future__ import annotations

import errno
import logging
import time

from . import constants as C
from . import table
from .cam import rtl_cam_reset_all_entry
from .dm_common import rtl92c_dm_check_txpower_tracking, rtl92c_dm_init
from .efuse import efuse_read_1byte, rtl_get_hwinfo
from .fw_common import rtl92c_download_fw
from .mac import (
    IS_92C_SERIAL,
    IS_NORMAL_CHIP,
    IS_VENDOR_UMC_A_CUT,
    rtl92c_init_adaptive_ctrl,
    rtl92c_init_ampdu_aggregation,
    rtl92c_init_beacon_max_error,
    rtl92c_init_driver_info_size,
    rtl92c_init_edca,
    rtl92c_init_interrupt,
    rtl92c_init_llt_table,
    rtl92c_init_network_type,
    rtl92c_init_rate_fallback,
    rtl92c_init_retry_function,
    rtl92c_set_min_space,
)
from .phy import rtl92cu_bb_block_on, rtl92cu_phy_bb_config, rtl92cu_phy_mac_config
from .phy_common import (
    rtl92c_phy_iq_calibrate,
    rtl92c_phy_lc_calibrate,
    rtl92c_phy_rf_config,
    rtl92c_phy_set_rfpath_switch,
)
from .priv import RtlPriv
from .wifi import rtl_get_rfreg, rtl_set_rfreg

logger = logging.getLogger(__name__)


def _s8(value: int) -> int:
    value &= 0xFF
    return value - 0x100 if value & 0x80 else value


def rtl92c_get_chnl_group(chnl: int) -> int:
    """rtl8192ce/hw.h:7."""
    if chnl < 3:
        return 0
    if chnl < 9:
        return 1
    return 2


def IS_HIGHT_PA(boardtype: int) -> bool:
    return boardtype == C.BOARD_USB_HIGH_PA


def _rtl92cu_read_txpower_info_from_hwpg(rtlpriv: RtlPriv, autoload_fail: bool, hwinfo: bytearray) -> None:
    """rtl8192cu/hw.c:98."""
    rtlefuse = rtlpriv.efuse
    for rf_path in range(2):
        for i in range(3):
            if not autoload_fail:
                rtlefuse.eeprom_chnlarea_txpwr_cck[rf_path][i] = hwinfo[C.EEPROM_TXPOWERCCK + rf_path * 3 + i]
                rtlefuse.eeprom_chnlarea_txpwr_ht40_1s[rf_path][i] = \
                    hwinfo[C.EEPROM_TXPOWERHT40_1S + rf_path * 3 + i]
            else:
                rtlefuse.eeprom_chnlarea_txpwr_cck[rf_path][i] = C.EEPROM_DEFAULT_TXPOWERLEVEL
                rtlefuse.eeprom_chnlarea_txpwr_ht40_1s[rf_path][i] = C.EEPROM_DEFAULT_TXPOWERLEVEL
    for i in range(3):
        tempval = hwinfo[C.EEPROM_TXPOWERHT40_2SDIFF + i] if not autoload_fail else C.EEPROM_DEFAULT_HT40_2SDIFF
        rtlefuse.eprom_chnl_txpwr_ht40_2sdf[C.RF90_PATH_A][i] = tempval & 0xF
        rtlefuse.eprom_chnl_txpwr_ht40_2sdf[C.RF90_PATH_B][i] = (tempval & 0xF0) >> 4
    for rf_path in range(2):
        for i in range(14):
            index = rtl92c_get_chnl_group(i)
            rtlefuse.txpwrlevel_cck[rf_path][i] = rtlefuse.eeprom_chnlarea_txpwr_cck[rf_path][index]
            rtlefuse.txpwrlevel_ht40_1s[rf_path][i] = rtlefuse.eeprom_chnlarea_txpwr_ht40_1s[rf_path][index]
            ht40_2s = (rtlefuse.eeprom_chnlarea_txpwr_ht40_1s[rf_path][index]
                       - rtlefuse.eprom_chnl_txpwr_ht40_2sdf[rf_path][index])
            rtlefuse.txpwrlevel_ht40_2s[rf_path][i] = ht40_2s if ht40_2s > 0 else 0
    for i in range(3):
        if not autoload_fail:
            rtlefuse.eeprom_pwrlimit_ht40[i] = hwinfo[C.EEPROM_TXPWR_GROUP + i]
            rtlefuse.eeprom_pwrlimit_ht20[i] = hwinfo[C.EEPROM_TXPWR_GROUP + 3 + i]
        else:
            rtlefuse.eeprom_pwrlimit_ht40[i] = 0
            rtlefuse.eeprom_pwrlimit_ht20[i] = 0
    for rf_path in range(2):
        for i in range(14):
            index = rtl92c_get_chnl_group(i)
            if rf_path == C.RF90_PATH_A:
                rtlefuse.pwrgroup_ht20[rf_path][i] = rtlefuse.eeprom_pwrlimit_ht20[index] & 0xF
                rtlefuse.pwrgroup_ht40[rf_path][i] = rtlefuse.eeprom_pwrlimit_ht40[index] & 0xF
            elif rf_path == C.RF90_PATH_B:
                rtlefuse.pwrgroup_ht20[rf_path][i] = (rtlefuse.eeprom_pwrlimit_ht20[index] & 0xF0) >> 4
                rtlefuse.pwrgroup_ht40[rf_path][i] = (rtlefuse.eeprom_pwrlimit_ht40[index] & 0xF0) >> 4
    for i in range(14):
        index = rtl92c_get_chnl_group(i)
        tempval = hwinfo[C.EEPROM_TXPOWERHT20DIFF + index] if not autoload_fail else C.EEPROM_DEFAULT_HT20_DIFF
        ht20diff_a = tempval & 0xF
        ht20diff_b = (tempval >> 4) & 0xF
        if ht20diff_a & C.BIT(3):
            ht20diff_a |= 0xF0
        if ht20diff_b & C.BIT(3):
            ht20diff_b |= 0xF0
        rtlefuse.txpwr_ht20diff[C.RF90_PATH_A][i] = _s8(ht20diff_a)
        rtlefuse.txpwr_ht20diff[C.RF90_PATH_B][i] = _s8(ht20diff_b)
        tempval = (hwinfo[C.EEPROM_TXPOWER_OFDMDIFF + index] if not autoload_fail
                   else C.EEPROM_DEFAULT_LEGACYHTTXPOWERDIFF)
        rtlefuse.txpwr_legacyhtdiff[C.RF90_PATH_A][i] = tempval & 0xF
        rtlefuse.txpwr_legacyhtdiff[C.RF90_PATH_B][i] = (tempval >> 4) & 0xF
    rtlefuse.legacy_ht_txpowerdiff = rtlefuse.txpwr_legacyhtdiff[C.RF90_PATH_A][7] & 0xFF
    rtlefuse.eeprom_regulatory = hwinfo[C.RF_OPTION1] & 0x7 if not autoload_fail else 0
    if not autoload_fail:
        rtlefuse.eeprom_tssi[C.RF90_PATH_A] = hwinfo[C.EEPROM_TSSI_A]
        rtlefuse.eeprom_tssi[C.RF90_PATH_B] = hwinfo[C.EEPROM_TSSI_B]
    else:
        rtlefuse.eeprom_tssi[C.RF90_PATH_A] = C.EEPROM_DEFAULT_TSSI
        rtlefuse.eeprom_tssi[C.RF90_PATH_B] = C.EEPROM_DEFAULT_TSSI
    tempval = hwinfo[C.EEPROM_THERMAL_METER] if not autoload_fail else C.EEPROM_DEFAULT_THERMALMETER
    rtlefuse.eeprom_thermalmeter = tempval & 0x1F
    if rtlefuse.eeprom_thermalmeter < 0x06 or rtlefuse.eeprom_thermalmeter > 0x1C:
        rtlefuse.eeprom_thermalmeter = 0x12
    if rtlefuse.eeprom_thermalmeter == 0x1F or autoload_fail:
        rtlefuse.apk_thermalmeterignore = True
    rtlefuse.thermalmeter[0] = rtlefuse.eeprom_thermalmeter


def _rtl92cu_read_board_type(rtlpriv: RtlPriv, contents: bytearray) -> None:
    """rtl8192cu/hw.c:300."""
    rtlefuse = rtlpriv.efuse
    if IS_NORMAL_CHIP(rtlpriv.rtlhal.version):
        boardtype = (contents[C.EEPROM_RF_OPT1] & C.BOARD_TYPE_NORMAL_MASK) >> 5
    else:
        boardtype = contents[C.EEPROM_RF_OPT4] & C.BOARD_TYPE_TEST_MASK
    rtlefuse.board_type = boardtype
    if IS_HIGHT_PA(rtlefuse.board_type):
        rtlefuse.external_pa = 1


def _rtl92cu_read_adapter_info(rtlpriv: RtlPriv) -> None:
    """rtl8192cu/hw.c:319."""
    rtlefuse = rtlpriv.efuse
    rtlhal = rtlpriv.rtlhal
    params = [C.RTL8190_EEPROM_ID, C.EEPROM_VID, C.EEPROM_DID, C.EEPROM_SVID, C.EEPROM_SMID,
              C.EEPROM_MAC_ADDR, C.EEPROM_CHANNELPLAN, C.EEPROM_VERSION, C.EEPROM_CUSTOMER_ID, 0]
    hwinfo = bytearray(C.HWSET_MAX_SIZE)

    if rtl_get_hwinfo(rtlpriv, C.HWSET_MAX_SIZE, hwinfo, params):
        return

    _rtl92cu_read_txpower_info_from_hwpg(rtlpriv, rtlefuse.autoload_failflag, hwinfo)
    _rtl92cu_read_board_type(rtlpriv, hwinfo)

    rtlefuse.txpwr_fromeprom = True
    if rtlhal.oem_id == C.RT_CID_DEFAULT:
        if rtlefuse.eeprom_oemid == C.EEPROM_CID_DEFAULT:
            if (rtlefuse.eeprom_did == 0x8176 and rtlefuse.eeprom_svid == 0x103C
                    and rtlefuse.eeprom_smid == 0x1629):
                rtlhal.oem_id = C.RT_CID_819X_HP
            else:
                rtlhal.oem_id = C.RT_CID_DEFAULT
        elif rtlefuse.eeprom_oemid == C.EEPROM_CID_TOSHIBA:
            rtlhal.oem_id = C.RT_CID_TOSHIBA
        elif rtlefuse.eeprom_oemid == C.EEPROM_CID_QMI:
            rtlhal.oem_id = C.RT_CID_819X_QMI
        else:
            rtlhal.oem_id = C.RT_CID_DEFAULT


def _rtl92cu_hal_customized_behavior(rtlpriv: RtlPriv) -> None:
    """rtl8192cu/hw.c:371."""
    if rtlpriv.rtlhal.oem_id == C.RT_CID_819X_HP:
        rtlpriv.ledctl.led_opendrain = True


def rtl92cu_read_eeprom_info(rtlpriv: RtlPriv) -> None:
    """rtl8192cu/hw.c:393. A test chip skips the EFUSE read entirely."""
    rtlefuse = rtlpriv.efuse
    if not IS_NORMAL_CHIP(rtlpriv.rtlhal.version):
        return
    tmp_u1b = rtlpriv.io.read8(C.REG_9346CR)
    rtlefuse.epromtype = C.EEPROM_93C46 if tmp_u1b & C.BOOT_FROM_EEPROM else C.EEPROM_BOOT_EFUSE
    rtlefuse.autoload_failflag = not (tmp_u1b & C.EEPROM_EN)
    _rtl92cu_read_adapter_info(rtlpriv)
    _rtl92cu_hal_customized_behavior(rtlpriv)


def _rtl92cu_phy_param_tab_init(rtlpriv: RtlPriv) -> None:
    """hw.c:24: high-PA boards take the _HP PHY_REG_PG, PHY_REG_1T, RADIOA_1T and AGCTAB_1T tables."""
    tables = rtlpriv.phy.hwparam_tables
    high_pa = IS_HIGHT_PA(rtlpriv.efuse.board_type)
    tables[C.MAC_REG] = table.RTL8192CUMAC_2T_ARRAY
    tables[C.PHY_REG_PG] = table.RTL8192CUPHY_REG_ARRAY_PG_HP if high_pa else table.RTL8192CUPHY_REG_ARRAY_PG
    tables[C.PHY_REG_2T] = table.RTL8192CUPHY_REG_2TARRAY
    tables[C.RADIOA_2T] = table.RTL8192CURADIOA_2TARRAY
    tables[C.RADIOB_2T] = table.RTL8192CU_RADIOB_2TARRAY
    tables[C.AGCTAB_2T] = table.RTL8192CUAGCTAB_2TARRAY
    if high_pa:
        tables[C.PHY_REG_1T] = table.RTL8192CUPHY_REG_1T_HPARRAY
        tables[C.RADIOA_1T] = table.RTL8192CURADIOA_1T_HPARRAY
        tables[C.RADIOB_1T] = table.RTL8192CU_RADIOB_1TARRAY
        tables[C.AGCTAB_1T] = table.RTL8192CUAGCTAB_1T_HPARRAY
    else:
        tables[C.PHY_REG_1T] = table.RTL8192CUPHY_REG_1TARRAY
        tables[C.RADIOA_1T] = table.RTL8192CU_RADIOA_1TARRAY
        tables[C.RADIOB_1T] = table.RTL8192CU_RADIOB_1TARRAY
        tables[C.AGCTAB_1T] = table.RTL8192CUAGCTAB_1TARRAY


def _rtl92cu_init_power_on(rtlpriv: RtlPriv) -> int:
    """hw.c:416."""
    io = rtlpriv.io
    pollingcount = 0
    while True:
        if io.read8(C.REG_APS_FSMCO) & C.PFM_ALDN:
            break
        if pollingcount > 100:
            logger.error("Failed to polling REG_APS_FSMCO[PFM_ALDN] done!")
            return -errno.ENODEV
        pollingcount += 1
    io.write8(C.REG_RSV_CTRL, 0x0)
    io.write8(C.REG_SPS0_CTRL, 0x2b)
    time.sleep(100e-6)
    value8 = io.read8(C.REG_LDOV12D_CTRL)
    if not (value8 & C.LDV12_EN):
        value8 |= C.LDV12_EN
        io.write8(C.REG_LDOV12D_CTRL, value8)
        time.sleep(100e-6)
        value8 = io.read8(C.REG_SYS_ISO_CTRL)
        value8 &= ~C.ISO_MD2PP
        io.write8(C.REG_SYS_ISO_CTRL, value8)
    pollingcount = 0
    value16 = io.read16(C.REG_APS_FSMCO)
    value16 |= C.APFM_ONMAC
    io.write16(C.REG_APS_FSMCO, value16)
    while True:
        if not (io.read16(C.REG_APS_FSMCO) & C.APFM_ONMAC):
            logger.info("MAC auto ON okay!")
            break
        if pollingcount > 1000:
            logger.error("Failed to polling REG_APS_FSMCO[APFM_ONMAC] done!")
            return -errno.ENODEV
        pollingcount += 1
    io.write16(C.REG_APS_FSMCO, 0x0812)
    value16 = io.read16(C.REG_SYS_ISO_CTRL)
    value16 &= ~C.ISO_DIOR
    io.write16(C.REG_SYS_ISO_CTRL, value16)
    pollingcount = 0
    io.write8(C.REG_APSD_CTRL, io.read8(C.REG_APSD_CTRL) & ~C.BIT(6) & 0xFF)
    while True:
        pollingcount += 1
        if not (pollingcount < 200 and io.read8(C.REG_APSD_CTRL) & C.BIT(7)):
            break
    value16 = io.read16(C.REG_CR)
    value16 |= (C.HCI_TXDMA_EN | C.HCI_RXDMA_EN | C.TXDMA_EN | C.RXDMA_EN | C.PROTOCOL_EN
                | C.SCHEDULE_EN | C.MACTXEN | C.MACRXEN | C.ENSEC)
    io.write16(C.REG_CR, value16)
    return 0


def _rtl92cu_init_queue_reserved_page(rtlpriv: RtlPriv, out_ep_num: int, queue_sel: int) -> None:
    """hw.c:491; the remainder pages go to the high queue."""
    io = rtlpriv.io
    ischipn = IS_NORMAL_CHIP(rtlpriv.rtlhal.version)
    outepnum = out_ep_num
    numhq = numlq = numnq = 0
    numpubq = C.CHIP_B_PAGE_NUM_PUBQ if ischipn else C.CHIP_A_PAGE_NUM_PUBQ
    txqpagenum = C.TX_TOTAL_PAGE_NUMBER - numpubq
    txqpageunit = txqpagenum // outepnum
    txqremaininpage = txqpagenum % outepnum
    if queue_sel & C.TX_SELE_HQ:
        numhq = txqpageunit
    if queue_sel & C.TX_SELE_LQ:
        numlq = txqpageunit
    if outepnum > 1 and txqremaininpage:
        numhq += txqremaininpage
    if ischipn:
        if queue_sel & C.TX_SELE_NQ:
            numnq = txqpageunit
        io.write8(C.REG_RQPN_NPQ, C._NPQ(numnq))
    value32 = C._HPQ(numhq) | C._LPQ(numlq) | C._PUBQ(numpubq) | C.LD_RQPN
    io.write32(C.REG_RQPN, value32)


def _rtl92c_init_trx_buffer(rtlpriv: RtlPriv) -> None:
    """hw.c:534."""
    io = rtlpriv.io
    txpktbuf_bndy = C.TX_PAGE_BOUNDARY
    io.write8(C.REG_TXPKTBUF_BCNQ_BDNY, txpktbuf_bndy)
    io.write8(C.REG_TXPKTBUF_MGQ_BDNY, txpktbuf_bndy)
    io.write8(C.REG_TXPKTBUF_WMAC_LBK_BF_HD, txpktbuf_bndy)
    io.write8(C.REG_TRXFF_BNDY, txpktbuf_bndy)
    io.write8(C.REG_TDECTRL + 1, txpktbuf_bndy)
    io.write16(C.REG_TRXFF_BNDY + 2, 0x27FF)
    value8 = C._PSRX(C.RX_PAGE_SIZE_REG_VALUE) | C._PSTX(C.PBP_128)
    io.write8(C.REG_PBP, value8)


def _rtl92c_init_chipn_reg_priority(rtlpriv: RtlPriv, beq: int, bkq: int, viq: int, voq: int,
                                    mgtq: int, hiq: int) -> None:
    """hw.c:550."""
    value16 = rtlpriv.io.read16(C.REG_TRXDMA_CTRL) & 0x7
    value16 |= (C._TXDMA_BEQ_MAP(beq) | C._TXDMA_BKQ_MAP(bkq) | C._TXDMA_VIQ_MAP(viq)
                | C._TXDMA_VOQ_MAP(voq) | C._TXDMA_MGQ_MAP(mgtq) | C._TXDMA_HIQ_MAP(hiq))
    rtlpriv.io.write16(C.REG_TRXDMA_CTRL, value16)


def _rtl92cu_init_chipn_one_out_ep_priority(rtlpriv: RtlPriv, queue_sel: int) -> None:
    """hw.c:563."""
    if queue_sel == C.TX_SELE_HQ:
        value = C.QUEUE_HIGH
    elif queue_sel == C.TX_SELE_LQ:
        value = C.QUEUE_LOW
    elif queue_sel == C.TX_SELE_NQ:
        value = C.QUEUE_NORMAL
    else:
        logger.warning("_rtl92cu_init_chipn_one_out_ep_priority: queue_sel 0x%02x", queue_sel)
        return
    _rtl92c_init_chipn_reg_priority(rtlpriv, value, value, value, value, value, value)
    logger.info("Tx queue select: 0x%02x", queue_sel)


def _rtl92cu_init_chipn_two_out_ep_priority(rtlpriv: RtlPriv, queue_sel: int) -> None:
    """hw.c:587; an unknown selection falls through to HQ|LQ."""
    if queue_sel == (C.TX_SELE_NQ | C.TX_SELE_LQ):
        valuehi, valuelow = C.QUEUE_NORMAL, C.QUEUE_LOW
    elif queue_sel == (C.TX_SELE_HQ | C.TX_SELE_NQ):
        valuehi, valuelow = C.QUEUE_HIGH, C.QUEUE_NORMAL
    else:
        if queue_sel != (C.TX_SELE_HQ | C.TX_SELE_LQ):
            logger.warning("_rtl92cu_init_chipn_two_out_ep_priority: queue_sel 0x%02x", queue_sel)
        valuehi, valuelow = C.QUEUE_HIGH, C.QUEUE_LOW
    beq = valuelow
    bkq = valuelow
    viq = valuehi
    voq = valuehi
    mgtq = valuehi
    hiq = valuehi
    _rtl92c_init_chipn_reg_priority(rtlpriv, beq, bkq, viq, voq, mgtq, hiq)
    logger.info("Tx queue select: 0x%02x", queue_sel)


def _rtl92cu_init_chipn_three_out_ep_priority(rtlpriv: RtlPriv, queue_sel: int) -> None:
    """hw.c:623."""
    beq = C.QUEUE_LOW
    bkq = C.QUEUE_LOW
    viq = C.QUEUE_NORMAL
    voq = C.QUEUE_HIGH
    mgtq = C.QUEUE_HIGH
    hiq = C.QUEUE_HIGH
    _rtl92c_init_chipn_reg_priority(rtlpriv, beq, bkq, viq, voq, mgtq, hiq)
    logger.info("Tx queue select :0x%02x..", queue_sel)


def _rtl92cu_init_chipn_queue_priority(rtlpriv: RtlPriv, out_ep_num: int, queue_sel: int) -> None:
    if out_ep_num == 1:
        _rtl92cu_init_chipn_one_out_ep_priority(rtlpriv, queue_sel)
    elif out_ep_num == 2:
        _rtl92cu_init_chipn_two_out_ep_priority(rtlpriv, queue_sel)
    elif out_ep_num == 3:
        _rtl92cu_init_chipn_three_out_ep_priority(rtlpriv, queue_sel)
    else:
        logger.warning("_rtl92cu_init_chipn_queue_priority: out_ep_num %d", out_ep_num)


def _rtl92cu_init_chipt_queue_priority(rtlpriv: RtlPriv, out_ep_num: int, queue_sel: int) -> None:
    """hw.c:659: the test chip maps queues through REG_TRXDMA_CTRL+1."""
    hq_sele = 0
    if out_ep_num == 2:
        hq_sele = C.HQSEL_VOQ | C.HQSEL_VIQ | C.HQSEL_MGTQ | C.HQSEL_HIQ
    elif out_ep_num == 1:
        if queue_sel == C.TX_SELE_LQ:
            hq_sele = 0
        elif queue_sel == C.TX_SELE_HQ:
            hq_sele = C.HQSEL_VOQ | C.HQSEL_VIQ | C.HQSEL_BEQ | C.HQSEL_BKQ | C.HQSEL_MGTQ | C.HQSEL_HIQ
    else:
        logger.warning("_rtl92cu_init_chipt_queue_priority: out_ep_num %d", out_ep_num)
    rtlpriv.io.write8(C.REG_TRXDMA_CTRL + 1, hq_sele)
    logger.info("Tx queue select :0x%02x..", hq_sele)


def _rtl92cu_init_queue_priority(rtlpriv: RtlPriv, out_ep_num: int, queue_sel: int) -> None:
    if IS_NORMAL_CHIP(rtlpriv.rtlhal.version):
        _rtl92cu_init_chipn_queue_priority(rtlpriv, out_ep_num, queue_sel)
    else:
        _rtl92cu_init_chipt_queue_priority(rtlpriv, out_ep_num, queue_sel)


def _rtl92cu_init_wmac_setting(rtlpriv: RtlPriv) -> None:
    """hw.c:700."""
    io = rtlpriv.io
    set_hw_reg = rtlpriv.cfg.ops.set_hw_reg
    value32 = (C.RCR_APM | C.RCR_AM | C.RCR_ADF | C.RCR_AB | C.RCR_APPFCS | C.RCR_APP_ICV | C.RCR_AMF
               | C.RCR_HTC_LOC_CTRL | C.RCR_APP_MIC | C.RCR_APP_PHYSTS | C.RCR_ACRC32)
    set_hw_reg(rtlpriv, C.HW_VAR_RCR, value32)
    io.write32(C.REG_MAR, 0xFFFFFFFF)
    io.write32(C.REG_MAR + 4, 0xFFFFFFFF)
    set_hw_reg(rtlpriv, C.HW_VAR_MGT_FILTER, 0xFFFF)
    set_hw_reg(rtlpriv, C.HW_VAR_CTRL_FILTER, 0x0)
    set_hw_reg(rtlpriv, C.HW_VAR_DATA_FILTER, 0xFFFF)


def _rtl92cu_init_beacon_parameters(rtlpriv: RtlPriv) -> None:
    """hw.c:727; the source's own TODO calls 0x6404 and 0x660F/0x66FF magic numbers."""
    io = rtlpriv.io
    io.write16(C.REG_BCN_CTRL, 0x1010)
    io.write16(C.REG_TBTT_PROHIBIT, 0x6404)
    io.write8(C.REG_DRVERLYINT, C.DRIVER_EARLY_INT_TIME)
    io.write8(C.REG_BCNDMATIM, C.BCN_DMA_ATIME_INT_TIME)
    if IS_NORMAL_CHIP(rtlpriv.rtlhal.version):
        io.write16(C.REG_BCNTCFG, 0x660F)
    else:
        io.write16(C.REG_BCNTCFG, 0x66FF)


def _rtl92cu_init_mac(rtlpriv: RtlPriv) -> int:
    """hw.c:747."""
    rtlusb = rtlpriv.usb
    boundary = C.TX_PAGE_BOUNDARY
    out_ep_nums = rtlusb.out_ep_nums
    queue_sel = rtlusb.out_queue_sel

    err = _rtl92cu_init_power_on(rtlpriv)
    if err:
        logger.error("Failed to init power on!")
        return err
    if not rtl92c_init_llt_table(rtlpriv, boundary):
        logger.error("Failed to init LLT Table!")
        return -errno.EINVAL
    _rtl92cu_init_queue_reserved_page(rtlpriv, out_ep_nums, queue_sel)
    _rtl92c_init_trx_buffer(rtlpriv)
    _rtl92cu_init_queue_priority(rtlpriv, out_ep_nums, queue_sel)
    rtl92c_init_driver_info_size(rtlpriv, C.RTL92C_DRIVER_INFO_SIZE)
    rtl92c_init_interrupt(rtlpriv)
    rtl92c_init_network_type(rtlpriv)
    _rtl92cu_init_wmac_setting(rtlpriv)
    rtl92c_init_adaptive_ctrl(rtlpriv)
    rtl92c_init_edca(rtlpriv)
    rtl92c_init_rate_fallback(rtlpriv)
    rtl92c_init_retry_function(rtlpriv)
    rtlpriv.cfg.ops.set_bw_mode(rtlpriv, C.NL80211_CHAN_HT20)
    rtl92c_set_min_space(rtlpriv, IS_92C_SERIAL(rtlpriv.rtlhal.version))
    _rtl92cu_init_beacon_parameters(rtlpriv)
    rtl92c_init_ampdu_aggregation(rtlpriv)
    rtl92c_init_beacon_max_error(rtlpriv)
    return err


def rtl92cu_enable_hw_security_config(rtlpriv: RtlPriv) -> None:
    """hw.c:788."""
    if rtlpriv.cfg.mod_params.sw_crypto or rtlpriv.sec.use_sw_sec:
        return
    sec_reg_value = C.SCR_TXENCENABLE | C.SCR_RXDECENABLE
    if rtlpriv.sec.use_defaultkey:
        sec_reg_value |= C.SCR_TXUSEDK
        sec_reg_value |= C.SCR_RXUSEDK
    if IS_NORMAL_CHIP(rtlpriv.rtlhal.version):
        sec_reg_value |= C.SCR_RXBCUSEDK | C.SCR_TXBCUSEDK
    rtlpriv.io.write8(C.REG_CR + 1, 0x02)
    rtlpriv.cfg.ops.set_hw_reg(rtlpriv, C.HW_VAR_WPA_CONFIG, sec_reg_value)


def _rtl92cu_hw_configure(rtlpriv: RtlPriv) -> None:
    """hw.c:816; 0x15 and 0xfe40-0xfe42 are unnamed in the source (loopback and USB interference fixes)."""
    io = rtlpriv.io
    io.write8(C.REG_LDOHCI12_CTRL, 0x0f)
    io.write8(0x15, 0xe9)
    io.write8(C.REG_HWSEQ_CTRL, 0xFF)
    io.write8(0xfe40, 0xe0)
    io.write8(0xfe41, 0x8d)
    io.write8(0xfe42, 0x80)
    rtlpriv.usb.reg_bcn_ctrl_val = 0x18
    io.write8(C.REG_BCN_CTRL, rtlpriv.usb.reg_bcn_ctrl_val & 0xFF)


def _initpabias(rtlpriv: RtlPriv) -> None:
    """hw.c:835: EFUSE byte 0x1FA clears a bit per path that needs the PA current fix."""
    pa_setting = efuse_read_1byte(rtlpriv, 0x1FA)
    version = rtlpriv.rtlhal.version
    if not (pa_setting & C.BIT(0)):
        rtl_set_rfreg(rtlpriv, C.RF90_PATH_A, 0x15, 0x0FFFFF, 0x0F406)
        rtl_set_rfreg(rtlpriv, C.RF90_PATH_A, 0x15, 0x0FFFFF, 0x4F406)
        rtl_set_rfreg(rtlpriv, C.RF90_PATH_A, 0x15, 0x0FFFFF, 0x8F406)
        rtl_set_rfreg(rtlpriv, C.RF90_PATH_A, 0x15, 0x0FFFFF, 0xCF406)
    if not (pa_setting & C.BIT(1)) and IS_NORMAL_CHIP(version) and IS_92C_SERIAL(version):
        rtl_set_rfreg(rtlpriv, C.RF90_PATH_B, 0x15, 0x0FFFFF, 0x0F406)
        rtl_set_rfreg(rtlpriv, C.RF90_PATH_B, 0x15, 0x0FFFFF, 0x4F406)
        rtl_set_rfreg(rtlpriv, C.RF90_PATH_B, 0x15, 0x0FFFFF, 0x8F406)
        rtl_set_rfreg(rtlpriv, C.RF90_PATH_B, 0x15, 0x0FFFFF, 0xCF406)
    if not (pa_setting & C.BIT(4)):
        pa_setting = rtlpriv.io.read8(0x16)
        pa_setting &= 0x0F
        rtlpriv.io.write8(0x16, pa_setting | 0x90)


def rtl92cu_hw_init(rtlpriv: RtlPriv) -> int:
    """hw.c:863. Returns 0, a negative errno from init_mac, or 1 when the firmware download fails."""
    rtlhal = rtlpriv.rtlhal
    rtlphy = rtlpriv.phy
    ppsc = rtlpriv.psc

    rtlhal.fw_ready = False
    rtlhal.hw_type = C.HARDWARE_TYPE_RTL8192CU
    err = _rtl92cu_init_mac(rtlpriv)
    if err:
        logger.error("init mac failed!")
        return err
    err = rtl92c_download_fw(rtlpriv)
    if err:
        logger.warning("Failed to download FW. Init HW without FW now..")
        return 1

    rtlhal.fw_ready = True
    rtlhal.last_hmeboxnum = 0
    _rtl92cu_phy_param_tab_init(rtlpriv)
    rtl92cu_phy_mac_config(rtlpriv)
    rtl92cu_phy_bb_config(rtlpriv)
    rtlphy.rf_mode = C.RF_OP_BY_SW_3WIRE
    rtl92c_phy_rf_config(rtlpriv)
    if IS_VENDOR_UMC_A_CUT(rtlhal.version) and not IS_92C_SERIAL(rtlhal.version):
        rtl_set_rfreg(rtlpriv, C.RF90_PATH_A, C.RF_RX_G1, C.MASKDWORD, 0x30255)
        rtl_set_rfreg(rtlpriv, C.RF90_PATH_A, C.RF_RX_G2, C.MASKDWORD, 0x50a00)
    rtlphy.rfreg_chnlval[0] = rtl_get_rfreg(rtlpriv, C.RF90_PATH_A, C.RF_CHNLBW, C.RFREG_OFFSET_MASK)
    rtlphy.rfreg_chnlval[1] = rtl_get_rfreg(rtlpriv, C.RF90_PATH_B, C.RF_CHNLBW, C.RFREG_OFFSET_MASK)
    rtl92cu_bb_block_on(rtlpriv)
    rtl_cam_reset_all_entry(rtlpriv)
    rtl92cu_enable_hw_security_config(rtlpriv)
    ppsc.rfpwr_state = C.ERFON
    rtlpriv.cfg.ops.set_hw_reg(rtlpriv, C.HW_VAR_ETHER_ADDR, rtlpriv.mac80211.mac_addr)
    if ppsc.rfpwr_state == C.ERFON:
        rtl92c_phy_set_rfpath_switch(rtlpriv, True)
        if rtlphy.iqk_initialized:
            rtl92c_phy_iq_calibrate(rtlpriv, True)
        else:
            rtl92c_phy_iq_calibrate(rtlpriv, False)
            rtlphy.iqk_initialized = True
        rtl92c_dm_check_txpower_tracking(rtlpriv)
        rtl92c_phy_lc_calibrate(rtlpriv)
    _rtl92cu_hw_configure(rtlpriv)
    _initpabias(rtlpriv)
    rtl92c_dm_init(rtlpriv)
    return 0


def _rtl92cu_set_bcn_ctrl_reg(rtlpriv: RtlPriv, set_bits: int, clear_bits: int) -> None:
    """hw.c:1126."""
    rtlusb = rtlpriv.usb
    rtlusb.reg_bcn_ctrl_val |= set_bits
    rtlusb.reg_bcn_ctrl_val &= ~clear_bits
    rtlpriv.io.write8(C.REG_BCN_CTRL, rtlusb.reg_bcn_ctrl_val & 0xFF)


def _rtl92cu_stop_tx_beacon(rtlpriv: RtlPriv) -> None:
    """hw.c:1137."""
    io = rtlpriv.io
    if IS_NORMAL_CHIP(rtlpriv.rtlhal.version):
        tmp1byte = io.read8(C.REG_FWHW_TXQ_CTRL + 2)
        io.write8(C.REG_FWHW_TXQ_CTRL + 2, tmp1byte & ~C.BIT(6) & 0xFF)
        io.write8(C.REG_TBTT_PROHIBIT + 1, 0x64)
        tmp1byte = io.read8(C.REG_TBTT_PROHIBIT + 2)
        tmp1byte &= ~C.BIT(0) & 0xFF
        io.write8(C.REG_TBTT_PROHIBIT + 2, tmp1byte)
    else:
        io.write8(C.REG_TXPAUSE, io.read8(C.REG_TXPAUSE) | C.BIT(6))


def _rtl92cu_enable_bcn_sub_func(rtlpriv: RtlPriv) -> None:
    """hw.c:1177."""
    if IS_NORMAL_CHIP(rtlpriv.rtlhal.version):
        _rtl92cu_set_bcn_ctrl_reg(rtlpriv, 0, C.BIT(1))
    else:
        _rtl92cu_set_bcn_ctrl_reg(rtlpriv, 0, C.BIT(4))


def _rtl92cu_set_media_status(rtlpriv: RtlPriv, type: int) -> int:
    """hw.c:1199, NO LINK and STATION only: the ADHOC/AP beacon paths are not ported."""
    io = rtlpriv.io
    bt_msr = io.read8(C.MSR)
    bt_msr &= 0xfc
    if type in (C.NL80211_IFTYPE_UNSPECIFIED, C.NL80211_IFTYPE_STATION):
        _rtl92cu_stop_tx_beacon(rtlpriv)
        _rtl92cu_enable_bcn_sub_func(rtlpriv)
    else:
        logger.warning("Set HW_VAR_MEDIA_STATUS:No such media status(%x)", type)
    if type == C.NL80211_IFTYPE_UNSPECIFIED:
        bt_msr |= C.MSR_NOLINK
    elif type == C.NL80211_IFTYPE_STATION:
        bt_msr |= C.MSR_INFRA
    else:
        logger.error("Network type %d not supported!", type)
        return 1
    io.write8(C.MSR, bt_msr)
    if (bt_msr & C.MSR_MASK) == C.MSR_AP:
        io.write8(C.REG_BCNTCFG + 1, 0x00)
    else:
        io.write8(C.REG_BCNTCFG + 1, 0x66)
    return 0


def disable_rfafeandresetbb(rtlpriv: RtlPriv) -> None:
    """hw.c:939."""
    io = rtlpriv.io
    erfpath = 0
    io.write8(C.REG_TXPAUSE, 0xFF)
    rtl_set_rfreg(rtlpriv, erfpath, 0x0, C.MASKBYTE0, 0x0)
    value8 = C.APSDOFF
    io.write8(C.REG_APSD_CTRL, value8)
    value8 = C.FEN_USBD | C.FEN_USBA | C.FEN_BB_GLB_RSTN
    io.write8(C.REG_SYS_FUNC_EN, value8)
    value8 &= ~C.FEN_BB_GLB_RSTN
    io.write8(C.REG_SYS_FUNC_EN, value8)


def _resetdigitalprocedure1(rtlpriv: RtlPriv, withouthwsm: bool) -> None:
    """hw.c:963."""
    io = rtlpriv.io
    if rtlpriv.rtlhal.fw_version <= 0x20:
        io.write8(C.REG_MCUFWDL, 0)
        valu16 = io.read16(C.REG_SYS_FUNC_EN)
        io.write16(C.REG_SYS_FUNC_EN, valu16 & ~C.FEN_CPUEN)
        valu16 = io.read16(C.REG_SYS_FUNC_EN) & 0x0FFF
        io.write16(C.REG_SYS_FUNC_EN, valu16 | C.FEN_HWPDN | C.FEN_ELDR)
        valu16 = io.read16(C.REG_SYS_FUNC_EN)
        io.write16(C.REG_SYS_FUNC_EN, valu16 | C.FEN_CPUEN)
    else:
        retry_cnts = 0
        if io.read8(C.REG_MCUFWDL) & C.BIT(1):
            io.write8(C.REG_MCUFWDL, 0)
            io.write8(C.REG_HMETFR + 3, 0x20)
            while True:
                retry_cnts += 1
                if not (retry_cnts - 1 < 100 and C.FEN_CPUEN & io.read16(C.REG_SYS_FUNC_EN)):
                    break
                time.sleep(50e-6)
            if retry_cnts >= 100:
                logger.error("8051 reset failed!.........................")
                io.write8(C.REG_SYS_FUNC_EN + 1, 0x50)
                time.sleep(100e-6)
        io.write8(C.REG_SYS_FUNC_EN + 1, 0x54)
        io.write8(C.REG_MCUFWDL, 0)
    if withouthwsm:
        io.write16(C.REG_SYS_CLKR, 0x70A3)
        io.write8(C.REG_AFE_PLL_CTRL, 0x80)
        io.write16(C.REG_AFE_XTAL_CTRL, 0x880F)
        io.write8(C.REG_SYS_ISO_CTRL, 0xF9)


def _resetdigitalprocedure2(rtlpriv: RtlPriv) -> None:
    """hw.c:1029."""
    rtlpriv.io.write16(C.REG_SYS_CLKR, 0x70A3)
    rtlpriv.io.write8(C.REG_SYS_ISO_CTRL + 1, 0x82)


def _disablegpio(rtlpriv: RtlPriv) -> None:
    """hw.c:1041. Step 2 writes the GPIO_MUXCFG value to REG_GPIO_PIN_CTRL+2, as the source does."""
    io = rtlpriv.io
    io.write16(C.REG_GPIO_PIN_CTRL + 2, 0x0000)
    value32 = io.read32(C.REG_GPIO_PIN_CTRL) & 0xFFFF00FF
    value8 = value32 & 0x000000FF
    value32 |= (value8 << 8) | 0x00FF0000
    io.write32(C.REG_GPIO_PIN_CTRL, value32)
    io.write8(C.REG_GPIO_MUXCFG + 3, 0x00)
    value16 = io.read16(C.REG_GPIO_MUXCFG + 2) & 0xFF0F
    value8 = value16 & 0x000F
    value16 |= (value8 << 4) | 0x0780
    io.write16(C.REG_GPIO_PIN_CTRL + 2, value16)
    io.write16(C.REG_LEDCFG0, 0x8080)


def disable_analog(rtlpriv: RtlPriv, withouthwsm: bool) -> None:
    """hw.c:1071."""
    io = rtlpriv.io
    if withouthwsm:
        io.write8(C.REG_LDOA15_CTRL, 0x04)
        value8 = io.read8(C.REG_LDOV12D_CTRL)
        value8 &= ~C.LDV12_EN & 0xFF
        io.write8(C.REG_LDOV12D_CTRL, value8)
    io.write8(C.REG_SPS0_CTRL, 0x23)
    value16 = C.APDM_HOST | C.AFSM_HSUS | C.PFM_ALDN
    io.write16(C.REG_APS_FSMCO, value16)
    io.write8(C.REG_RSV_CTRL, 0x0E)


def carddisable_hwsm(rtlpriv: RtlPriv) -> None:
    """hw.c:1100."""
    disable_rfafeandresetbb(rtlpriv)
    _resetdigitalprocedure1(rtlpriv, False)
    _disablegpio(rtlpriv)
    disable_analog(rtlpriv, False)


def carddisablewithout_hwsm(rtlpriv: RtlPriv) -> None:
    """hw.c:1112."""
    disable_rfafeandresetbb(rtlpriv)
    _resetdigitalprocedure1(rtlpriv, True)
    _disablegpio(rtlpriv)
    _resetdigitalprocedure2(rtlpriv)
    disable_analog(rtlpriv, True)


def rtl92cu_card_disable(rtlpriv: RtlPriv) -> None:
    """hw.c:1257. led_control writes no register (led.c:63)."""
    rtlpriv.mac80211.link_state = C.MAC80211_NOLINK
    opmode = C.NL80211_IFTYPE_UNSPECIFIED
    _rtl92cu_set_media_status(rtlpriv, opmode)
    rtlpriv.psc.cur_ps_level |= C.RT_RF_OFF_LEVL_HALT_NIC
    if rtlpriv.usb.disablehwsm:
        carddisable_hwsm(rtlpriv)
    else:
        carddisablewithout_hwsm(rtlpriv)
    rtlpriv.phy.iqk_initialized = False


def rtl92cu_set_check_bssid(rtlpriv: RtlPriv, check_bssid: bool) -> None:
    """hw.c:1279."""
    if rtlpriv.psc.rfpwr_state != C.ERFON:
        return
    reg_rcr = rtlpriv.cfg.ops.get_hw_reg(rtlpriv, C.HW_VAR_RCR)
    normal_chip = IS_NORMAL_CHIP(rtlpriv.rtlhal.version)
    if check_bssid:
        if normal_chip:
            reg_rcr |= C.RCR_CBSSID_DATA | C.RCR_CBSSID_BCN
            tmp = C.BIT(4)
        else:
            reg_rcr |= C.RCR_CBSSID
            tmp = C.BIT(4) | C.BIT(5)
        rtlpriv.cfg.ops.set_hw_reg(rtlpriv, C.HW_VAR_RCR, reg_rcr)
        _rtl92cu_set_bcn_ctrl_reg(rtlpriv, 0, tmp)
    else:
        if normal_chip:
            reg_rcr &= ~(C.RCR_CBSSID_DATA | C.RCR_CBSSID_BCN)
            tmp = C.BIT(4)
        else:
            reg_rcr &= ~C.RCR_CBSSID
            tmp = C.BIT(4) | C.BIT(5)
        reg_rcr &= ~(C.RCR_CBSSID_DATA | C.RCR_CBSSID_BCN)
        rtlpriv.cfg.ops.set_hw_reg(rtlpriv, C.HW_VAR_RCR, reg_rcr & 0xFFFFFFFF)
        _rtl92cu_set_bcn_ctrl_reg(rtlpriv, tmp, 0)


def rtl92cu_set_network_type(rtlpriv: RtlPriv, type: int) -> int:
    """hw.c:1322; returns -EOPNOTSUPP for a type _rtl92cu_set_media_status rejects."""
    if _rtl92cu_set_media_status(rtlpriv, type):
        return -errno.EOPNOTSUPP
    if rtlpriv.mac80211.link_state == C.MAC80211_LINKED:
        if type != C.NL80211_IFTYPE_AP:
            rtl92cu_set_check_bssid(rtlpriv, True)
    else:
        rtl92cu_set_check_bssid(rtlpriv, False)
    return 0


def rtl92cu_get_hw_reg(rtlpriv: RtlPriv, variable: int) -> int | bool:
    """hw.c:1403, minus HW_VAR_CORRECT_TSF (STA/IBSS TSF sync)."""
    mac = rtlpriv.mac80211
    ppsc = rtlpriv.psc
    io = rtlpriv.io
    if variable == C.HW_VAR_RCR:
        return mac.rx_conf
    if variable == C.HW_VAR_RF_STATE:
        return ppsc.rfpwr_state
    if variable == C.HW_VAR_FWLPS_RF_ON:
        rfstate = rtlpriv.cfg.ops.get_hw_reg(rtlpriv, C.HW_VAR_RF_STATE)
        if rfstate == C.ERFOFF:
            return True
        val_rcr = io.read32(C.REG_RCR)
        val_rcr &= 0x00070000
        return not val_rcr
    if variable == C.HW_VAR_FW_PSMODE_STATUS:
        return ppsc.fw_current_inpsmode
    if variable == C.HW_VAR_MGT_FILTER:
        return io.read16(C.REG_RXFLTMAP0)
    if variable == C.HW_VAR_CTRL_FILTER:
        return io.read16(C.REG_RXFLTMAP1)
    if variable == C.HW_VAR_DATA_FILTER:
        return io.read16(C.REG_RXFLTMAP2)
    if variable == C.HAL_DEF_WOWLAN:
        return False
    raise NotImplementedError(f"rtl92cu_get_hw_reg variable {variable:#x} is not ported")


def rtl92cu_set_hw_reg(rtlpriv: RtlPriv, variable: int, val: int | bytes) -> None:
    """hw.c:1480: the variables the monitor-mode bring-up, channel switch and watchdog set."""
    io = rtlpriv.io
    mac = rtlpriv.mac80211
    if variable == C.HW_VAR_ETHER_ADDR:
        assert isinstance(val, bytes)
        for idx in range(6):
            io.write8(C.REG_MACID + idx, val[idx])
    elif variable == C.HW_VAR_SIFS:
        assert isinstance(val, bytes)
        io.write8(C.REG_SIFS_CCK + 1, val[0])
        io.write8(C.REG_SIFS_OFDM + 1, val[1])
        io.write8(C.REG_SPEC_SIFS + 1, val[0])
        io.write8(C.REG_MAC_SPEC_SIFS + 1, val[0])
        io.write8(C.REG_R2T_SIFS + 1, val[0])
        io.write8(C.REG_T2T_SIFS + 1, val[0])
    elif variable == C.HW_VAR_SLOT_TIME:
        assert isinstance(val, int)
        io.write8(C.REG_SLOT, val)
        for e_aci in range(C.AC_MAX):
            rtlpriv.cfg.ops.set_hw_reg(rtlpriv, C.HW_VAR_AC_PARAM, e_aci)
    elif variable == C.HW_VAR_AC_PARAM:
        assert isinstance(val, int)
        e_aci = val
        ac = mac.ac[e_aci]
        u4b_ac_param = ac.aifs
        u4b_ac_param |= (ac.cw_min & 0xF) << C.AC_PARAM_ECW_MIN_OFFSET
        u4b_ac_param |= (ac.cw_max & 0xF) << C.AC_PARAM_ECW_MAX_OFFSET
        u4b_ac_param |= ac.tx_op << C.AC_PARAM_TXOP_OFFSET
        if e_aci == C.AC1_BK:
            io.write32(C.REG_EDCA_BK_PARAM, u4b_ac_param)
        elif e_aci == C.AC0_BE:
            io.write32(C.REG_EDCA_BE_PARAM, u4b_ac_param)
        elif e_aci == C.AC2_VI:
            io.write32(C.REG_EDCA_VI_PARAM, u4b_ac_param)
        elif e_aci == C.AC3_VO:
            io.write32(C.REG_EDCA_VO_PARAM, u4b_ac_param)
        else:
            logger.warning("rtl8192cu: invalid aci: %d !", e_aci)
    elif variable == C.HW_VAR_RCR:
        assert isinstance(val, int)
        io.write32(C.REG_RCR, val)
        mac.rx_conf = val
    elif variable == C.HW_VAR_RETRY_LIMIT:
        assert isinstance(val, int)
        io.write16(C.REG_RL, val << C.RETRY_LIMIT_SHORT_SHIFT | val << C.RETRY_LIMIT_LONG_SHIFT)
    elif variable == C.HW_VAR_WPA_CONFIG:
        assert isinstance(val, int)
        io.write8(C.REG_SECCFG, val)
    elif variable == C.HW_VAR_FW_PSMODE_STATUS:
        rtlpriv.psc.fw_current_inpsmode = bool(val)
    elif variable == C.HW_VAR_MGT_FILTER:
        assert isinstance(val, int)
        io.write16(C.REG_RXFLTMAP0, val)
        mac.rx_mgt_filter = val
    elif variable == C.HW_VAR_CTRL_FILTER:
        assert isinstance(val, int)
        io.write16(C.REG_RXFLTMAP1, val)
        mac.rx_ctrl_filter = val
    elif variable == C.HW_VAR_DATA_FILTER:
        assert isinstance(val, int)
        io.write16(C.REG_RXFLTMAP2, val)
        mac.rx_data_filter = val
    else:
        raise NotImplementedError(f"rtl92cu_set_hw_reg variable {variable:#x} is not ported")


def rtl92cu_update_channel_access_setting(rtlpriv: RtlPriv) -> None:
    """hw.c:2063."""
    mac = rtlpriv.mac80211
    rtlpriv.cfg.ops.set_hw_reg(rtlpriv, C.HW_VAR_SLOT_TIME, mac.slot_time)
    sifs_timer = 0x0a0a if not mac.ht_enable else 0x0e0e
    rtlpriv.cfg.ops.set_hw_reg(rtlpriv, C.HW_VAR_SIFS, sifs_timer.to_bytes(2, "little"))
