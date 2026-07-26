"""RTL8922AU MAC helpers, ported from rtw89-7.2 (mac.c, mac_be.c, core.c, rtw8922a.c)."""

import time

from .constants import (
    R_AX_SYS_CFG1, R_BE_SYS_CHIPINFO, R_AX_WLAN_XTAL_SI_CTRL,
    B_AX_CHIP_VER_MASK, B_BE_HW_ID_MASK,
    B_AX_WL_XTAL_SI_ADDR_MASK, B_AX_WL_XTAL_SI_DATA_MASK, B_AX_WL_XTAL_SI_MODE_MASK,
    B_AX_WL_XTAL_SI_BITMASK_MASK, B_AX_WL_XTAL_SI_CMD_POLL,
    XTAL_SI_NORMAL_READ, XTAL_SI_NORMAL_WRITE, XTAL_SI_POLL_ATTEMPTS,
    XTAL_SI_CV, XTAL_SI_ACV_MASK, XTAL_SI_CHIP_ID_L, XTAL_SI_CHIP_ID_H,
    XTAL_SI_PLL, XTAL_SI_PLL_1, XTAL_SI_ANAPAR_WL, XTAL_SI_WL_RFC_S0, XTAL_SI_WL_RFC_S1,
    XTAL_SI_XREF_RF1, XTAL_SI_XREF_RF2, XTAL_SI_SRAM_CTRL, XTAL_SI_SRAM_DIS,
    CHIP_CAV, PWR_POLL_ATTEMPTS,
    R_AX_GPIO_MUXCFG, B_AX_BOOT_MODE, R_AX_SYS_PW_CTRL, B_AX_APFN_ONMAC,
    R_AX_SYS_STATUS1, B_AX_AUTO_WLPON, R_AX_RSV_CTRL, B_AX_R_DIS_PRST,
    R_BE_SYSON_FSM_MON, WLAN_FSM_MASK, WLAN_FSM_SET, WLAN_FSM_STATE_MASK, WLAN_FSM_IDLE,
    R_BE_IC_PWR_STATE, B_BE_WLMAC_PWR_STE_MASK, MAC_AX_MAC_OFF, MAC_AX_MAC_ON, MAC_AX_MAC_LPS,
    R_BE_HCI_OPT_CTRL, B_BE_HAXIDMA_IO_EN, B_BE_HAXIDMA_IO_ST, B_BE_HAXIDMA_BACKUP_RESTORE_ST,
    B_BE_HCI_WLAN_IO_EN, B_BE_HCI_WLAN_IO_ST,
    R_BE_SYS_PW_CTRL, B_BE_EN_WLON, B_BE_APFM_SWLPS, B_BE_APFM_OFFMAC,
    R_BE_WLLPS_CTRL, B_BE_FORCE_LEAVE_LPS,
    B_BE_AFSM_WLSUS_EN, B_BE_AFSM_PCIE_SUS_EN, B_BE_DIS_WLBT_PDNSUSEN_SOPC,
    B_BE_DIS_WLBT_LPSEN_LOPC, B_BE_APDM_HPDN, B_BE_RDY_SYSPWR,
    R_BE_WLRESUME_CTRL, B_BE_LPSROP_CMAC0, B_BE_LPSROP_CMAC1, B_BE_APFN_ONMAC,
    R_BE_AFE_ON_CTRL1, B_BE_REG_CK_MON_CK960M_EN,
    R_BE_ANAPAR_POW_MAC, B_BE_POW_PC_LDO_PORT0, B_BE_POW_PC_LDO_PORT1,
    R_BE_FEN_RST_ENABLE, B_BE_R_SYM_ISO_ADDA_P02PP, B_BE_R_SYM_ISO_ADDA_P12PP,
    B_BE_FEN_BB_IP_RSTN, B_BE_FEN_BBPLAT_RSTB,
    R_BE_PLATFORM_ENABLE, B_BE_PLATFORM_EN,
    R_BE_SYS_ADIE_PAD_PWR_CTRL, B_BE_SYM_PADPDN_WL_RFC1_1P3, B_BE_SYM_PADPDN_WL_RFC0_1P3,
    R_BE_PMC_DBG_CTRL2, B_BE_SYSON_DIS_PMCR_BE_WRMSK,
    R_BE_SYS_ISO_CTRL, B_BE_ISO_EB2CORE, B_BE_PWC_EV2EF_B, B_BE_PWC_EV2EF_S,
    R_BE_DMAC_FUNC_EN, B_BE_MAC_FUNC_EN, B_BE_DMAC_FUNC_EN, B_BE_MPDU_PROC_EN,
    B_BE_WD_RLS_EN, B_BE_DLE_WDE_EN, B_BE_TXPKT_CTRL_EN, B_BE_STA_SCH_EN, B_BE_DLE_PLE_EN,
    B_BE_PKT_BUF_EN, B_BE_DMAC_TBL_EN, B_BE_PKT_IN_EN, B_BE_DLE_CPUIO_EN, B_BE_DISPATCHER_EN,
    B_BE_BBRPT_EN, B_BE_MAC_SEC_EN, B_BE_H_AXIDMA_EN, B_BE_DMAC_MLO_EN, B_BE_PLRLS_EN,
    B_BE_P_AXIDMA_EN, B_BE_DLE_DATACPUIO_EN,
    R_BE_CMAC_SHARE_FUNC_EN, B_BE_CMAC_SHARE_EN, B_BE_RESPBA_EN, B_BE_ADDRSRCH_EN, B_BE_BTCOEX_EN,
    R_BE_CMAC_FUNC_EN, B_BE_CMAC_EN, B_BE_CMAC_TXEN, B_BE_CMAC_RXEN, B_BE_SIGB_EN,
    B_BE_PHYINTF_EN, B_BE_CMAC_DMA_EN, B_BE_PTCLTOP_EN, B_BE_SCHEDULER_EN, B_BE_TMAC_EN,
    B_BE_RMAC_EN, B_BE_TXTIME_EN, B_BE_RESP_PKTCTL_EN,
    R_BE_WL_BT_PWR_CTRL, B_BE_BT_DISN_EN, B_BE_WHOLE_SYS_PWR_STE_MASK, MAC_AX_SYS_ACT,
    R_BE_EFUSE_CTRL, B_BE_EF_ADDR_MASK, B_BE_EF_RDY, R_BE_EFUSE_CTRL_1_V1,
    R_BE_EFUSE_CTRL_2_V1, B_BE_EF_BURST,
    EF_FV_OFSET_BE_V1, EF_CV_MASK, EF_CV_INV,
    EFUSE_SEC_BE_START, EFUSE_SEC_BE_SIZE, EFUSE_SB_CRYP_SEL_ADDR, EFUSE_SB_CRYP_SEL_DEFAULT,
    R_BE_SCOREBOARD, MAC_AX_NOTIFY_TP_MAJOR,
    R_BE_HCI_FUNC_EN, B_BE_HCI_TXDMA_EN, B_BE_HCI_RXDMA_EN,
    R_BE_HAXI_INIT_CFG1, B_BE_DMA_MODE_MASK, S_BE_DMA_MOD_USB, B_BE_STOP_AXI_MST,
    B_BE_TXDMA_EN, B_BE_RXDMA_EN, R_BE_HAXI_DMA_STOP1, B_BE_TX_STOP1_MASK,
    R_BE_DMAC_TABLE_CTRL, B_BE_DMAC_ADDR_MODE,
    R_BE_DMAC_CLK_EN, B_BE_DLE_WDE_CLK_EN, B_BE_DLE_PLE_CLK_EN,
    R_BE_WDE_PKTBUF_CFG, R_BE_PLE_PKTBUF_CFG,
    B_BE_WDE_PAGE_SEL_MASK, B_BE_WDE_START_BOUND_MASK, B_BE_WDE_FREE_PAGE_NUM_MASK,
    B_BE_PLE_PAGE_SEL_MASK, B_BE_PLE_START_BOUND_MASK, B_BE_PLE_FREE_PAGE_NUM_MASK,
    S_AX_WDE_PAGE_SEL_64, S_AX_PLE_PAGE_SEL_128, DLE_BOUND_UNIT,
    R_BE_WDE_QTA0_CFG, R_BE_PLE_QTA0_CFG, B_BE_QTA_MIN_SIZE_MASK, B_BE_QTA_MAX_SIZE_MASK,
    R_AX_WDE_INI_STATUS, R_AX_PLE_INI_STATUS, WDE_MGN_INI_RDY, PLE_MGN_INI_RDY,
    WDE_SIZE3_LNK_PGE_NUM, WDE_SIZE3_SRT_OFST, PLE_SIZE3_LNK_PGE_NUM, PLE_SIZE3_SRT_OFST,
    PLE_QT9, EXT_WDE_MIN_QT_WCPU,
    R_BE_HCI_FC_CTRL, B_BE_HCI_FC_EN, B_BE_HCI_FC_CH12_EN,
    R_BE_CH_PAGE_CTRL, B_BE_PREC_PAGE_CH12_V1_MASK, HFC_H2C_PREC,
    R_BE_FW_AUTO_CAL_DELAY, B_BE_WCPU_FW_DELAY_COUNT_VALID, B_BE_WCPU_FW_DELAY_COUNT_MASK,
    B_BE_WCPU_EN, B_BE_HOLD_AFTER_RESET,
    R_BE_WCPU_FW_CTRL, B_BE_RUN_ENV_MASK, B_BE_WLANCPU_FWDL_EN, B_BE_BBMCU0_FWDL_EN,
    B_BE_WDT_PLT_RST_EN, B_BE_WCPU_ROM_CUT_GET,
    R_BE_DCPU_PLATFORM_ENABLE, B_BE_DCPU_PLATFORM_EN,
    R_BE_UDM0, R_BE_UDM1, R_BE_UDM2,
    R_BE_HALT_H2C_CTRL, R_BE_HALT_C2H_CTRL, R_BE_HALT_H2C, R_BE_HALT_C2H,
    R_BE_BOOT_DBG, R_BE_HISR0, B_BE_HALT_C2H_INT,
    R_BE_SYS_CLK_CTRL, B_BE_CPU_CLK_EN, R_BE_SYS_CFG5,
    B_BE_WDT_WAKE_PCIE_EN, B_BE_WDT_WAKE_USB_EN, R_BE_SECURE_BOOT_MALLOC_INFO,
    R_BE_GPIO_MUXCFG, B_BE_BOOT_MODE, R_BE_BOOT_REASON, B_BE_BOOT_REASON_MASK,
)


