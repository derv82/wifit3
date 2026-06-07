"""RTL8188EUS phydm DIG/AGC/EDCCA init seed (M7) — ``rtl8188e_InitHalDm``.

The hal_init tail after the MISC11 block: ``rtl8188e_InitHalDm`` [SRC] rtl8188e_dm.c:227
= ``dm_InitGPIOSetting`` (USB) + ``rtw_phydm_init`` -> ``odm_dm_init``. This seeds the
DIG/AGC/NHM/EDCCA state the runtime 2 s watchdog later adapts. Only the
register-touching phydm sub-inits land on the wire; the rest are software-only.

Two parts:
  * A deterministic seed — GPIO, the DIG IGI read, the NHM env-monitor thresholds
    (IGI-derived), and the adaptivity MAC-EDCCA setup.
  * ``phydm_search_pwdb_lower_bound`` [SRC] phydm_adaptivity.c:333 — an EDCCA
    pwdb-lower-bound search: disable the LNA, then step the EDCCA L2H/H2L threshold
    while counting EDCCA assertions on the BB debug port (0x908 select / 0xdf4 value),
    looping until the band reads clear or the L2H ceiling (10) is hit, then re-enable
    the LNA and reset the threshold. The loop length is **data-dependent** (it follows
    the debug-port reads), so it is ported as the real algorithm — the replay serves
    the reads and a faithful port reproduces the writes. [WIRE] cap1 ops 1631-1865.

Several SW-state reads (RF-interface/RX-path/CCK/RF-gain) feed phydm structs and change
no chip state; they are reproduced in wire order so the replay stays aligned.
"""
from __future__ import annotations

from . import bb, rf

# --- phydm register addresses ---------------------------------------------
_REG_GPIO_MUXCFG = 0x0040     # dm_InitGPIOSetting
_GPIOSEL_ENBT = 1 << 5        # GPIOSEL_ENBT
_REG_IGI = 0x0C50             # ODM_REG(IGI_A) — DIG cur_ig_value, mask 0x7f
_CCA_CAP = 14                 # phydm NHM threshold base (IGI_2_NHM_TH(igi - CCA_CAP))
# env-monitor (CCX) — 11N regs [SRC] phydm_ccx.c
_REG_CCX = 0x0890             # NHM/CLM/FAHM enable + NHM th[9..10] (bits[31:16])
_REG_CLM_PERIOD = 0x0894
_REG_NHM_TH0_3 = 0x0898
_REG_NHM_TH4_7 = 0x089C
_REG_NHM_TH8 = 0x0E28
# adaptivity / EDCCA
_REG_TX_PTCL = 0x0520         # REG_TX_PTCL_CTRL — MAC ignore-EDCCA (BIT15)
_REG_ECCA_TH = 0x0C4C         # rOFDM0_ECCAThreshold (L2H byte0, H2L byte2)
_DBG_SELECT = 0x0908          # rFPGA1_DebugSelect
_DBG_VALUE = 0x0DF4
_ADAPT_DBG_PORT = 0x208       # adaptivity_dbg_port (11N)
_IGI_BASE = 0x32              # adaptivity->igi_base
_IGI_TARGET = 0x32            # igi_target
_TH_EDCCA_HL_DIFF = 7         # default th_edcca_hl_diff


def init_hal_dm(t) -> None:
    """``rtl8188e_InitHalDm`` — GPIO + the register-touching phydm sub-inits."""
    _init_gpio(t)
    igi = _dig_init(t)
    _env_monitor_init(t, igi)
    _adaptivity_init(t)
    _post_seed_reads(t)


def _init_gpio(t) -> None:
    """``dm_InitGPIOSetting`` [SRC] rtl8188e_dm.c:190 — clear GPIOSEL_ENBT in MUXCFG."""
    v = t.read8(_REG_GPIO_MUXCFG)
    t.write8(_REG_GPIO_MUXCFG, v & ~_GPIOSEL_ENBT)


def _dig_init(t) -> int:
    """Up to ``phydm_dig_init`` [SRC] phydm_dig.c. The RF-interface (0x824) and
    RX-path (0xc04) reads feed phydm common-info; ``phydm_dig_init`` reads the AGC
    cur_ig_value (IGI) from 0xc50; the CCK AFE read (0xa08) feeds the CCK setup.
    Only the IGI read is consumed (it drives the NHM thresholds)."""
    t.read32(0x0824)
    t.read32(0x0C04)
    igi = t.read32(_REG_IGI) & 0x7F
    t.read32(0x0A08)
    return igi


