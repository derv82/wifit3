"""Minimal RTL8822C MAC power-on configuration for USB firmware loading."""
from __future__ import annotations

import time
from dataclasses import dataclass

from wifit3.chips.rtw88_base.registers import (
    BIT_BOOT_FSPI_EN,
    BIT_DDMA_EN,
    BIT_DIS_TSF_UDT,
    BIT_EN_BCN_FUNCTION,
    BIT_FEN_BB_GLB_RST,
    BIT_FEN_BB_RSTB,
    BIT_FSPI_EN,
    BIT_LNAON_SEL_EN,
    BIT_LNAON_WLBT_SEL,
    BIT_PAPE_SEL_EN,
    BIT_PAPE_WLBT_SEL,
    BIT_RF_EN,
    BIT_RF_RSTB,
    BIT_RF_SDM_RSTB,
    BIT_WL_PLATFORM_RST,
    BIT_WLRF1_BBRF_EN,
    REG_BCN_CTRL,
    REG_CPU_DMEM_CON,
    REG_CR,
    REG_CR_EXT,
    REG_GPIO_MUXCFG,
    REG_LED_CFG,
    REG_MCUFW_CTRL,
    REG_PAD_CTRL1,
    REG_RF_CTRL,
    REG_RSV_CTRL,
    REG_RXDMA_MODE,
    REG_SYS_CFG1,
    REG_SYS_CFG2,
    REG_SYS_FUNC_EN,
    REG_SYS_STATUS1,
    REG_TXDMA_OFFSET_CHK,
    REG_WLRF1,
)

from .constants import (
    BIT_ACRC32,
    BIT_AICV,
    BIT_DMA_MODE,
    BIT_DROP_DATA_EN,
    BIT_SHIFT_BURST_CNT,
    BIT_EN_QUEUE_RPT,
    BIT_MAC_SEC_EN,
    BIT_SECCAM_CLR,
    BIT_SECCAM_POLLING,
    BIT_SHIFT_BURST_SIZE,
    BIT_TCPOFLD_EN,
    REG_CAMCMD,
    REG_FWHW_TXQ_CTRL,
    REG_LTECOEX_CTRL,
    REG_LTECOEX_WDATA,
    REG_MACID,
    REG_RCR,
    REG_RXFLTMAP1,
    REG_USB_HRPWM,
    REG_USB_USBSTAT,
    USB_BURST_SIZE_2_0_FS,
    USB_BURST_SIZE_2_0_HS,
    USB_BURST_SIZE_3_0,
)
from .firmware import H2cState, fill_h2c_cmd
from .power_seq import card_disable_flow_8822c, card_enable_flow_8822c
from .transport import RTL8822CUTransport

SYS_FUNC_EN_8822C = 0xD8

# rtl8822c_set_usb_suspend_mode H2C (the BT-unknown-device / USB-suspend workaround).
H2C_BT_UNKNOWN_DEVICE_WA = 0xD1          # [SRC include/hal_com_h2c.h:138]
BT_UNKNOWN_DEVICE_WA_FORCE_IB_EN = 1 << 1  # param byte0 bit1 [SRC include/hal_com_h2c.h:758]

# REG_BCN_CTRL bit added by hw_port_enable (BIT_DIS_TSF_UDT / BIT_EN_BCN_FUNCTION are imported).
BIT_P0_EN_RXBCN_RPT = 1 << 2            # [SRC hal/halmac/halmac_bit2.h:43584]

# REG_RXFLTMAP1 BAR / BlockAckReq control-frame filter enabled by HW_VAR_ENABLE_RX_BAR.
BIT_CTRLFLT8EN = 1 << 8                 # [SRC hal/hal_com.c:14608, halmac_bit_8822c.h]


def cut_mask_from_sys_cfg1(chip_version: int) -> int:
    return 1 << (((chip_version >> 12) & 0xF) + 1)


def pre_init_system_cfg(transport: RTL8822CUTransport) -> None:
    """HALMAC ``pre_init_system_cfg_8822c`` USB branch."""
    transport.write8(REG_RSV_CTRL, 0)
    if transport.read8(REG_SYS_CFG2 + 3) == 0x20:
        transport.write8_set(0xFE5B, 1 << 4)

    value = transport.read32(REG_PAD_CTRL1)
    transport.write32(REG_PAD_CTRL1, value | BIT_PAPE_WLBT_SEL | BIT_LNAON_WLBT_SEL)
    transport.write32(REG_LED_CFG, transport.read32(REG_LED_CFG) & ~(BIT_PAPE_SEL_EN | BIT_LNAON_SEL_EN))
    transport.write32(REG_GPIO_MUXCFG, transport.read32(REG_GPIO_MUXCFG) | 0x04)

    transport.write8(REG_SYS_FUNC_EN, transport.read8(REG_SYS_FUNC_EN) & ~(BIT_FEN_BB_RSTB | BIT_FEN_BB_GLB_RST))
    transport.write8(REG_RF_CTRL, transport.read8(REG_RF_CTRL) & ~(BIT_RF_SDM_RSTB | BIT_RF_RSTB | BIT_RF_EN))
    transport.write32(REG_WLRF1, transport.read32(REG_WLRF1) & ~BIT_WLRF1_BBRF_EN)
    if transport.read8(REG_SYS_CFG1 + 2) & 0x10:
        raise IOError("RTL8822CU is in test mode")


def _power_switch(transport: RTL8822CUTransport, on: bool, cut_mask: int) -> int:
    """Port ``mac_pwr_switch_usb_8822c``: read RPWM and FW-alive, detect the power state,
    then run the card enable/disable flow. Returns -EALREADY for an unchanged state, like
    the HALMAC API. [SRC hal/halmac/halmac_88xx/halmac_8822c/halmac_usb_8822c.c:41-87]
    """
    rpwm = transport.read8(REG_USB_HRPWM)
    if transport.read16(REG_MCUFW_CTRL) == 0xC078:      # FW still alive: leave 32K
        transport.write8(REG_USB_HRPWM, (rpwm ^ 0x80) & 0x80)
    is_off = transport.read8(REG_CR) == 0xEA or bool(transport.read8(REG_SYS_STATUS1 + 1) & 1)
    # PWR_UNCHANGE: the C short-circuits ONLY when ON is requested and the chip is already ON.
    # An OFF request while already off is NOT short-circuited. [SRC halmac_usb_8822c.c:61-65]
    if on and not is_off:
        return -114
    if on:
        card_enable_flow_8822c(transport, cut_mask=cut_mask)
        transport.write8_clr(REG_SYS_STATUS1 + 1, 1)
    else:
        card_disable_flow_8822c(transport, cut_mask=cut_mask)
    return 0