def field_replace(val: int, mask: int, field: int) -> int:
    """u32_replace_bits: overwrite `mask`'s field of `val` with `field`."""
    return (val & ~mask & 0xFFFFFFFF) | field_prep(mask, field)


def _shift(mask: int) -> int:
    return (mask & -mask).bit_length() - 1      # trailing-zero count of the mask


def field_prep(mask: int, val: int) -> int:
    """FIELD_PREP: place `val` into `mask`'s field."""
    return (val << _shift(mask)) & mask


def field_get(mask: int, val: int) -> int:
    """u32_get_bits: extract `mask`'s field from `val`."""
    return (val & mask) >> _shift(mask)


def read_xtal_si(t, offset: int) -> int:
    """rtw89_mac_read_xtal_si_ax: indirect crystal-SI read. Writes an address+read command
    to XTAL_SI_CTRL, polls the same register until the command bit clears, returns the data
    byte. [SRC] mac.c:7208-7234."""
    cmd = (field_prep(B_AX_WL_XTAL_SI_ADDR_MASK, offset)
           | field_prep(B_AX_WL_XTAL_SI_MODE_MASK, XTAL_SI_NORMAL_READ)
           | B_AX_WL_XTAL_SI_CMD_POLL)
    t.write32(R_AX_WLAN_XTAL_SI_CTRL, cmd)
    for _ in range(XTAL_SI_POLL_ATTEMPTS):
        val32 = t.read32(R_AX_WLAN_XTAL_SI_CTRL)
        if not (val32 & B_AX_WL_XTAL_SI_CMD_POLL):
            return field_get(B_AX_WL_XTAL_SI_DATA_MASK, val32)
    return 0