def _env_monitor_init(t, igi: int) -> None:
    """``phydm_env_monitor_init`` [SRC] phydm_ccx.c:2000 = ccx_hw_restart + nhm_init
    + clm_init. NHM thresholds are IGI-derived (NHM_BACKGROUND): th[0] = (igi -
    CCA_CAP) << 1, th[i] = th[0] + 4*i (IGI_2_NHM_TH(x) = x<<1)."""
    # ccx_hw_restart: disable NHM/CLM/FAHM, then toggle the restart bit.
    bb.set_bb_reg(t, _REG_CCX, 0x7, 0x0)
    bb.set_bb_reg(t, _REG_CCX, 1 << 8, 0x0)
    bb.set_bb_reg(t, _REG_CCX, 1 << 8, 0x1)
    # nhm_init -> nhm_set_th_reg.
    th = [(((igi - _CCA_CAP) << 1) + 4 * i) & 0xFF for i in range(11)]
    t.write32(_REG_NHM_TH0_3, th[0] | th[1] << 8 | th[2] << 16 | th[3] << 24)
    t.write32(_REG_NHM_TH4_7, th[4] | th[5] << 8 | th[6] << 16 | th[7] << 24)
    bb.set_bb_reg(t, _REG_NHM_TH8, 0xFF, th[8])
    bb.set_bb_reg(t, _REG_CCX, 0xFFFF0000, th[9] | th[10] << 8)
    # clm_init -> clm_setting(65535): CLM period.
    bb.set_bb_reg(t, _REG_CLM_PERIOD, 0xFFFF, 0xFFFF)


def _adaptivity_init(t) -> None:
    """``phydm_adaptivity_init`` [SRC] phydm_adaptivity.c:479 (register-touching part):
    set the MAC to not ignore EDCCA, then run the pwdb-lower-bound search."""
    bb.set_bb_reg(t, _REG_TX_PTCL, 1 << 15, 0)   # phydm_mac_edcca_state(dont_ignore)
    _search_pwdb_lower_bound(t)


def _search_pwdb_lower_bound(t) -> None:
    """``phydm_search_pwdb_lower_bound`` [SRC] phydm_adaptivity.c:333."""
    _set_lna(t, enable=False)
    # th_l2h_dmc = th_l2h_ini + (igi_target - IGI); IGI = igi_base + 30 + th_l2h_ini -
    # th_edcca_hl_diff, so th_l2h_ini cancels: th_l2h_dmc = igi_target - igi_base - 30
    # + th_edcca_hl_diff.
    th_l2h = min(10, _IGI_TARGET - _IGI_BASE - 30 + _TH_EDCCA_HL_DIFF)
    th_h2l = th_l2h - _TH_EDCCA_HL_DIFF
    _set_edcca_threshold(t, th_h2l, th_l2h)

    is_adjust = True
    while is_adjust:
        # check CCA status: dbg port 0, poll the busy bit (BIT3) up to 3x.
        _set_dbg_port(t, 0x0)
        v = t.read32(_DBG_VALUE)
        tries = 0
        while (v & (1 << 3)) and tries < 3:
            tries += 1
            v = t.read32(_DBG_VALUE)
        # count EDCCA=1 over 20 reads of the adaptivity dbg port.
        tx_edcca1 = 0
        for _ in range(20):
            _set_dbg_port(t, _ADAPT_DBG_PORT)
            v32 = t.read32(_DBG_VALUE)
            if v32 & (1 << 30):          # 8188E: EDCCA asserted on BIT30
                tx_edcca1 += 1
        if tx_edcca1 > 1:
            th_l2h = min(10, th_l2h + 1)
            th_h2l = th_l2h - _TH_EDCCA_HL_DIFF
            _set_edcca_threshold(t, th_h2l, th_l2h)
            if th_l2h == 10:
                is_adjust = False
        else:
            is_adjust = False

    _set_lna(t, enable=True)
    _set_edcca_threshold(t, 0x7F, 0x7F)          # resume no-link state


def _set_dbg_port(t, dbg_port: int) -> None:
    """``phydm_set_bb_dbg_port`` + ``phydm_release_bb_dbg_port`` (11N) [SRC]
    phydm_debug.c — the BB debug port. The release lowers the SW priority below
    PRIORITY_1, so every set re-acquires and writes the select (no read on release
    for 11N). Writing 0x908 is the only register effect."""
    t.write32(_DBG_SELECT, dbg_port)