# init_system_cfg_8822c REG_CR_EXT+3[27:24] PHY_REQ_DELAY. curr_bw is fixed BW_20 on our path,
# so the else arm's WLAN_PHY_REQ_DELAY is taken; WLAN_PHY_REQ_DELAY_5M(0xE)/_10M(0xA) narrowband
# are out of scope. [SRC halmac_init_8822c.c:163-165, :872-880]
WLAN_PHY_REQ_DELAY = 0x0C
# init_system_cfg_8822c B-cut ANAPAR_MAC_0 LDO_VSEL clear.
# [SRC halmac_reg2.h:6246, halmac_bit2.h:56922-56924, halmac_type.h:566]
REG_ANAPAR_MAC_0 = 0x1018
BITS_LDO_VSEL = 0x3
HALMAC_CHIP_VER_B_CUT = 0x01


def init_system_cfg(transport: RTL8822CUTransport, *, chip_ver: int) -> None:
    """HALMAC ``init_system_cfg_8822c`` for 20 MHz USB operation.
    [SRC hal/halmac/halmac_88xx/halmac_8822c/halmac_init_8822c.c:834-899]"""
    transport.write32_set(REG_CPU_DMEM_CON, BIT_WL_PLATFORM_RST | BIT_DDMA_EN)
    transport.write8_set(REG_SYS_FUNC_EN + 1, SYS_FUNC_EN_8822C)
    transport.write8(REG_CR_EXT + 3, (transport.read8(REG_CR_EXT + 3) & 0xF0) | WLAN_PHY_REQ_DELAY)
    fw_ctrl = transport.read32(REG_MCUFW_CTRL)
    if fw_ctrl & BIT_BOOT_FSPI_EN:
        transport.write32(REG_MCUFW_CTRL, fw_ctrl & ~BIT_BOOT_FSPI_EN)
        transport.write32(REG_GPIO_MUXCFG, transport.read32(REG_GPIO_MUXCFG) & ~BIT_FSPI_EN)
    # B-cut silicon clears ANAPAR_MAC_0 LDO_VSEL; our D-Link AC13U is post B-cut (capture-1 has
    # no 0x1018 write) so this branch stays untaken. [SRC halmac_init_8822c.c:890-894]
    if chip_ver == HALMAC_CHIP_VER_B_CUT:
        transport.write8(REG_ANAPAR_MAC_0, transport.read8(REG_ANAPAR_MAC_0) & ~BITS_LDO_VSEL)


def mac_power_on(transport: RTL8822CUTransport, *, cut_mask: int | None = None,
                 chip_ver: int | None = None) -> None:
    """Port of ``rtw_halmac_poweron``: pre_init_system_cfg, power switch ON (with the
    PWR_UNCHANGE OFF/ON recovery), then init_system_cfg. [SRC hal/hal_halmac.c:2698-2815]"""
    if cut_mask is None or chip_ver is None:
        cfg1 = transport.read32(REG_SYS_CFG1)
        if cut_mask is None:
            cut_mask = cut_mask_from_sys_cfg1(cfg1)
        if chip_ver is None:
            chip_ver = (cfg1 >> 12) & 0xF
    pre_init_system_cfg(transport)
    if _power_switch(transport, True, cut_mask) == -114:
        # PWR_UNCHANGE recovery: power OFF then ON directly. rtw_halmac_poweron does NOT
        # re-run pre_init_system_cfg here. [SRC hal/hal_halmac.c:2797-2798]
        _power_switch(transport, False, cut_mask)
        if _power_switch(transport, True, cut_mask) != 0:
            raise IOError("RTL8822CU power-cycle failed")
    init_system_cfg(transport, chip_ver=chip_ver)


def mac_power_off(transport: RTL8822CUTransport, *, cut_mask: int) -> None:
    """HALMAC ``mac_pwr_switch_usb_8822c`` to OFF: the RPWM / FW-alive preamble then the
    card-disable power flow. Ends cycle 1 of the vendor's two-cycle cold init.
    [SRC hal/halmac/halmac_88xx/halmac_8822c/halmac_usb_8822c.c:67-76]"""
    _power_switch(transport, False, cut_mask)


_REG_MSR = 0x0102
_REG_BCN_CTRL = 0x0550
_REG_FWHW_TXQ_CTRL = 0x0420
_REG_TBTT_PROHIBIT = 0x0540
_REG_DIS_ATIM = 0x05B0
_REG_RCR = 0x0608
_REG_RX_DRVINFO_SZ = 0x060F
_REG_RXFLTMAP0 = 0x06A0
_REG_RXFLTMAP2 = 0x06A4
_REG_RXPSF_CTRL = 0x1610
# RXPSF_CTRL bit fields [SRC halmac_bit2.h:72127-72141]. ERRTHR is the low 3 bits.
_BIT_RXPSF_MHCHKEN = 1 << 5
_BIT_RXPSF_CONT_ERRCHKEN = 1 << 4
_BIT_RXPSF_CCKRST = 1 << 6
_BIT_MASK_RXPSF_ERRTHR = 0x7
# fcs_chk_thr default HALMAC_PSF_FCS_CHK_THR_28=7 [SRC halmac_init_88xx.c:178, halmac_type.h:1844]
_RXPSF_FCS_CHK_THR = 7
_REG_WMAC_OPTION_FUNCTION = 0x07D0
# REG_WMAC_OPTION_FUNCTION_1 (0x07D4) drv-info bits [SRC halmac_bit_8822c.h:20890-20891]
_BIT_WMAC_PHYSTS_SNIF = 1 << 9       # sniffer_en
_BIT_WMAC_PHYSTS_PLCP = 1 << 8       # plcp_hdr_en
_TBTT_HOLD_TIME_STOP_BCN = 0x64      # [SRC include/hal_com.h:316]

# Monitor RCR: BIT_AAP (accept all physical, promiscuous) | BIT_APP_PHYSTS (append PHY status)
# | BIT_APP_FCS (radiotap ndev keeps the FCS). [SRC rtl8822c_ops.c:1166]
_RCR_MONITOR = 0x90000001


def _set_msr(transport: RTL8822CUTransport, mode: int) -> None:
    """Set_MSR for hw_port 0: masked RMW of the media-status port-0 bits[1:0]. [SRC hal_com.c]"""
    transport.write8(_REG_MSR, (transport.read8(_REG_MSR) & ~0x03) | mode)