def read_chip_ver(t) -> dict:
    """rtw89_read_chip_ver (BE path): read chip version, analog cut, hw id, and analog id.
    [SRC] core.c:7091-7130. The RTL8852A cv-fixup branch is not on the 8922A graph
    (its guard is chip_id == RTL8852A), so it is not ported here."""
    cv = field_get(B_AX_CHIP_VER_MASK, t.read32(R_AX_SYS_CFG1))
    acv = field_get(XTAL_SI_ACV_MASK, read_xtal_si(t, XTAL_SI_CV))
    cid = field_get(B_BE_HW_ID_MASK, t.read32(R_BE_SYS_CHIPINFO))
    aid = read_xtal_si(t, XTAL_SI_CHIP_ID_L) | (read_xtal_si(t, XTAL_SI_CHIP_ID_H) << 8)
    return {"cv": cv, "acv": acv, "cid": cid, "aid": aid}


def write_xtal_si(t, offset: int, val: int, mask: int) -> None:
    """rtw89_mac_write_xtal_si (BE): indirect crystal-SI write. Writes an address+data+bitmask
    command to XTAL_SI_CTRL, then polls until the command bit clears. [SRC] mac_be.c:413-441.
    The BE field positions match the AX ones (mac.c:7179), so the AX masks apply."""
    cmd = (field_prep(B_AX_WL_XTAL_SI_ADDR_MASK, offset)
           | field_prep(B_AX_WL_XTAL_SI_DATA_MASK, val)
           | field_prep(B_AX_WL_XTAL_SI_BITMASK_MASK, mask)
           | field_prep(B_AX_WL_XTAL_SI_MODE_MASK, XTAL_SI_NORMAL_WRITE)
           | B_AX_WL_XTAL_SI_CMD_POLL)
    t.write32(R_AX_WLAN_XTAL_SI_CTRL, cmd)
    for _ in range(XTAL_SI_POLL_ATTEMPTS):
        if not (t.read32(R_AX_WLAN_XTAL_SI_CTRL) & B_AX_WL_XTAL_SI_CMD_POLL):
            return
    # TODO: verify, untested here. Kernel warns "xtal si not ready(W)". [SRC] mac_be.c:428.


def _poll32(t, addr: int, cond, mask: int = None) -> int:
    """read_poll_timeout over rtw89_read32[_mask]: read `addr` (extracting `mask`'s field when
    given) until `cond(val)` holds. The replay feeds the recorded reads, so the loop stops at
    the same count the kernel did. [SRC] linux/iopoll.h read_poll_timeout."""
    for _ in range(PWR_POLL_ATTEMPTS):
        val = t.read32(addr)
        if mask is not None:
            val = field_get(mask, val)
        if cond(val):
            return val
    raise TimeoutError(f"rtl8922au: poll timeout on 0x{addr:04x}")


def power_switch_boot_mode(t) -> None:
    """rtw89_mac_power_switch_boot_mode: on USB, if the boot-ROM handoff bit is set, clear the
    on-mac / auto-wlpon / boot-mode / prst-disable bits. [SRC] mac.c:1480-1495."""
    if not field_get(B_AX_BOOT_MODE, t.read32(R_AX_GPIO_MUXCFG)):
        return
    t.write32_clr(R_AX_SYS_PW_CTRL, B_AX_APFN_ONMAC)
    t.write32_clr(R_AX_SYS_STATUS1, B_AX_AUTO_WLPON)
    t.write32_clr(R_AX_GPIO_MUXCFG, B_AX_BOOT_MODE)
    t.write32_clr(R_AX_RSV_CTRL, B_AX_R_DIS_PRST)


def reset_pwr_state_be(t) -> None:
    """rtw89_mac_reset_pwr_state_be: force the SYSON FSM to WLAN, wait for it idle, then drive
    the MAC to off from whichever power state it is in. [SRC] mac_be.c:474-601.
    The cold-boot capture comes up MAC_ON; the MAC_OFF and MAC_LPS arms are ported untested."""
    val32 = t.read32(R_BE_SYSON_FSM_MON)
    val32 = (val32 & WLAN_FSM_MASK) | WLAN_FSM_SET
    t.write32(R_BE_SYSON_FSM_MON, val32)
    _poll32(t, R_BE_SYSON_FSM_MON, lambda v: v == WLAN_FSM_IDLE, mask=WLAN_FSM_STATE_MASK)

    state = field_get(B_BE_WLMAC_PWR_STE_MASK, t.read32(R_BE_IC_PWR_STATE))
    if state == MAC_AX_MAC_OFF:
        # TODO: verify, untested here. Cold boot came up MAC_ON. [SRC] mac_be.c:493-516.
        t.write32_clr(R_BE_HCI_OPT_CTRL, B_BE_HAXIDMA_IO_EN)
        _poll32(t, R_BE_HCI_OPT_CTRL, lambda v: not v,
                mask=B_BE_HAXIDMA_IO_ST | B_BE_HAXIDMA_BACKUP_RESTORE_ST)
        t.write32_clr(R_BE_HCI_OPT_CTRL, B_BE_HCI_WLAN_IO_EN)
        _poll32(t, R_BE_HCI_OPT_CTRL, lambda v: not v, mask=B_BE_HCI_WLAN_IO_ST)
        t.write32_clr(R_BE_SYS_PW_CTRL, B_BE_EN_WLON)
        t.write32_clr(R_BE_SYS_PW_CTRL, B_BE_APFM_SWLPS)
    elif state == MAC_AX_MAC_ON:
        t.write32_clr(R_BE_HCI_OPT_CTRL, B_BE_HAXIDMA_IO_EN)
        _poll32(t, R_BE_HCI_OPT_CTRL, lambda v: not v,
                mask=B_BE_HAXIDMA_IO_ST | B_BE_HAXIDMA_BACKUP_RESTORE_ST)
        t.write32_clr(R_BE_HCI_OPT_CTRL, B_BE_HCI_WLAN_IO_EN)
        _poll32(t, R_BE_HCI_OPT_CTRL, lambda v: not v, mask=B_BE_HCI_WLAN_IO_ST)
        t.write32_set(R_BE_SYS_PW_CTRL, B_BE_EN_WLON)
        t.write32_set(R_BE_SYS_PW_CTRL, B_BE_APFM_OFFMAC)
        _poll32(t, R_BE_SYS_PW_CTRL, lambda v: v == MAC_AX_MAC_OFF, mask=B_BE_APFM_OFFMAC)
        t.write32_clr(R_BE_SYS_PW_CTRL, B_BE_EN_WLON)
        t.write32_clr(R_BE_SYS_PW_CTRL, B_BE_APFM_SWLPS)
    elif state == MAC_AX_MAC_LPS:
        # TODO: verify, untested here. Cold boot came up MAC_ON. [SRC] mac_be.c:552-597.
        t.write32_clr(R_BE_HCI_OPT_CTRL, B_BE_HAXIDMA_IO_EN)
        _poll32(t, R_BE_HCI_OPT_CTRL, lambda v: not v,
                mask=B_BE_HAXIDMA_IO_ST | B_BE_HAXIDMA_BACKUP_RESTORE_ST)
        t.write32_clr(R_BE_HCI_OPT_CTRL, B_BE_HCI_WLAN_IO_EN)
        _poll32(t, R_BE_HCI_OPT_CTRL, lambda v: not v, mask=B_BE_HCI_WLAN_IO_ST)
        t.write32_set(R_BE_WLLPS_CTRL, B_BE_FORCE_LEAVE_LPS)
        _poll32(t, R_BE_IC_PWR_STATE, lambda v: v == MAC_AX_MAC_ON, mask=B_BE_WLMAC_PWR_STE_MASK)
        t.write32_set(R_BE_SYS_PW_CTRL, B_BE_EN_WLON)
        t.write32_set(R_BE_SYS_PW_CTRL, B_BE_APFM_OFFMAC)
        _poll32(t, R_BE_SYS_PW_CTRL, lambda v: v == MAC_AX_MAC_OFF, mask=B_BE_APFM_OFFMAC)
        t.write32_clr(R_BE_WLLPS_CTRL, B_BE_FORCE_LEAVE_LPS)
        t.write32_clr(R_BE_SYS_PW_CTRL, B_BE_EN_WLON)
        t.write32_clr(R_BE_SYS_PW_CTRL, B_BE_APFM_SWLPS)


