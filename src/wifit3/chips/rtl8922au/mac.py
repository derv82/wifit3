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
)


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


def mac_pwr_on(t, cv: int) -> None:
    """rtw89_mac_pwr_on -> rtw89_mac_power_switch(on=True): boot-mode handoff, reset the power
    state, run the chip power-on sequence. [SRC] mac.c:1586-1599, 1521-1553.
    The post-sequence efuse reads, scoreboard notify, and AON-intr clear are the next milestone."""
    power_switch_boot_mode(t)
    reset_pwr_state_be(t)
    pwr_on_func(t, cv)