def _stop_tx_beacon(transport: RTL8822CUTransport) -> None:
    """StopTxBeacon_with_reason (USB, no tx_pause): clear the beacon-queue bit, then load the
    stop-beacon TBTT hold time into 0x540[19:8]. [SRC hal_com.c StopTxBeacon_with_reason]"""
    transport.write8_clr(_REG_FWHW_TXQ_CTRL + 2, 1 << 6)
    transport.write8(_REG_TBTT_PROHIBIT + 1, _TBTT_HOLD_TIME_STOP_BCN & 0xFF)
    hold = transport.read8(_REG_TBTT_PROHIBIT + 2)
    transport.write8(_REG_TBTT_PROHIBIT + 2, (hold & 0xF0) | (_TBTT_HOLD_TIME_STOP_BCN >> 8))


def _set_opmode_port0_station(transport: RTL8822CUTransport) -> None:
    """set_opmode_port0(_HW_STATE_STATION_): the managed-mode opmode the vendor selects before
    dropping to monitor. Reproduces ops 9540-9550. [SRC rtl8822c_ops.c:1196]"""
    transport.read8(_REG_BCN_CTRL)                       # rtw_iface_disable_tsf_update reads BCN_CTRL
    _set_msr(transport, 0x02)                            # STATION
    _stop_tx_beacon(transport)
    transport.write8(_REG_BCN_CTRL, 0x18)                # DIS_TSF_UDT | EN_BCN_FUNCTION
    transport.write8_set(_REG_DIS_ATIM, 1 << 0)          # DIS_ATIM_ROOT


def _cfg_drv_info_sniffer(transport: RTL8822CUTransport) -> None:
    """cfg_drv_info_8822c(HALMAC_DRV_INFO_PHY_SNIFFER): the RX-ignore RMW (sniffer clears the MAC
    header + continuous-FCS checks), RX_DRVINFO_SZ=5, RCR |= APP_PHYSTS, WMAC option sniffer_en,
    then the RCR cache-sync read. Reproduces ops 9555-9562. [SRC halmac_cfg_wmac_8822c.c:29]"""
    # cfg_rx_ignore_8822c on a live R16(RXPSF_CTRL): PHY_SNIFFER cfg has hdr_chk_mask=fcs_chk_mask=0
    # (clear MHCHKEN + CONT_ERRCHKEN) and the halmac init defaults cck_rst_en=0 (clear CCKRST) and
    # fcs_chk_thr=7 (ERRTHR field := 7), neither reprogrammed on our path.
    # [SRC halmac_cfg_wmac_8822c.c:170-198]
    rxpsf = transport.read16(_REG_RXPSF_CTRL)
    rxpsf &= ~(_BIT_RXPSF_MHCHKEN | _BIT_RXPSF_CONT_ERRCHKEN | _BIT_RXPSF_CCKRST)
    rxpsf = (rxpsf & ~_BIT_MASK_RXPSF_ERRTHR) | _RXPSF_FCS_CHK_THR
    transport.write16(_REG_RXPSF_CTRL, rxpsf)
    transport.write8(_REG_RX_DRVINFO_SZ, 5)              # 4B phy status + 1B sniffer info
    transport.write32(_REG_RCR, transport.read32(_REG_RCR) | (1 << 28))    # APP_PHYSTS
    opt = transport.read32(_REG_WMAC_OPTION_FUNCTION + 4)
    transport.write32(_REG_WMAC_OPTION_FUNCTION + 4, (opt & ~0x300) | 0x200)   # sniffer_en (BIT9)
    transport.read32(_REG_RCR)                           # rtw_halmac_config_rx_info RCR cache sync


def _set_opmode_monitor(transport: RTL8822CUTransport) -> None:
    """set_opmode_monitor: the promiscuous RCR, the sniffer drv-info config, the raw-report bit and
    the accept-all RX filter maps. Reproduces ops 9551-9570. [SRC rtl8822c_ops.c:1154]"""
    _set_msr(transport, 0x00)                            # NOLINK
    transport.read32(_REG_RCR)                           # backup->rcr = get_hwreg(RCR)
    transport.write32(_REG_RCR, _RCR_MONITOR)
    _cfg_drv_info_sniffer(transport)
    transport.write8_set(_REG_RX_DRVINFO_SZ, 0x80)       # raw sniffer report format
    transport.read16(_REG_RXFLTMAP0)                     # backup rxfilter0/1/2
    transport.read16(0x06A2)
    transport.read16(_REG_RXFLTMAP2)
    transport.write16(_REG_RXFLTMAP0, 0xFFFF)            # accept all mgmt
    transport.write16(0x06A2, 0xFFFF)                    # accept all ctrl
    transport.write16(_REG_RXFLTMAP2, 0xFFFF)            # accept all data


def enter_monitor_mode(transport: RTL8822CUTransport) -> None:
    """The vendor's one-time opmode / RX-enable block (ops 9540-9570): select STATION opmode, then
    hw_var_set_opmode(MONITOR) = MSR NOLINK + set_opmode_monitor. On the wire this runs right after
    the first channel tune, and the NEXT tune re-latches the BB/RF RX chain. [SRC rtl8822c_ops.c:1458]
    """
    _set_opmode_port0_station(transport)
    _set_opmode_monitor(transport)


def enable_bb_rf(transport: RTL8822CUTransport) -> None:
    """HALMAC ``enable_bb_rf_88xx(..., 1)`` before writing PHY tables."""
    transport.write8_set(REG_SYS_FUNC_EN, BIT_FEN_BB_RSTB | BIT_FEN_BB_GLB_RST)
    transport.write8_set(REG_RF_CTRL, BIT_RF_SDM_RSTB | BIT_RF_RSTB | BIT_RF_EN)
    transport.write32_set(REG_WLRF1, BIT_WLRF1_BBRF_EN)


def set_mac_addr(transport: RTL8822CUTransport, mac6: bytes) -> None:
    """Program the interface MAC into REG_MACID (low 4 bytes as a dword, high 2 as a word).

    Mirrors ``cfg_mac_addr_88xx`` (halmac_cfg_wmac_88xx.c). The MAC hardware HW-ACKs
    frames addressed to this MAC even while in monitor mode, which is what
    ``enter_active_monitor`` uses to impersonate a client STA.
    """
    if len(mac6) != 6:
        raise ValueError(f"RTL8822CU: malformed MAC address {mac6!r}")
    transport.write32(REG_MACID, int.from_bytes(mac6[0:4], "little"))
    transport.write16(REG_MACID + 4, int.from_bytes(mac6[4:6], "little"))