def pwr_on_func(t, cv: int) -> None:
    """rtw8922a_pwr_on_func: the 8922A MAC power-on register sequence. [SRC] rtw8922a.c:475-634.
    The two PCIE-only blocks are behind the hci-type test; this card is USB, so they never run."""
    t.write32_clr(R_BE_SYS_PW_CTRL, B_BE_AFSM_WLSUS_EN | B_BE_AFSM_PCIE_SUS_EN)
    t.write32_set(R_BE_SYS_PW_CTRL, B_BE_DIS_WLBT_PDNSUSEN_SOPC)
    t.write32_set(R_BE_WLLPS_CTRL, B_BE_DIS_WLBT_LPSEN_LOPC)
    t.write32_clr(R_BE_SYS_PW_CTRL, B_BE_APDM_HPDN)
    t.write32_clr(R_BE_SYS_PW_CTRL, B_BE_APFM_SWLPS)
    _poll32(t, R_BE_SYS_PW_CTRL, lambda v: v & B_BE_RDY_SYSPWR)

    t.write32_set(R_BE_SYS_PW_CTRL, B_BE_EN_WLON)
    t.write32_set(R_BE_WLRESUME_CTRL, B_BE_LPSROP_CMAC0 | B_BE_LPSROP_CMAC1)
    t.write32_set(R_BE_SYS_PW_CTRL, B_BE_APFN_ONMAC)
    _poll32(t, R_BE_SYS_PW_CTRL, lambda v: not (v & B_BE_APFN_ONMAC))

    t.write32_clr(R_BE_AFE_ON_CTRL1, B_BE_REG_CK_MON_CK960M_EN)
    t.write8_set(R_BE_ANAPAR_POW_MAC, B_BE_POW_PC_LDO_PORT0 | B_BE_POW_PC_LDO_PORT1)
    t.write32_clr(R_BE_FEN_RST_ENABLE, B_BE_R_SYM_ISO_ADDA_P02PP | B_BE_R_SYM_ISO_ADDA_P12PP)
    t.write8_set(R_BE_PLATFORM_ENABLE, B_BE_PLATFORM_EN)

    # TODO: verify, untested here. PCIE-only HAXIDMA enable+poll. [SRC] rtw8922a.c:510-526.

    t.write32_set(R_BE_HCI_OPT_CTRL, B_BE_HCI_WLAN_IO_EN)
    _poll32(t, R_BE_HCI_OPT_CTRL, lambda v: v & B_BE_HCI_WLAN_IO_ST)

    # TODO: verify, untested here. PCIE-only force-ibx clear. [SRC] rtw8922a.c:535-537.

    write_xtal_si(t, XTAL_SI_PLL, 0x02, 0x02)
    write_xtal_si(t, XTAL_SI_PLL, 0x01, 0x01)
    t.write32_set(R_BE_SYS_ADIE_PAD_PWR_CTRL, B_BE_SYM_PADPDN_WL_RFC1_1P3)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x40, 0x40)
    t.write32_set(R_BE_SYS_ADIE_PAD_PWR_CTRL, B_BE_SYM_PADPDN_WL_RFC0_1P3)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x20, 0x20)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x04, 0x04)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x08, 0x08)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x00, 0x10)
    write_xtal_si(t, XTAL_SI_WL_RFC_S0, 0xEB, 0xFF)
    write_xtal_si(t, XTAL_SI_WL_RFC_S1, 0xEB, 0xFF)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x01, 0x01)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x02, 0x02)
    write_xtal_si(t, XTAL_SI_ANAPAR_WL, 0x00, 0x80)
    write_xtal_si(t, XTAL_SI_XREF_RF1, 0x00, 0x40)
    write_xtal_si(t, XTAL_SI_XREF_RF2, 0x00, 0x40)
    write_xtal_si(t, XTAL_SI_PLL_1, 0x40, 0x60)
    write_xtal_si(t, XTAL_SI_SRAM_CTRL, 0x00, XTAL_SI_SRAM_DIS)

    if cv != CHIP_CAV:
        t.write32_set(R_BE_PMC_DBG_CTRL2, B_BE_SYSON_DIS_PMCR_BE_WRMSK)
        t.write32_set(R_BE_SYS_ISO_CTRL, B_BE_ISO_EB2CORE)
        t.write32_clr(R_BE_SYS_ISO_CTRL, B_BE_PWC_EV2EF_B)
        time.sleep(0.001)     # mdelay(1). [SRC] rtw8922a.c:600
        t.write32_clr(R_BE_SYS_ISO_CTRL, B_BE_PWC_EV2EF_S)
        t.write32_clr(R_BE_PMC_DBG_CTRL2, B_BE_SYSON_DIS_PMCR_BE_WRMSK)

    val32 = (B_BE_MAC_FUNC_EN | B_BE_DMAC_FUNC_EN | B_BE_MPDU_PROC_EN
             | B_BE_WD_RLS_EN | B_BE_DLE_WDE_EN | B_BE_TXPKT_CTRL_EN
             | B_BE_STA_SCH_EN | B_BE_DLE_PLE_EN | B_BE_PKT_BUF_EN
             | B_BE_DMAC_TBL_EN | B_BE_PKT_IN_EN | B_BE_DLE_CPUIO_EN
             | B_BE_DISPATCHER_EN | B_BE_BBRPT_EN | B_BE_MAC_SEC_EN
             | B_BE_H_AXIDMA_EN | B_BE_DMAC_MLO_EN | B_BE_PLRLS_EN
             | B_BE_P_AXIDMA_EN | B_BE_DLE_DATACPUIO_EN)
    # TODO: verify, untested here. PCIE adds B_BE_LTR_CTL_EN. [SRC] rtw8922a.c:613-614.
    t.write32_set(R_BE_DMAC_FUNC_EN, val32)

    t.write32_set(R_BE_CMAC_SHARE_FUNC_EN,
                  B_BE_CMAC_SHARE_EN | B_BE_RESPBA_EN | B_BE_ADDRSRCH_EN | B_BE_BTCOEX_EN)
    t.write32_set(R_BE_CMAC_FUNC_EN,
                  B_BE_CMAC_EN | B_BE_CMAC_TXEN | B_BE_CMAC_RXEN | B_BE_SIGB_EN
                  | B_BE_PHYINTF_EN | B_BE_CMAC_DMA_EN | B_BE_PTCLTOP_EN | B_BE_SCHEDULER_EN
                  | B_BE_TMAC_EN | B_BE_RMAC_EN | B_BE_TXTIME_EN | B_BE_RESP_PKTCTL_EN)

    t.write32_set(R_BE_FEN_RST_ENABLE, B_BE_FEN_BB_IP_RSTN | B_BE_FEN_BBPLAT_RSTB)