def _set_lna(t, enable: bool) -> None:
    """``phydm_set_lna`` [SRC] phydm_adaptivity.c:177 (8188E, RF_PATH_A, 1T1R): open
    the RF gain page, write the RX-mode gain rows (LNA disabled = 0x37f82, normal =
    0x77f82), close the page."""
    rf.set_rf_reg(t, 0, 0xEF, 0x80000, 0x1)
    rf.set_rf_reg(t, 0, 0x30, 0xFFFFF, 0x18000)
    rf.set_rf_reg(t, 0, 0x31, 0xFFFFF, 0x0000F)
    rf.set_rf_reg(t, 0, 0x32, 0xFFFFF, 0x77F82 if enable else 0x37F82)
    rf.set_rf_reg(t, 0, 0xEF, 0x80000, 0x0)


def _set_edcca_threshold(t, h2l: int, l2h: int) -> None:
    """``phydm_set_edcca_threshold`` [SRC] phydm_adaptivity.c:159 (11N): rOFDM0_ECCA
    byte0 = L2H, byte2 = H2L."""
    bb.set_bb_reg(t, _REG_ECCA_TH, 0x00FF00FF, (l2h & 0xFF) | ((h2l & 0xFF) << 16))


def _post_seed_reads(t) -> None:
    """The remaining odm_dm_init sub-inits (rf_init / primary_cca / ra_info) read BB
    state into phydm structs; these reads change no chip state."""
    t.read32(0x0D2C)
    t.read32(0x0C80)
    t.read8(0x0A22)
    t.read32(0x0C24)
    t.read32(0x0C84)


def init_hal_tail(t) -> None:
    """The hal_init tail after ``rtl8188e_InitHalDm`` [SRC] usb_halinit.c:1597-1633:
    the fw_ractrl-off MAC defaults, the IQK-stage power-tracking arm + LC calibration,
    then the USB HRPWM clear and the xmit-ack enable. This card runs fw_ractrl=False
    (the wire emits the Tx-report writes) and **defers IQK** — only neediqk_24g is
    flagged; the runtime IQK fires on the first link. 28 ops; the LCK/power-track write
    values are read-derived (the replay serves the RF reads). [WIRE] cap1 1866-1893."""
    # if (!fw_ractrl): enable Tx report + the tynli test Tx-report time.
    t.write8(0x0421, 0x0F)             # REG_FWHW_TXQ_CTRL+1
    t.write16(0x04F0, 0x3DF0)          # REG_TX_RPT_TIME
    t.write8(0x04D3, 0x01)             # REG_EARLY_MODE_CONTROL+3 (Pretx_en, WEP/TKIP)
    v = t.read16(0x020C)
    t.write16(0x020C, v | (1 << 9))    # REG_TXDMA_OFFSET_CHK |= DROP_DATA_EN
    _txpwrtrack_arm(t)
    _lc_calibrate(t)
    t.write8(0xFE58, 0x00)             # REG_USB_HRPWM
    v = t.read32(0x0420)
    t.write32(0x0420, v | (1 << 12))   # REG_FWHW_TXQ_CTRL |= BIT12 (xmit-ack)


def _txpwrtrack_arm(t) -> None:
    """``odm_txpowertracking_check_ce`` init pass [SRC] halrf_powertracking_ce.c:694 —
    arm the thermal meter (RF_T_METER_NEW 0x42[17:16] = 0x3) and return; the thermal
    read-back happens on a later watchdog pass, not at init."""
    rf.set_rf_reg(t, 0, 0x42, (1 << 17) | (1 << 16), 0x03)


def _lc_calibrate(t) -> None:
    """``_phy_lc_calibrate_8188e(is2T=False)`` [SRC] halrf_8188e_ce.c:1308 — LC-tank
    (VCO) calibration, packet-TX path (the only path at init: cont-TX bits 0xd03[6:4]
    are 0). Block all queues, read RF reg18, set the LCK-begin bit (bit15, driven
    through the 0xfff mask via PHY_SetRFReg's no-remask), restore the queues."""
    cont = t.read8(0x0D03)
    if cont & 0x70:                    # continuous-TX path — not reached at init
        raise RuntimeError("RTL8188EUS LCK: unexpected continuous TX at init")
    t.write8(0x0522, 0xFF)             # REG_TXPAUSE: block all queues
    lc_cal = rf.phy_query_rf_reg(t, 0, 0x18, 0xFFF)    # RF_CHNLBW, MASK12BITS
    rf.set_rf_reg(t, 0, 0x18, 0xFFF, lc_cal | 0x08000)  # LCK begin (bit15)
    t.write8(0x0522, 0x00)             # resume queues