def arm_monitor(transport: RTL8822CUTransport, h2c_state: H2cState, mac6: bytes) -> None:
    """Vendor monitor-interface bring-up tail: the USB-suspend / BT-unknown-device workaround
    H2C, program the interface MAC, enable hw port0 in REG_BCN_CTRL, then enable the RX BAR
    control-frame filter. ``mac6`` is the interface MAC.
    [SRC rtl8822cu_halinit.c:222, hal_intf.c:545-547, rtw_mlme_ext.c:461-477]
    """
    # rtl8822c_set_usb_suspend_mode: param[0] |= FORCE_IB_EN, then rtl8822c_fillh2ccmd(
    # H2C_BT_UNKNOWN_DEVICE_WA, len 1, param). The box index (from LastHMEBoxNum) and the
    # HMETFR free-box poll come from the shared H2C sender, not a fixed box.
    # [SRC rtl8822c_cmd.c:343-350, include/hal_com_h2c.h:758]
    fill_h2c_cmd(transport, h2c_state, H2C_BT_UNKNOWN_DEVICE_WA,
                 bytes([BT_UNKNOWN_DEVICE_WA_FORCE_IB_EN]))

    set_mac_addr(transport, mac6)

    # rtw_hal_hw_port_enable -> HW_VAR_PORT_CFG(1) -> hw_var_hw_port_cfg(enable=1) ->
    # hw_bcn_ctrl_add(HW_PORT0, EN_RXBCN_RPT | DIS_TSF_UDT | EN_BCN_FUNCTION): OR those bits
    # into REG_BCN_CTRL. [SRC hal_com.c:1112, rtl8822c_ops.c:1527, hw_bcn_ctrl_add:150]
    transport.write8(REG_BCN_CTRL, transport.read8(REG_BCN_CTRL)
                     | BIT_P0_EN_RXBCN_RPT | BIT_DIS_TSF_UDT | BIT_EN_BCN_FUNCTION)

    # init_hw_mlme_ext -> HW_VAR_ENABLE_RX_BAR(TRUE): RXFLTMAP1 |= BIT(8), the BAR /
    # BlockAckReq control-frame filter. Our monitor path always enables it, so the
    # *val == FALSE (clear BIT(8)) branch is never taken. The trailing read mirrors the
    # vendor's RTW_INFO log read-back. [SRC rtw_mlme_ext.c:461-477, hal_com.c:14600-14616]
    transport.write16(REG_RXFLTMAP1, transport.read16(REG_RXFLTMAP1) | BIT_CTRLFLT8EN)
    transport.read16(REG_RXFLTMAP1)


# --- Reserved-page / FIFO / rqpn allocation (USB, 3 bulkout, NORMAL transfer mode, 20 MHz) -------
# One port of set_trx_fifo_info_8822c + pg_num_parser_88xx + rqpn_parser_88xx +
# txdma_queue_mapping_8822c for our fixed path. _init_trx_cfg and _init_h2c both consume it, so the
# page boundaries, queue map, pubq size, RX FIFO boundary and H2C ring addresses are computed here
# once instead of frozen as capture literals. Fixed condition for our path (untaken branches
# omitted): USB 3-bulkout NORMAL 20 MHz. Out of scope: SDIO/PCIE, 2/4 bulkout, LA mode, the
# RX-FIFO-expanding modes, and the DELAY/LOOPBACK transfer modes.

# halmac_init_8822c.c:39-45 reserved-page counts.
_RSVD_PG_DRV_NUM = 16
_RSVD_PG_H2C_EXTRAINFO_NUM = 24
_RSVD_PG_H2C_STATICINFO_NUM = 8
_RSVD_PG_H2CQ_NUM = 8
_RSVD_PG_CPU_INSTRUCTION_NUM = 0
_RSVD_PG_FW_TXBUF_NUM = 4
_RSVD_PG_CSIBUF_NUM = 50
# halmac_8822c_cfg.h:24-25 ; halmac_88xx_cfg.h:24, :35.
_TX_FIFO_SIZE_8822C = 262144
_RX_FIFO_SIZE_8822C = 24576
_TX_PAGE_SIZE_SHIFT_88XX = 7
_C2H_PKT_BUF_88XX = 256
# HALMAC_PG_NUM_3BULKOUT_8822C NORMAL row {hq, nq, lq, exq, gap} [halmac_init_8822c.c:393-401].
_PG_NUM_3BULKOUT_NORMAL_HQ = 64
_PG_NUM_3BULKOUT_NORMAL_NQ = 64
_PG_NUM_3BULKOUT_NORMAL_LQ = 64
_PG_NUM_3BULKOUT_NORMAL_EXQ = 0
_PG_NUM_3BULKOUT_NORMAL_GAP = 1
# HALMAC_DMA_MAPPING_* enum [halmac_type.h:598-601].
_MAP_EXTRA, _MAP_LOW, _MAP_NORMAL, _MAP_HIGH = 0, 1, 2, 3
# halmac_init_8822c.c:49-51 (all 8 REG_CR enables) and :55.
_MAC_TRX_ENABLE = 0xFF
_BLK_DESC_NUM = 0x3
# WMAC_FWPKT_CR en_fwff [halmac_reg_8822c.h:626, halmac_bit2.h:47159].
_REG_WMAC_FWPKT_CR = 0x0601
_BIT_FWEN = 1 << 7


@dataclass(frozen=True)
class _TrxAlloc:
    queue_map: int          # REG_TXDMA_PQ_MAP, composed from the rqpn pq_map
    hq: int
    lq: int
    nq: int
    exq: int
    pubq: int
    rsvd_boundary: int      # == rsvd_drv_addr
    rsvd_csibuf_addr: int
    rsvd_fw_txbuf_addr: int # cur_pg_addr after subtracting the CSIBUF pages
    rxff_bndy: int          # REG_RXFF_BNDY
    h2cq_addr: int          # rsvd_h2cq_addr << TX_PAGE_SIZE_SHIFT_88XX
    h2cq_size: int          # RSVD_PG_H2CQ_NUM << TX_PAGE_SIZE_SHIFT_88XX


def _txdma_queue_map() -> int:
    """rqpn_parser_88xx + txdma_queue_mapping_8822c for HALMAC_RQPN_3BULKOUT_8822C NORMAL:
    VO/VI=NQ, BE/BK=LQ, MG/HI=HQ. Shifts are BIT_SHIFT_TXDMA_*_MAP_8822C.
    [SRC halmac_init_8822c.c:301-321, 612-619 ; halmac_bit_8822c.h:3864-3924]"""
    pq_vo = pq_vi = _MAP_NORMAL
    pq_be = pq_bk = _MAP_LOW
    pq_mg = pq_hi = _MAP_HIGH
    return ((pq_hi << 14) | (pq_mg << 12) | (pq_bk << 10) |
            (pq_be << 8) | (pq_vi << 6) | (pq_vo << 4))