def cnv_efuse_state(t, idle: bool) -> None:
    """rtw89_cnv_efuse_state_be: toggle the BT-disable bit around an efuse access, waiting for
    the whole-system power state to go active on entry. [SRC] efuse_be.c:143-162."""
    if idle:
        t.write32_set(R_BE_WL_BT_PWR_CTRL, B_BE_BT_DISN_EN)
    else:
        t.write32_clr(R_BE_WL_BT_PWR_CTRL, B_BE_BT_DISN_EN)
        _poll32(t, R_BE_IC_PWR_STATE, lambda v: v == MAC_AX_SYS_ACT,
                mask=B_BE_WHOLE_SYS_PWR_STE_MASK)


def _enable_efuse_pwr_cut_ddv(t, cv: int) -> None:
    """rtw89_enable_efuse_pwr_cut_ddv_be. The aphy patch runs on every 8922A cut except CAV.
    [SRC] efuse_be.c:23-42."""
    aphy_patch = cv != CHIP_CAV      # chip_id is RTL8922A
    t.write8_set(R_BE_PMC_DBG_CTRL2, B_BE_SYSON_DIS_PMCR_BE_WRMSK)
    if aphy_patch:
        t.write16_set(R_BE_SYS_ISO_CTRL, B_BE_PWC_EV2EF_S)
        time.sleep(0.001)            # mdelay(1). [SRC] efuse_be.c:36
        t.write16_set(R_BE_SYS_ISO_CTRL, B_BE_PWC_EV2EF_B)
        t.write16_clr(R_BE_SYS_ISO_CTRL, B_BE_ISO_EB2CORE)
    t.write32_set(R_BE_EFUSE_CTRL_2_V1, B_BE_EF_BURST)


def _disable_efuse_pwr_cut_ddv(t, cv: int) -> None:
    """rtw89_disable_efuse_pwr_cut_ddv_be. [SRC] efuse_be.c:44-62."""
    aphy_patch = cv != CHIP_CAV      # chip_id is RTL8922A
    if aphy_patch:
        t.write16_set(R_BE_SYS_ISO_CTRL, B_BE_ISO_EB2CORE)
        t.write16_clr(R_BE_SYS_ISO_CTRL, B_BE_PWC_EV2EF_B)
        time.sleep(0.001)            # mdelay(1). [SRC] efuse_be.c:56
        t.write16_clr(R_BE_SYS_ISO_CTRL, B_BE_PWC_EV2EF_S)
    t.write8_clr(R_BE_PMC_DBG_CTRL2, B_BE_SYSON_DIS_PMCR_BE_WRMSK)
    t.write32_clr(R_BE_EFUSE_CTRL_2_V1, B_BE_EF_BURST)