def _compute_trx_alloc() -> _TrxAlloc:
    """set_trx_fifo_info_8822c + pg_num_parser_88xx for USB 3-bulkout NORMAL 20 MHz.
    [SRC halmac_init_8822c.c:749-824 ; halmac_init_88xx.c:826-879]"""
    tx_fifo_pg_num = _TX_FIFO_SIZE_8822C >> _TX_PAGE_SIZE_SHIFT_88XX
    rsvd_pg_num = (_RSVD_PG_DRV_NUM + _RSVD_PG_H2C_EXTRAINFO_NUM +
                   _RSVD_PG_H2C_STATICINFO_NUM + _RSVD_PG_H2CQ_NUM +
                   _RSVD_PG_CPU_INSTRUCTION_NUM + _RSVD_PG_FW_TXBUF_NUM +
                   _RSVD_PG_CSIBUF_NUM)
    if rsvd_pg_num > tx_fifo_pg_num:
        raise IOError("RTL8822CU rsvd page count exceeds TX FIFO")
    acq_pg_num = tx_fifo_pg_num - rsvd_pg_num
    rsvd_boundary = tx_fifo_pg_num - rsvd_pg_num

    cur_pg_addr = tx_fifo_pg_num
    cur_pg_addr -= _RSVD_PG_CSIBUF_NUM
    rsvd_csibuf_addr = cur_pg_addr
    cur_pg_addr -= _RSVD_PG_FW_TXBUF_NUM
    rsvd_fw_txbuf_addr = cur_pg_addr
    cur_pg_addr -= _RSVD_PG_CPU_INSTRUCTION_NUM
    cur_pg_addr -= _RSVD_PG_H2CQ_NUM
    rsvd_h2cq_addr = cur_pg_addr
    cur_pg_addr -= _RSVD_PG_H2C_STATICINFO_NUM
    cur_pg_addr -= _RSVD_PG_H2C_EXTRAINFO_NUM
    cur_pg_addr -= _RSVD_PG_DRV_NUM
    rsvd_drv_addr = cur_pg_addr
    if rsvd_boundary != rsvd_drv_addr:
        raise IOError("RTL8822CU rsvd boundary mismatch")

    pubq = (acq_pg_num - _PG_NUM_3BULKOUT_NORMAL_HQ - _PG_NUM_3BULKOUT_NORMAL_LQ -
            _PG_NUM_3BULKOUT_NORMAL_NQ - _PG_NUM_3BULKOUT_NORMAL_EXQ -
            _PG_NUM_3BULKOUT_NORMAL_GAP)

    return _TrxAlloc(
        queue_map=_txdma_queue_map(),
        hq=_PG_NUM_3BULKOUT_NORMAL_HQ,
        lq=_PG_NUM_3BULKOUT_NORMAL_LQ,
        nq=_PG_NUM_3BULKOUT_NORMAL_NQ,
        exq=_PG_NUM_3BULKOUT_NORMAL_EXQ,
        pubq=pubq,
        rsvd_boundary=rsvd_boundary,
        rsvd_csibuf_addr=rsvd_csibuf_addr,
        rsvd_fw_txbuf_addr=rsvd_fw_txbuf_addr,
        rxff_bndy=_RX_FIFO_SIZE_8822C - _C2H_PKT_BUF_88XX - 1,
        h2cq_addr=rsvd_h2cq_addr << _TX_PAGE_SIZE_SHIFT_88XX,
        h2cq_size=_RSVD_PG_H2CQ_NUM << _TX_PAGE_SIZE_SHIFT_88XX,
    )


def _init_trx_cfg(t: RTL8822CUTransport) -> None:
    """init_trx_cfg_8822c: 3-bulkout queue map, FIFO/page allocation, auto-LLT, TRX enable.
    All queue/page values come from _compute_trx_alloc, not frozen literals.
    [SRC halmac_init_8822c.c:527-578, priority_queue_cfg_8822c:624-747]"""
    alloc = _compute_trx_alloc()
    t.write16(0x010C, alloc.queue_map)     # REG_TXDMA_PQ_MAP (txdma_queue_mapping_8822c)
    en_fwff = t.read8(_REG_WMAC_FWPKT_CR) & _BIT_FWEN
    if en_fwff:                            # fwff_is_empty poll dropped: en_fwff=0 at cold boot
        t.write8_clr(_REG_WMAC_FWPKT_CR, _BIT_FWEN)
    t.write8(0x0100, 0x00)                 # REG_CR = 0
    t.write16(0x029C, t.read16(0x02A0))    # REG_FWFF_CTRL = R16(REG_FWFF_PKT_INFO)
    t.write8(0x0100, _MAC_TRX_ENABLE)      # REG_CR = MAC_TRX_ENABLE
    if en_fwff:
        t.write8_set(_REG_WMAC_FWPKT_CR, _BIT_FWEN)
    t.write32(0x1330, 0x80000000)          # REG_H2CQ_CSR BIT31
    t.write16(0x0230, alloc.hq)            # FIFOPAGE_INFO_1 high_queue_pg_num
    t.write16(0x0234, alloc.lq)            # FIFOPAGE_INFO_2 low_queue_pg_num
    t.write16(0x0238, alloc.nq)            # FIFOPAGE_INFO_3 normal_queue_pg_num
    t.write16(0x023C, alloc.exq)           # FIFOPAGE_INFO_4 extra_queue_pg_num
    t.write16(0x0240, alloc.pubq)          # FIFOPAGE_INFO_5 pub_queue_pg_num
    t.write32_set(0x022C, 1 << 31)         # RQPN_CTRL_2 BIT31
    t.write16(0x0204, alloc.rsvd_boundary)      # FIFOPAGE_CTRL_2 rsvd_boundary
    t.write16(0x169C, alloc.rsvd_csibuf_addr)   # WMAC_CSIDMA_CFG rsvd_csibuf_addr
    t.write8_set(0x0422, 1 << 4)           # FWHW_TXQ_CTRL+2 BIT4
    t.write16(0x0424, alloc.rsvd_boundary)      # BCNQ_BDNY_V1 = rsvd_boundary
    t.write16(0x0206, alloc.rsvd_boundary)      # FIFOPAGE_CTRL_2+2 = rsvd_boundary
    t.write16(0x0456, alloc.rsvd_boundary)      # BCNQ1_BDNY_V1 = rsvd_boundary
    t.write32(0x011C, alloc.rxff_bndy)     # RXFF_BNDY = rx_fifo_size - C2H_PKT_BUF - 1
    t.write8(0x0208, (t.read8(0x0208) & ~0xF0) | (_BLK_DESC_NUM << 4))  # AUTO_LLT_V1 BLK_DESC_NUM
    t.write8(0x020B, _BLK_DESC_NUM)        # AUTO_LLT_V1+3 = BLK_DESC_NUM
    t.write8_set(0x020D, 1 << 1)           # TXDMA_OFFSET_CHK+1 BIT1
    t.write8_set(0x0208, 1 << 0)           # AUTO_LLT_V1 BIT_AUTO_INIT_LLT
    for _ in range(1000):
        if not t.read8(0x0208) & 1:        # poll AUTO_INIT_LLT complete
            break
        time.sleep(0.001)
    else:
        raise IOError("RTL8822CU auto-LLT init timed out")
    t.write8(0x0103, 0x00)                 # CR+3 = transfer mode NORMAL


def _init_h2c(t: RTL8822CUTransport) -> None:
    """init_h2c_8822c: point the H2C ring at its reserved pages, then verify the ring free space.
    Ring addresses derive from the FIFO allocation (rsvd_h2cq_addr), not frozen literals.
    [SRC halmac_init_8822c.c:976-1025]"""
    alloc = _compute_trx_alloc()
    base = t.read32(0x0244) & 0xFFFC0000   # REG_H2C_HEAD
    t.write32(0x0244, base | alloc.h2cq_addr)
    base = t.read32(0x024C) & 0xFFFC0000   # REG_H2C_READ_ADDR
    t.write32(0x024C, base | alloc.h2cq_addr)
    base = t.read32(0x0248) & 0xFFFC0000   # REG_H2C_TAIL
    t.write32(0x0248, base | (alloc.h2cq_addr + alloc.h2cq_size))
    t.write8(0x0254, (t.read8(0x0254) & 0xFC) | 0x01)   # REG_H2C_INFO: clear[1:0], head-in-h2cq
    t.write8(0x0254, (t.read8(0x0254) & 0xFB) | 0x04)   # REG_H2C_INFO: h2c-in-h2cq
    t.write8_set(0x020D, 1 << 7)           # TXDMA_OFFSET_CHK+1 drop-overflow
    # get_h2c_buf_free_space_88xx: buf_fs must equal the ring size [halmac_common_88xx.c:678-695].
    hw_wptr = t.read32(0x10D4) & 0x3FFFF   # REG_H2C_PKT_WRITEADDR
    fw_rptr = t.read32(0x10D0) & 0x3FFFF   # REG_H2C_PKT_READADDR
    buf_fs = alloc.h2cq_size - (hw_wptr - fw_rptr) if hw_wptr >= fw_rptr else fw_rptr - hw_wptr
    if buf_fs != alloc.h2cq_size:
        raise IOError("RTL8822CU H2C ring free-space mismatch")


def _init_protocol_cfg(t: RTL8822CUTransport) -> None:
    """init_protocol_cfg_8822c: RTS/AMPDU/BAR, SIFS, rate-fallback (ARFR) tables, fast-EDCA,
    beamforming timeouts, RRSR. [SRC halmac_init_8822c.c:908]"""
    t.write8_set(0x0420, 1 << 7)           # TXQ_CTRL BIT7 (en_bcn_area)
    t.write8(0x0421, 0x1F)                 # TXQ_RPT_EN
    t.write16(0x063E, 0x0E0E)              # RESP_SIFS_OFDM
    t.write16(0x0428, 0x100A)              # SPEC_SIFS
    t.write32(0x0514, 0x100A0E0A)          # SIFS
    t.write16(0x063C, 0x0A0A)              # RESP_SIFS_CCK
    t.write32(0x0430, 0x01000000)          # DARFRC
    t.write32(0x0434, 0x08070504)          # DARFRCH
    t.write32(0x043C, 0x08070504)          # RARFRCH
    t.write32(0x0444, 0xFE01F010)          # ARFR0
    t.write32(0x0448, 0x40000000)          # ARFRH0
    t.write32(0x044C, 0x003FF010)          # ARFR1_V1
    t.write32(0x0450, 0x40000000)          # ARFRH1_V1
    t.write32(0x049C, 0x0600F010)          # ARFR4
    t.write32(0x04A0, 0x400003E0)          # ARFRH4
    t.write32(0x04A4, 0x0600F015)          # ARFR5
    t.write32(0x04A8, 0x000000E0)          # ARFRH5
    t.write8(0x0455, 0x70)                 # AMPDU_MAX_TIME_V1
    t.write8_set(0x045E, 1 << 2)           # TX_HANG_CTRL EN_EOF_V1
    t.write8(0x04E5, 0xE4)                 # PRECNT_CTRL pre_txcnt
    t.write8(0x04E6, 0x09)                 # PRECNT_CTRL EN_PRECNT
    t.write32(0x04C8, 0x203F08FF)          # PROT_MODE_CTRL
    t.write16(0x04CE, 0x0801)              # BAR_MODE_CTRL+2
    t.write8(0x1448, 0x06)                 # FAST_EDCA VO
    t.write8(0x144A, 0x06)                 # FAST_EDCA VI
    t.write8(0x144C, 0x06)                 # FAST_EDCA BE
    t.write8(0x144E, 0x06)                 # FAST_EDCA BK
    t.write8_clr(0x0426, 1 << 5)           # LIFETIME_EN clear BIT5
    t.write32(0x1428, (t.read32(0x1428) & ~(1 << 29)) | (1 << 28))  # BF0: -UPDATE_EN +TIMER_EN
    t.write32(0x142C, (t.read32(0x142C) & ~(1 << 29)) | (1 << 28))  # BF1: -UPDATE_EN +TIMER_EN
    t.write32_clr(0x1430, 0x03)            # BF_TIMEOUT clear BF0/BF1_TIMEOUT_EN
    t.write32_clr(0x0440, 0x00600000)      # RRSR clear RSC
    t.write8_set(0x0480, 1 << 5)           # INIRTS_RATE_SEL