def dump_physical_efuse_map(t, cv: int, dump_addr: int, dump_size: int) -> bytes:
    """rtw89_dump_physical_efuse_map_be (dav=false): read `dump_size` bytes of physical efuse
    from `dump_addr` (both 4-aligned) via the DDV controller, bracketed by the state convert
    and power-cut enable/disable. [SRC] efuse_be.c:64-97, 164-193."""
    cnv_efuse_state(t, False)
    _enable_efuse_pwr_cut_ddv(t, cv)
    out = bytearray()
    for addr in range(dump_addr, dump_addr + dump_size, 4):
        efuse_ctl = field_prep(B_BE_EF_ADDR_MASK, addr)
        t.write32(R_BE_EFUSE_CTRL, efuse_ctl & ~B_BE_EF_RDY & 0xFFFFFFFF)
        _poll32(t, R_BE_EFUSE_CTRL, lambda v: v & B_BE_EF_RDY)
        out += t.read32(R_BE_EFUSE_CTRL_1_V1).to_bytes(4, "little")
    _disable_efuse_pwr_cut_ddv(t, cv)
    cnv_efuse_state(t, True)
    return bytes(out)


def efuse_read_ecv(t, cv: int) -> int:
    """rtw89_efuse_read_ecv_be: read the efuse chip-version nibble and adopt it as the cut,
    unless it reads invalid. [SRC] efuse_be.c:516-540. Returns the cv to use for later access."""
    dump_addr = EF_FV_OFSET_BE_V1 & ~0x3
    buff = dump_physical_efuse_map(t, cv, dump_addr, 4)
    ecv = field_get(EF_CV_MASK, buff[EF_FV_OFSET_BE_V1 & 0x3])
    if ecv == EF_CV_INV:
        return cv
    return ecv


def efuse_read_fw_secure(t, cv: int) -> None:
    """rtw89_efuse_read_fw_secure_be: dump the secure-boot selector. [SRC] efuse_be.c:468-514.
    This card reads the default selector (secure boot off), so the MSS parse is skipped."""
    sec_map = dump_physical_efuse_map(t, cv, EFUSE_SEC_BE_START, EFUSE_SEC_BE_SIZE)
    i = EFUSE_SB_CRYP_SEL_ADDR - EFUSE_SEC_BE_START
    sb_cryp_sel = sec_map[i] | (sec_map[i + 1] << 8)
    if sb_cryp_sel == EFUSE_SB_CRYP_SEL_DEFAULT:
        return
    # TODO: verify, untested here. Secure-boot MSS parse (rtw89_efuse_recognize_mss_info_v1);
    # this card reads the default selector, so it never runs. [SRC] efuse_be.c:490-505.


def update_scoreboard(t, val: int) -> None:
    """rtw89_mac_update_scoreboard: notify the BT-coex firmware over the scoreboard register.
    8922A btc_sb has one entry (R_BE_SCOREBOARD). [SRC] mac.c:1506-1519, rtw8922a.c:3300."""
    t.write8(R_BE_SCOREBOARD + 3, val)


def mac_pwr_on(t, cv: int) -> None:
    """rtw89_mac_pwr_on -> rtw89_mac_power_switch(on=True): boot-mode handoff, reset the power
    state, run the chip power-on sequence, then the first-probe efuse reads and coex notify.
    [SRC] mac.c:1586-1599, 1521-1568."""
    power_switch_boot_mode(t)
    reset_pwr_state_be(t)
    pwr_on_func(t, cv)
    # power_switch(on=True) tail. On first probe (RTW89_FLAG_PROBE_DONE unset) the efuse reads
    # run; efuse_read_ecv can update the cut used by the secure-boot dump. [SRC] mac.c:1557-1568.
    cv = efuse_read_ecv(t, cv)
    efuse_read_fw_secure(t, cv)
    update_scoreboard(t, MAC_AX_NOTIFY_TP_MAJOR)
    # rtw89_mac_clr_aon_intr -> clr_aon_intr_be is PCIE-only; a no-op on USB. [SRC] mac_be.c:2064.


def ctrl_hci_dma_trx(t, enable: bool) -> None:
    """rtw89_mac_ctrl_hci_dma_trx: toggle HCI TX+RX DMA on hci_func_en_addr (R_BE_HCI_FUNC_EN
    for the 8922A). [SRC] mac.h:1613-1624, rtw8922a.c:3274."""
    bits = B_BE_HCI_TXDMA_EN | B_BE_HCI_RXDMA_EN
    if enable:
        t.write32_set(R_BE_HCI_FUNC_EN, bits)
    else:
        t.write32_clr(R_BE_HCI_FUNC_EN, bits)


def hci_func_en(t) -> None:
    """rtw89_mac_hci_func_en_be: enable HCI TX+RX DMA. [SRC] mac_be.c:369-373."""
    t.write32_set(R_BE_HCI_FUNC_EN, B_BE_HCI_TXDMA_EN | B_BE_HCI_RXDMA_EN)


def dmac_func_pre_en(t) -> None:
    """rtw89_mac_dmac_func_pre_en_be (USB arm): select USB DMA mode, enable TX/RX DMA and the
    AXI master, release the TX stop channels, set DMAC table addressing. [SRC] mac_be.c:375-410.
    Only the USB and RTL8922A branches are taken here; PCIE/SDIO and the _V1 mask are elsewhere."""
    val = t.read32(R_BE_HAXI_INIT_CFG1)
    val = field_replace(val, B_BE_DMA_MODE_MASK, S_BE_DMA_MOD_USB)
    val = (val & ~B_BE_STOP_AXI_MST & 0xFFFFFFFF) | B_BE_TXDMA_EN | B_BE_RXDMA_EN
    t.write32(R_BE_HAXI_INIT_CFG1, val)
    t.write32_clr(R_BE_HAXI_DMA_STOP1, B_BE_TX_STOP1_MASK)   # chip_id == RTL8922A
    t.write32_set(R_BE_DMAC_TABLE_CTRL, B_BE_DMAC_ADDR_MODE)


def dle_func_en(t, enable: bool) -> None:
    """dle_func_en_be: WDE+PLE function enable on DMAC_FUNC_EN. [SRC] mac_be.c:216-224."""
    bits = B_BE_DLE_WDE_EN | B_BE_DLE_PLE_EN
    if enable:
        t.write32_set(R_BE_DMAC_FUNC_EN, bits)
    else:
        t.write32_clr(R_BE_DMAC_FUNC_EN, bits)