def _init_edca_cfg(t: RTL8822CUTransport) -> None:
    """init_edca_cfg_8822c: slot/PIFS/TBTT, EDCA VO/VI/BE/BK, NAV, MAC clock, beacon timing.
    [SRC halmac_init_8822c.c:1034]"""
    t.write8(0x051B, 0x09)                 # SLOT
    t.write8(0x0512, 0x1C)                 # PIFS
    t.write32(0x0540, 0x00006404)          # TBTT_PROHIBIT
    t.write32(0x0500, 0x002FA226)          # EDCA_VO_PARAM
    t.write32(0x0504, 0x005EA328)          # EDCA_VI_PARAM
    t.write32(0x0508, 0x005EA42B)          # EDCA_BE_PARAM
    t.write32(0x050C, 0x0000A44F)          # EDCA_BK_PARAM
    t.write8_clr(0x0521, 1 << 4)           # TX_PTCL_CTRL+1 clear BIT4
    t.write8_set(0x0525, 0x07)             # RD_CTRL+1 |= BIT0|1|2
    # cfg_mac_clk_88xx [:728]: clear BIT20|BIT21 of REG_AFE_CTRL1(0x0024), then the else/BW20 arm
    # ORs MAC_CLK_HW_DEF_80M(0) << BIT_SHIFT_MAC_CLK_SEL(20) = 0 (BW_5/BW_10 narrowband out of scope).
    afe = t.read32(0x0024) & ~((1 << 20) | (1 << 21))    # [SRC halmac_cfg_wmac_88xx.c:728]
    afe |= 0 << 20                         # MAC_CLK_HW_DEF_80M << BIT_SHIFT_MAC_CLK_SEL [:740]
    t.write32(0x0024, afe)
    t.write8(0x055C, 0x50)                 # REG_USTIME_TSF = MAC_CLK_SPEED (80) [:742]
    t.write8(0x0638, 0x50)                 # REG_USTIME_EDCA = MAC_CLK_SPEED (80) [:743]
    t.write8_set(0x0577, 0x0B)             # MISC_CTRL |= BIT3|1|0
    t.write8_clr(0x05B4, 0x70)             # TIMER0_SRC_SEL clear [6:4]
    t.write16(0x0522, 0x0000)              # TXPAUSE = 0
    t.write32(0x0544, 0x001B0005)          # RD_NAV_NXT
    t.write16(0x055E, 0x3030)              # RXTSF_OFFSET_CCK
    t.write8_set(0x0550, 1 << 3)           # BCN_CTRL EN_BCN_FUNCTION
    t.write8(0x0558, 0x04)                 # DRVERLYINT
    t.write8(0x0551, 0x10)                 # BCN_CTRL_CLINT0
    t.write8(0x0559, 0x02)                 # BCNDMATIM
    t.write8(0x055D, 0xFF)                 # BCN_MAX_ERR
    t.write8_set(0x0530, 1 << 0)           # BAR_TX_CTRL


def _init_wmac_cfg(t: RTL8822CUTransport) -> None:
    """init_wmac_cfg_8822c + init_low_pwr_8822c: ACK/EIFS/NAV, MAR, managed RX filter/RCR,
    RX_PKT_LIMIT, RXPSF. [SRC halmac_init_8822c.c:1177, halmac_cfg_wmac_8822c.c:117]"""
    t.write8(0x0640, 0x21)                 # ACKTO
    t.write16(0x0642, 0x0040)              # EIFS
    t.write32(0x0620, 0xFFFFFFFF)          # MAR low
    t.write32(0x0624, 0xFFFFFFFF)          # MAR high
    t.write8(0x06DE, 0x84)                 # BBPSF_CTRL+2 resp txrate
    t.write8(0x0639, 0x6A)                 # ACKTO_CCK
    t.write8(0x0652, 0xC8)                 # NAV_CTRL+2 NAV_MAX
    t.write8_set(0x066C, 1 << 1)           # TRXPTCL_CTL_H EN_TXCTS_IN_RXNAV
    t.write8(0x066E, 0x05)                 # TRXPTCL_CTL_H+2 BAR_ACK_TYPE
    t.write32(0x06A0, 0xFFFFFFFF)          # RXFLTMAP0 + RXFLTMAP1 (managed accept-all)
    t.write16(0x06A4, 0xFFFF)              # RXFLTMAP2
    t.write32(0x0608, 0xE410220E)          # WLAN_RCR_CFG (managed RCR)
    t.write8_set(0x1612, 0x0E)             # RXPSF_CTRL+2
    t.write8(0x060C, 0x18)                 # RX_PKT_LIMIT (12288 >> 9)
    t.write8(0x0606, 0x30)                 # TCR+2
    t.write8(0x0605, 0x30)                 # TCR+1
    t.write16(0x1664, t.read16(0x1664) | 0x0300)     # GENERAL_OPTION set bits
    t.write8_set(0x0718, 1 << 6)           # SND_PTCL_CTRL DIS_CHK_VHTSIGB_CRC
    t.write32(0x07D8, 0xB1810041)          # WMAC_OPTION_FUNCTION_2
    t.write8(0x07D4, 0x98)                 # WMAC_OPTION_FUNCTION_1 (NORMAL)
    # init_low_pwr_8822c [:126]: RXGCK FIFO thr = (R16(REG_RXPSF_CTRL+2 = 0x1612) & 0xF00F)
    # | BIT(10)|BIT(8)|BIT(6)|BIT(4) (= 0x0550). [SRC halmac_cfg_wmac_8822c.c:126]
    t.write16(0x1612, (t.read16(0x1612) & 0xF00F) | 0x0550)   # init_low_pwr RXGCK FIFO threshold
    t.write16(0x1610, 0x3F80)              # RXPSF_CTRL invalid-pkt cfg
    t.write32(0x1614, 0xFFFFFFFF)          # RXPSF_TYPE_CTRL


def init_mac_cfg(t: RTL8822CUTransport) -> None:
    """init_mac_cfg_88xx (8822C): the managed-mode MAC init the vendor runs after FW download,
    reproduced byte-for-byte. Monitor mode is a later runtime switch. [SRC halmac_init_88xx.c:518]"""
    _init_trx_cfg(t)
    _init_h2c(t)
    _init_protocol_cfg(t)
    _init_edca_cfg(t)
    _init_wmac_cfg(t)


def sync_rcr(t: RTL8822CUTransport) -> int:
    """``rtw_hal_get_hwreg(HW_VAR_RCR)``: re-read REG_RCR so the driver's cached copy tracks
    the hardware. [SRC rtl8822c_halinit.c:199]"""
    return t.read32(REG_RCR)