def dle_clk_en(t, enable: bool) -> None:
    """dle_clk_en_be: WDE+PLE clock enable (RTL8922A only). [SRC] mac_be.c:226-237."""
    bits = B_BE_DLE_WDE_CLK_EN | B_BE_DLE_PLE_CLK_EN
    if enable:
        t.write32_set(R_BE_DMAC_CLK_EN, bits)
    else:
        t.write32_clr(R_BE_DMAC_CLK_EN, bits)


def dle_mix_cfg(t) -> None:
    """dle_mix_cfg_be for the QTA_DLFW size config (wde_size3_v1 / ple_size3_v1): WDE 64B pages,
    PLE 128B pages, with the free-page count and start bound. [SRC] mac_be.c:239-295."""
    val = t.read32(R_BE_WDE_PKTBUF_CFG)
    val = field_replace(val, B_BE_WDE_PAGE_SEL_MASK, S_AX_WDE_PAGE_SEL_64)
    val = field_replace(val, B_BE_WDE_START_BOUND_MASK, WDE_SIZE3_SRT_OFST // DLE_BOUND_UNIT)
    val = field_replace(val, B_BE_WDE_FREE_PAGE_NUM_MASK, WDE_SIZE3_LNK_PGE_NUM)
    t.write32(R_BE_WDE_PKTBUF_CFG, val)

    val = t.read32(R_BE_PLE_PKTBUF_CFG)
    val = field_replace(val, B_BE_PLE_PAGE_SEL_MASK, S_AX_PLE_PAGE_SEL_128)
    val = field_replace(val, B_BE_PLE_START_BOUND_MASK, PLE_SIZE3_SRT_OFST // DLE_BOUND_UNIT)
    val = field_replace(val, B_BE_PLE_FREE_PAGE_NUM_MASK, PLE_SIZE3_LNK_PGE_NUM)
    t.write32(R_BE_PLE_PKTBUF_CFG, val)


def _set_quota(t, reg: int, min_v: int, max_v: int) -> None:
    """SET_QUOTA_VAL: pack a (min, max) size pair into a QTAn_CFG register. [SRC] mac_be.c:315-322."""
    t.write32(reg, field_prep(B_BE_QTA_MIN_SIZE_MASK, min_v)
              | field_prep(B_BE_QTA_MAX_SIZE_MASK, max_v))


def dle_quota_cfg(t) -> None:
    """dle_quota_cfg for QTA_DLFW: wde_qt4 (all zero) and ple_qt9, min == max. WDE Q1 takes the
    ext SCC wcpu. 8922A stops PLE before Q13 (snrpt). [SRC] mac.c:2264-2272, mac_be.c:326-367."""
    _set_quota(t, R_BE_WDE_QTA0_CFG + 0 * 4, 0, 0)                                      # hif
    _set_quota(t, R_BE_WDE_QTA0_CFG + 1 * 4, EXT_WDE_MIN_QT_WCPU, EXT_WDE_MIN_QT_WCPU)  # wcpu
    _set_quota(t, R_BE_WDE_QTA0_CFG + 2 * 4, 0, 0)
    _set_quota(t, R_BE_WDE_QTA0_CFG + 3 * 4, 0, 0)                                      # pkt_in
    _set_quota(t, R_BE_WDE_QTA0_CFG + 4 * 4, 0, 0)                                      # cpu_io
    for i, q in enumerate(PLE_QT9):
        _set_quota(t, R_BE_PLE_QTA0_CFG + i * 4, q, q)


def chk_dle_rdy(t, wde_or_ple: bool) -> None:
    """chk_dle_rdy_be: poll WDE (or PLE) init status until the manager-ready bits set.
    [SRC] mac_be.c:297-312."""
    reg = R_AX_WDE_INI_STATUS if wde_or_ple else R_AX_PLE_INI_STATUS
    mask = WDE_MGN_INI_RDY if wde_or_ple else PLE_MGN_INI_RDY
    _poll32(t, reg, lambda v: (v & mask) == mask)


def dle_init(t) -> None:
    """rtw89_mac_dle_init(RTW89_QTA_DLFW): configure the DLE (WDE/PLE) packet-buffer sizes and
    quotas for firmware download, then wait for the managers ready. [SRC] mac.c:2274-2343.
    check_mac_en is a software-flag test (DMAC_FUNC was set in pwr_on), so it emits no wire op."""
    dle_func_en(t, False)
    dle_clk_en(t, True)
    dle_mix_cfg(t)
    dle_quota_cfg(t)
    dle_func_en(t, True)
    chk_dle_rdy(t, True)
    chk_dle_rdy(t, False)


def hfc_func_en(t, en: bool, h2c_en: bool) -> None:
    """hfc_func_en_be: toggle the HCI flow-control enable and the CH12 (H2C) enable.
    [SRC] mac_be.c:202-214."""
    val = t.read32(R_BE_HCI_FC_CTRL)
    val = (val | B_BE_HCI_FC_EN) if en else (val & ~B_BE_HCI_FC_EN & 0xFFFFFFFF)
    val = (val | B_BE_HCI_FC_CH12_EN) if h2c_en else (val & ~B_BE_HCI_FC_CH12_EN & 0xFFFFFFFF)
    t.write32(R_BE_HCI_FC_CTRL, val)


def hfc_h2c_cfg(t) -> None:
    """hfc_h2c_cfg_be: program the H2C (CH12) page precedence. [SRC] mac_be.c:152-160."""
    t.write32(R_BE_CH_PAGE_CTRL, field_prep(B_BE_PREC_PAGE_CH12_V1_MASK, HFC_H2C_PREC))


def hfc_init(t) -> None:
    """rtw89_mac_hfc_init(reset=True, en=False, h2c_en=True): reset the flow-control params
    (software), then take the H2C-only path: disable FC, program the H2C precedence, enable
    the H2C channel. The AC-channel and public-quota setup is skipped by that early return.
    [SRC] mac.c:1194-1246. check_mac_en is a software-flag test, so it emits no wire op."""
    hfc_func_en(t, False, False)
    hfc_h2c_cfg(t)
    hfc_func_en(t, False, True)


def fwdl_preconfig(t) -> None:
    """rtw89_mac_fwdl_preconfig_be: clear the WCPU firmware auto-cal delay before download.
    [SRC] mac_be.c:625-629."""
    t.write32_clr(R_BE_FW_AUTO_CAL_DELAY, B_BE_WCPU_FW_DELAY_COUNT_VALID)
    t.write32_mask(R_BE_FW_AUTO_CAL_DELAY, B_BE_WCPU_FW_DELAY_COUNT_MASK, 0)


def disable_cpu(t) -> None:
    """rtw89_mac_disable_cpu_be: park the WLAN CPU (hold-after-reset toggle), clear the run-env
    and AON debug registers before a fresh firmware download. [SRC] mac_be.c:603-623."""
    t.write32_clr(R_BE_PLATFORM_ENABLE, B_BE_WCPU_EN)
    t.write32_set(R_BE_PLATFORM_ENABLE, B_BE_HOLD_AFTER_RESET)
    t.write32_set(R_BE_PLATFORM_ENABLE, B_BE_WCPU_EN)
    t.write32(R_BE_WCPU_FW_CTRL, t.read32(R_BE_WCPU_FW_CTRL) & B_BE_RUN_ENV_MASK)
    t.write32_set(R_BE_DCPU_PLATFORM_ENABLE, B_BE_DCPU_PLATFORM_EN)   # chip_id == RTL8922A
    t.write32(R_BE_UDM0, 0)
    t.write32(R_BE_HALT_C2H, 0)
    t.write32(R_BE_UDM2, 0)


def set_cpu_en(t, include_bb: bool) -> None:
    """set_cpu_en: arm the WLAN CPU (and, when downloading BB MCU, the BBMCU0) for firmware
    download. [SRC] mac_be.c:631-639."""
    bits = B_BE_WLANCPU_FWDL_EN
    if include_bb:
        bits |= B_BE_BBMCU0_FWDL_EN
    t.write32_set(R_BE_WCPU_FW_CTRL, bits)


def wcpu_on(t, boot_reason: int, dlfw: bool) -> None:
    """wcpu_on: clear the boot/halt mailbox state, enable the CPU clock, set the boot reason,
    and pulse the WLAN CPU out of reset. [SRC] mac_be.c:641-698. The pre-boot AON warnings are
    read-only diagnostics; the reads are reproduced. On the 8922A the HOST_EXIST write is
    skipped; with dlfw the trailing FreeRTOS-ready poll is skipped."""
    t.read32(R_BE_HALT_C2H)
    t.read32(R_BE_UDM1)
    t.read32(R_BE_UDM2)
    t.write32(R_BE_UDM1, 0)
    t.write32(R_BE_UDM2, 0)
    t.write32(R_BE_BOOT_DBG, 0)
    t.write32(R_BE_HALT_H2C, 0)
    t.write32(R_BE_HALT_C2H, 0)
    t.write32(R_BE_HALT_H2C_CTRL, 0)
    t.write32(R_BE_HALT_C2H_CTRL, 0)
    t.read32(R_BE_HISR0)
    t.write32(R_BE_HISR0, B_BE_HALT_C2H_INT)
    t.write32_set(R_BE_SYS_CLK_CTRL, B_BE_CPU_CLK_EN)
    t.write32_clr(R_BE_SYS_CFG5, B_BE_WDT_WAKE_PCIE_EN | B_BE_WDT_WAKE_USB_EN)
    t.write32_clr(R_BE_WCPU_FW_CTRL, B_BE_WDT_PLT_RST_EN | B_BE_WCPU_ROM_CUT_GET)
    t.write32(R_BE_SECURE_BOOT_MALLOC_INFO, 0)
    t.write32_clr(R_BE_GPIO_MUXCFG, B_BE_BOOT_MODE)
    # chip_id == RTL8922A: the non-8922A HOST_EXIST set is skipped. [SRC] mac_be.c:683-684.
    t.write16_mask(R_BE_BOOT_REASON, B_BE_BOOT_REASON_MASK, boot_reason)
    t.write32_clr(R_BE_PLATFORM_ENABLE, B_BE_WCPU_EN)
    t.write32_clr(R_BE_PLATFORM_ENABLE, B_BE_HOLD_AFTER_RESET)
    t.write32_set(R_BE_PLATFORM_ENABLE, B_BE_WCPU_EN)
    # dlfw is True on the download path, so the FreeRTOS-ready poll is skipped. [SRC] mac_be.c:691.


def fwdl_enable_wcpu(t, boot_reason: int, dlfw: bool, include_bb: bool) -> None:
    """rtw89_mac_fwdl_enable_wcpu_be: arm the CPU download bits, then boot the WLAN CPU.
    [SRC] mac_be.c:700-707."""
    set_cpu_en(t, include_bb)
    wcpu_on(t, boot_reason, dlfw)


def fw_download(t) -> None:
    """rtw89_fw_download(RTW89_FW_NORMAL, include_bb=False): disable the CPU and enable it for
    download. [SRC] fw.c:1984-2047, mac.c:4334. The firmware-suit transfer (download_hdr /
    download_main over bulk-OUT) and the ready check are the next milestone."""
    disable_cpu(t)
    fwdl_enable_wcpu(t, 0, True, False)
    # TODO: next milestone. rtw89_fw_download_suit: fwdl_check_path_ready, download_hdr,
    # download_main (bulk-OUT firmware sections), fw_check_rdy. [SRC] fw.c:1948-2025.


def partial_init(t) -> None:
    """rtw89_mac_partial_init(include_bb=False) as reached from rtw89_chip_efuse_info_setup:
    the pre-firmware DMAC bring-up, then the firmware download. [SRC] mac.c:4307-4338.
    include_bb is False here, so chip_bb_preinit is skipped; the USB hci mac_pre_init is a
    no-op on the 8922A. [SRC] usb.c:797-804."""
    ctrl_hci_dma_trx(t, True)
    hci_func_en(t)
    dmac_func_pre_en(t)
    dle_init(t)
    hfc_init(t)
    fwdl_preconfig(t)
    fw_download(t)