def init_usb_cfg(t: RTL8822CUTransport) -> None:
    """``init_usb_cfg_88xx``: RXDMA burst mode and count, a burst size chosen from the negotiated
    USB speed, then TXDMA drop-data. [SRC halmac_usb_88xx.c:39]"""
    value8 = BIT_DMA_MODE | (0x3 << BIT_SHIFT_BURST_CNT)
    if t.read8(REG_SYS_CFG2 + 3) == 0x20:
        value8 |= USB_BURST_SIZE_3_0 << BIT_SHIFT_BURST_SIZE
    elif (t.read8(REG_USB_USBSTAT) & 0x3) == 0x1:
        value8 |= USB_BURST_SIZE_2_0_HS << BIT_SHIFT_BURST_SIZE
    else:
        value8 |= USB_BURST_SIZE_2_0_FS << BIT_SHIFT_BURST_SIZE
    t.write8(REG_RXDMA_MODE, value8)
    t.write16(REG_TXDMA_OFFSET_CHK, t.read16(REG_TXDMA_OFFSET_CHK) | BIT_DROP_DATA_EN)


def init_mac_flow_tail(t: RTL8822CUTransport) -> None:
    """The rtw_hal_init_hal steps right after init_mac_cfg: RCR sync read-back, rts_full_bw,
    then cfg_usb_rx_agg (5 pages; timeout 0xA on USB3 else 0x20).
    [SRC hal_halmac.c:3512+; cfg_usb_rx_agg_88xx halmac_usb_88xx.c:122-146]"""
    sync_rcr(t)
    t.write8_set(0x0480, 1 << 5)           # set_rts_full_bw
    rxdma = t.read8(0x0283)                # cfg_usb_rx_agg: read current agg mode
    pq = t.read8(0x010C)                   # read current TXDMA_PQ_MAP
    # drv_define==0 branch [:122]: R8(REG_SYS_CFG2+3 = 0x00FF)==0x20 -> USB3 (size 5, timeout 0xA),
    # else USB2 (size 5, timeout 0x20). size shared by both arms. [SRC halmac_usb_88xx.c:123-131]
    usb3 = t.read8(0x00FF) == 0x20
    size = 0x05
    timeout = 0x0A if usb3 else 0x20
    t.write32_set(0x0280, 1 << 29)         # RXDMA_AGG_PG_TH EN_PRE_CALC
    t.write8(0x010C, pq | (1 << 2))        # TXDMA_PQ_MAP RXDMA_AGG_EN
    t.write8(0x0283, rxdma & ~(1 << 7))    # USB (not DMA) aggregation mode
    t.write16(0x0280, size | (timeout << 8))  # size | (timeout << BIT_SHIFT_DMA_AGG_TO_V1=8) [:145]


def config_rx_info(t: RTL8822CUTransport) -> None:
    """``cfg_drv_info_8822c(HALMAC_DRV_INFO_PHY_STATUS)`` plus its callee ``cfg_rx_ignore_8822c``:
    the RXPSF-ignore RMW, RX_DRVINFO_SZ=4, RCR |= APP_PHYSTS, the WMAC option drv-info bits, then the
    HW_VAR_RCR read-back. Cycle-2 only. [SRC halmac_cfg_wmac_8822c.c:28-108, :160-201]"""
    # cfg_rx_ignore_8822c on a live R16(RXPSF_CTRL): the PHY_STATUS cfg carries the persistent
    # rx_ignore_info "en" flags (hdr_chk_en=fcs_chk_en=cck_rst_en=0, never reprogrammed on our path)
    # so all three checks clear, and fcs_chk_thr=7 sets the ERRTHR field. [SRC cfg_rx_ignore_8822c:170-198]
    rxpsf = t.read16(_REG_RXPSF_CTRL)
    rxpsf &= ~(_BIT_RXPSF_MHCHKEN | _BIT_RXPSF_CONT_ERRCHKEN | _BIT_RXPSF_CCKRST)
    rxpsf = (rxpsf & ~_BIT_MASK_RXPSF_ERRTHR) | _RXPSF_FCS_CHK_THR
    t.write16(_REG_RXPSF_CTRL, rxpsf)
    t.write8(_REG_RX_DRVINFO_SZ, 4)                 # drv_info_size = 4 (PHY_STATUS arm)
    t.write32_set(_REG_RCR, 1 << 28)                # phy_status_en=1: RCR |= APP_PHYSTS
    # WMAC option: PHY_STATUS has sniffer_en=0, plcp_hdr_en=0, so clear both drv-info bits.
    opt = t.read32(_REG_WMAC_OPTION_FUNCTION + 4)
    t.write32(_REG_WMAC_OPTION_FUNCTION + 4, opt & ~(_BIT_WMAC_PHYSTS_SNIF | _BIT_WMAC_PHYSTS_PLCP))
    sync_rcr(t)


def btcoex_wifionly_hw_config(t: RTL8822CUTransport) -> None:
    """ex_hal8822c_wifi_only_hw_config: hand the antenna to WiFi and park the BT coexistence
    tables, for a board whose EFUSE reports no BT. [SRC hal/btc/halbtc8822cwifionly.c:19]"""
    t.write32(0x0070, (t.read32(0x0070) & ~0xFF000000) | (0x0E << 24))
    t.write32(REG_LTECOEX_WDATA, 0x00007700)        # gnt_wl = 1, gnt_bt = 0
    t.write32(REG_LTECOEX_CTRL, 0xC00F0000 | 0x38)
    t.write32(0x06C0, 0xAAAAAAAA)
    t.write32(0x06C4, 0xAAAAAAAA)


def init_misc(t: RTL8822CUTransport) -> None:
    """rtl8822c_init_misc: the last cold-init step. Clear the security CAM, stop accepting CRC
    and ICV errors, drop control frames, enable the MAC security engine, ask for management-frame
    TX reports and turn on RX TCP checksum offload. [SRC rtl8822c_halinit.c:224]"""
    t.write32(REG_CAMCMD, BIT_SECCAM_POLLING | BIT_SECCAM_CLR)
    t.write32(REG_RCR, t.read32(REG_RCR) & ~(BIT_ACRC32 | BIT_AICV))
    t.write16(REG_RXFLTMAP1, 0x0000)
    t.write16(REG_CR, t.read16(REG_CR) | BIT_MAC_SEC_EN)
    t.write32(REG_FWHW_TXQ_CTRL, t.read32(REG_FWHW_TXQ_CTRL) | BIT_EN_QUEUE_RPT)
    t.write32(REG_RCR, t.read32(REG_RCR) | BIT_TCPOFLD_EN)
