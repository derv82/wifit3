"""RTL8922AU BT-coexistence init, ported from rtw89-7.2 coex.c and rtw8922a.c.

rtw89_btc_ntfy_init configures the WL/BT antenna sharing before the interface runs. On this card
(rfe_type 1) btc_set_rfe resolves to a 2-antenna shared setup with the BT-general path on RF path
B, and btc_init_cfg programs the per-path trx-mask LUT and the PTA priority / break tables.
"""
import struct

from .constants import (
    RF_PATH_A, RF_PATH_B, RR_LUTWE, RR_LUTWA, RR_LUTWD0, B_LUTWEN, RFREG_MASK,
    BTC_BT_SS_GROUP, BTC_BT_TX_GROUP, BTC_BT_RX_GROUP,
    BTC_TRX_MASK_SS, BTC_TRX_MASK_RX, BTC_TRX_MASK_TX_BTG, BTC_TRX_MASK_TX,
    R_BTC_COEX_WL_REQ_BE, B_BTC_RSP_ACK_HI, B_BTC_TX_BCN_HI, B_BTC_TX_TRI_HI, B_BTC_TX_NULL_HI,
    R_BE_BT_BREAK_TABLE, BTC_BREAK_PARAM, R_BTC_ZB_COEX_TBL_0, R_BTC_ZB_COEX_TBL_1,
    R_BTC_ZB_BREAK_TBL, BTC_ZB_COEX_TBL_VAL, BTC_ZB_BREAK_TBL_VAL,
    R_BE_SCOREBOARD, WL_TX_POWER_NO_BTC_CTRL,
    R_BE_PWR_RATE_CTRL, R_BE_PWR_REG_CTRL, R_BE_PWR_COEX_CTRL,
    B_BE_FORCE_PWR_BY_RATE_EN, B_BE_FORCE_PWR_BY_RATE_VAL, B_BE_PWR_BT_EN, B_BE_PWR_BT_VAL,
    H2C_CAT_OUTSRC, H2C_CL_OUTSRC_BTC,
    BTF_SET_REPORT_EN, BTF_SET_SLOT_TABLE, BTF_SET_MREG_TABLE, BTF_SET_CX_POLICY, BTF_SET_DRV_INFO,
    R_BE_BT_PLT, B_BE_TX_PLT_GNT_WL, B_BE_RX_PLT_GNT_WL, B_BE_PLT_EN,
    B_MAC_AX_SB_FW_MASK, B_AX_TOGGLE, B_MAC_AX_BTGS1_NOTIFY, MAC_AX_NOTIFY_TP_MAJOR,
    B_MAC_AX_SB_DRV_MASK, BTC_WSCB_INIT,
)
from . import firmware, phy

# The coex fw structs are version-selected (rtw89_btc_ver_defs[2] for this fw); the cold-init H2C
# payloads for THIS card (rfe_type 1, 2-ant shared, BTG on path B, no BT) are fixed. Values and
# byte layouts verified against the capture. [SRC] coex.c:152-159, 399-404, 336-341, core.h.
REG_MAC, REG_BB = 0, 1
_MON_REG = [                                     # rtw89_btc_8922a_mon_reg[]. rtw8922a.c:2904-2924
    (REG_MAC, 4, 0xE300), (REG_MAC, 4, 0xE320), (REG_MAC, 4, 0xE324), (REG_MAC, 4, 0xE328),
    (REG_MAC, 4, 0xE32C), (REG_MAC, 4, 0xE330), (REG_MAC, 4, 0xE334), (REG_MAC, 4, 0xE338),
    (REG_MAC, 4, 0xE344), (REG_MAC, 4, 0xE348), (REG_MAC, 4, 0xE34C), (REG_MAC, 4, 0xE350),
    (REG_MAC, 4, 0x11A2C), (REG_MAC, 4, 0x11A50),
    (REG_BB, 4, 0x980), (REG_BB, 4, 0x660), (REG_BB, 4, 0x1660), (REG_BB, 4, 0x418C),
    (REG_BB, 4, 0x518C),
]
_SLOT_MIX, _SLOT_ISO = 0, 1
_SLOT_DEF = [                                     # s_def[CXST_*] = (dur, cxtbl, cxtype). coex.c:81-100
    (100, 0x55555555, _SLOT_MIX), (5, 0xEA5A5A5A, _SLOT_ISO), (70, 0xEA5A5A5A, _SLOT_ISO),
    (15, 0xEA5A5A5A, _SLOT_ISO), (15, 0xEA5A5A5A, _SLOT_ISO), (250, 0xE5555555, _SLOT_MIX),
    (7, 0xEA5A5A5A, _SLOT_MIX), (5, 0xE5555555, _SLOT_MIX), (50, 0xE5555555, _SLOT_MIX),
    (20, 0xEA5A5A5A, _SLOT_ISO), (500, 0x55555555, _SLOT_MIX), (5, 0xEA5A5A5A, _SLOT_MIX),
    (5, 0xFFFFFFFF, _SLOT_ISO), (5, 0xE5555555, _SLOT_MIX), (5, 0x55555555, _SLOT_MIX),
    (250, 0xEA5A5A5A, _SLOT_MIX), (50, 0xFFFFFFFF, _SLOT_ISO), (50, 0xFFFFDFFF, _SLOT_ISO),
]


def _set_rfe(rfe_type: int, cv: int) -> dict:
    """rtw8922a_btc_set_rfe (software): resolve the antenna module info from efuse rfe_type and
    chip cut. Returns the fields btc_init_cfg consumes. [SRC] rtw8922a.c:2727-2769."""
    ant_num = 2 if (rfe_type % 2) else 3
    if cv == 0:
        ant_num = 2
    shared = ant_num != 3                    # num 3 -> dedicated, else shared
    btg_pos = RF_PATH_B
    single_pos = RF_PATH_A
    return {"num": ant_num, "shared": shared, "btg_pos": btg_pos, "single_pos": single_pos}


def _set_trx_mask(t, path: int, group: int, val: int) -> None:
    """rtw8922a_set_trx_mask: write the group index then its WL trx-mask value. [SRC] rtw8922a.c:2771."""
    phy.write_rf(t, path, RR_LUTWA, RFREG_MASK, group)
    phy.write_rf(t, path, RR_LUTWD0, RFREG_MASK, val)


def _init_cfg(t, ant: dict) -> None:
    """rtw8922a_btc_init_cfg: per-path trx-mask LUT setup, then the PTA priority, break table, and
    ZB coex tables. [SRC] rtw8922a.c:2778-2833."""
    if ant["num"] == 1:
        path_min = path_max = ant["single_pos"]
    else:
        path_min, path_max = RF_PATH_A, RF_PATH_B

    for path in range(path_min, path_max + 1):
        phy.write_rf(t, path, RR_LUTWE, RFREG_MASK, B_LUTWEN)
        _set_trx_mask(t, path, BTC_BT_SS_GROUP, BTC_TRX_MASK_SS)
        _set_trx_mask(t, path, BTC_BT_RX_GROUP, BTC_TRX_MASK_RX)
        if ant["shared"] and ant["btg_pos"] == path:
            _set_trx_mask(t, path, BTC_BT_TX_GROUP, BTC_TRX_MASK_TX_BTG)
        else:
            _set_trx_mask(t, path, BTC_BT_TX_GROUP, BTC_TRX_MASK_TX)
        phy.write_rf(t, path, RR_LUTWE, RFREG_MASK, 0)

    wl_pri = B_BTC_RSP_ACK_HI | B_BTC_TX_BCN_HI | B_BTC_TX_TRI_HI | B_BTC_TX_NULL_HI
    t.write32(R_BTC_COEX_WL_REQ_BE, wl_pri)
    t.write32(R_BE_BT_BREAK_TABLE, BTC_BREAK_PARAM)
    t.write32(R_BTC_ZB_COEX_TBL_0, BTC_ZB_COEX_TBL_VAL)
    t.write32(R_BTC_ZB_COEX_TBL_1, BTC_ZB_COEX_TBL_VAL)
    t.write32(R_BTC_ZB_BREAK_TBL, BTC_ZB_BREAK_TBL_VAL)


def _set_wl_txpwr_ctrl(t, txpwr_val: int) -> None:
    """rtw8922a_btc_set_wl_txpwr_ctrl: force-power and BT-power control on PHY_0 (get_txpwr_cr is
    identity for PHY_0). WL_TX_POWER_NO_BTC_CTRL passes 0xffff to both fields (the disable arms).
    [SRC] rtw8922a.c:2836-2867, mac.h:1573."""
    ctrl_all_time = txpwr_val & 0xFFFF
    ctrl_gnt_bt = (txpwr_val >> 16) & 0xFFFF
    if ctrl_all_time == 0xFFFF:
        t.write32_mask(R_BE_PWR_RATE_CTRL, B_BE_FORCE_PWR_BY_RATE_EN, 0x0)
        t.write32_mask(R_BE_PWR_RATE_CTRL, B_BE_FORCE_PWR_BY_RATE_VAL, 0x0)
    else:
        t.write32_mask(R_BE_PWR_RATE_CTRL, B_BE_FORCE_PWR_BY_RATE_VAL, ctrl_all_time)
        t.write32_mask(R_BE_PWR_RATE_CTRL, B_BE_FORCE_PWR_BY_RATE_EN, 0x1)
    if ctrl_gnt_bt == 0xFFFF:
        t.write32_mask(R_BE_PWR_REG_CTRL, B_BE_PWR_BT_EN, 0x0)
        t.write32_mask(R_BE_PWR_COEX_CTRL, B_BE_PWR_BT_VAL, 0x0)
    else:
        t.write32_mask(R_BE_PWR_COEX_CTRL, B_BE_PWR_BT_VAL, ctrl_gnt_bt)
        t.write32_mask(R_BE_PWR_REG_CTRL, B_BE_PWR_BT_EN, 0x1)


def _btc_h2c(t, ep: int, func: int, payload: bytes, dack: bool) -> None:
    """A coex BTFC_SET H2C: cat OUTSRC, class BTC. _send_fw_cmd uses dack=True; the cxdrv_* family
    uses dack=False. [SRC] coex.c:877-914, fw.c:5815."""
    firmware.h2c_command(t, ep, H2C_CAT_OUTSRC, H2C_CL_OUTSRC_BTC, func, payload,
                         rack=False, dack=dack)


def _fw_set_monreg(t, ep: int) -> None:
    """btc_fw_set_monreg (mreg table v7) + fw_en_rpt(RPT_EN_MREG). [SRC] coex.c:2642-2702, 399-404."""
    body = b"".join(struct.pack("<HHI", typ, nbytes, off) for typ, nbytes, off in _MON_REG)
    _btc_h2c(t, ep, BTF_SET_MREG_TABLE, bytes((0x02, 0x07, len(_MON_REG))) + body, dack=True)
    _btc_h2c(t, ep, BTF_SET_REPORT_EN,                    # rpt_ver(RPT_EN_MREG) = BIT(2)
             bytes((0x00, 0x08, 0x04)) + struct.pack("<I", 0x00000004), dack=True)


def _fw_set_slots(t, ep: int) -> None:
    """rtw89_btc_fw_set_slots (slot table v7): CXST_MAX slots from s_def. [SRC] coex.c:2554-2576."""
    body = b"".join(struct.pack("<HHI", dur, cxtype, cxtbl) for dur, cxtbl, cxtype in _SLOT_DEF)
    _btc_h2c(t, ep, BTF_SET_SLOT_TABLE, bytes((0x01, 0x07, len(_SLOT_DEF))) + body, dack=True)


def _fw_set_drv_info_init(t, ep: int) -> None:
    """_fw_set_drv_info(CXDRVINFO_INIT) -> cxdrv_init_v7: 24-byte init/module/ant info for this
    card (rfe_type 1, shared 2-ant, BTG path B). [SRC] coex.c:2772-2790, fw.c:5831, core.h:2305."""
    payload = bytes((
        0x00, 0x07, 0x18,                        # hdr: type INIT, ver 7, len 24
        0x06, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00,   # guard_ch, wl_only, init_ok, ...
        0x01, 0x02, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00,   # module: rfe_type, kt_ver, bt_pos(BTG), ...
        0x00, 0x02, 0x0A, 0x00, 0x00, 0x01, 0x00, 0x00,   # ant: type SHARED, num 2, iso 10, btg_pos B
    ))
    _btc_h2c(t, ep, BTF_SET_DRV_INFO, payload, dack=False)


def _fw_set_drv_info_ctrl(t, ep: int) -> None:
    """_fw_set_drv_info(CXDRVINFO_CTRL) -> cxdrv_ctrl_v7: manual 0, igno_bt 1. drvinfo_type 2 keeps
    type=CXDRVINFO_CTRL(6). [SRC] coex.c:2800-2806, fw.c:6305, core.h:3334."""
    _btc_h2c(t, ep, BTF_SET_DRV_INFO,
             bytes((0x06, 0x07, 0x04, 0x00, 0x01, 0x00, 0x00)), dack=False)


def _cfg_plt(t) -> None:
    """rtw89_mac_cfg_plt_be for _set_bt_plut(GNT_WL): PTA GNT_WL on tx+rx, PLT enabled, band 0.
    A plain 16-bit write. [SRC] coex.c:4444-4473, mac_be.c:2489-2512."""
    t.write16(R_BE_BT_PLT, B_BE_TX_PLT_GNT_WL | B_BE_RX_PLT_GNT_WL | B_BE_PLT_EN)


def _set_policy_off(t, ep: int) -> None:
    """_set_policy(BTC_CXP_OFF_BT): the only slot delta vs s_def is CXST_OFF's cxtbl -> 0xe5555555,
    sent as a one-record policy TLV (SET_CX_POLICY). [SRC] coex.c:3672-3679, 4058-4067, 2312-2374."""
    slot = struct.pack("<HHI", 100, _SLOT_MIX, 0xE5555555)      # CXST_OFF updated
    payload = bytes((0x01, 0x07, 0x09, 0x00)) + slot           # tlv v7: type SLOT, ver 7, len 9, id 0
    _btc_h2c(t, ep, BTF_SET_CX_POLICY, payload, dack=True)


def _fw_set_drv_info_role(t, ep: int) -> None:
    """_fw_set_drv_info(CXDRVINFO_ROLE) -> cxdrv_role_v8: no role set on cold init, so the 164-byte
    body is all zero. [SRC] coex.c:5616, fw.c:6177, fw.h:2475."""
    _btc_h2c(t, ep, BTF_SET_DRV_INFO, bytes((0x01, 0x08, 0xA4)) + bytes(164), dack=False)


def _cfg_sb(t, scbd: int) -> None:
    """rtw89_mac_cfg_sb: read the fw half of the scoreboard, keep it (minus BTGS1_NOTIFY, plus the
    power-on TP-major flag), and write our drv scoreboard with the toggle bit. POWERON is set by
    this point. [SRC] mac.c:6606-6625, coex.c:5624-5630."""
    fw_sb = (t.read32(R_BE_SCOREBOARD) & B_MAC_AX_SB_FW_MASK) >> 24
    fw_sb = (fw_sb & ~B_MAC_AX_BTGS1_NOTIFY) | MAC_AX_NOTIFY_TP_MAJOR
    val = B_AX_TOGGLE | (scbd & B_MAC_AX_SB_DRV_MASK) | ((fw_sb & 0x7F) << 24)
    t.write32(R_BE_SCOREBOARD, val)
    # fsleep(1000) after the write is a delay, not a wire op.


def _fw_set_drv_info_osi(t, ep: int) -> None:
    """_fw_set_drv_info(CXDRVINFO_OSI) -> cxdrv_osi_info: the GNT/WLACT config _set_gnt_v1 staged
    (fcxosi=1 ships it here instead of writing GNT registers). [SRC] coex.c:5632-5640, fw.c:6220."""
    payload = bytes((
        0x07, 0x01, 0x14,                        # hdr: type OSI(7), ver fcxosi 1, len 20
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,      # rf_band[2], btg_rx[2], nbtg_tx[2]
        0x01, 0x00, 0x01, 0x01,                  # gnt_set[0]: BT SW_LO (en1 v0), WL SW_HI (en1 v1)
        0x01, 0x00, 0x01, 0x01,                  # gnt_set[1]
        0x01, 0x01, 0x00, 0x00,                  # wlact_set[0] SW_HI, wlact_set[1]
        0x00, 0x00,                              # pta_req_hw_band, rf_gbt_source
    ))
    _btc_h2c(t, ep, BTF_SET_DRV_INFO, payload, dack=False)


def _run_coex_ntfy_init(t, ep: int) -> None:
    """_run_coex(BTC_RSN_NTFY_INIT) on the cold/no-BT path: _action_wl_init (set_bt_plut PLT write,
    OFF-BT policy) then _action_common (role info, scoreboard sync, OSI GNT info). The set_ant GNT
    and every other action are software no-ops here. [SRC] coex.c:7491-7609, 4722-4728, 5583-5643."""
    _cfg_plt(t)                                  # _set_bt_plut(GNT_WL)
    _set_policy_off(t, ep)                        # _set_policy(BTC_CXP_OFF_BT)
    _fw_set_drv_info_role(t, ep)                  # _action_common: CXDRVINFO_ROLE
    _cfg_sb(t, BTC_WSCB_INIT)                      # scbd_change sync (mac_cfg_sb)
    _fw_set_drv_info_osi(t, ep)                   # CXDRVINFO_OSI (fcxosi=1)


def ntfy_init(t, h2c_ep: int, cv: int) -> None:
    """rtw89_btc_ntfy_init(BTC_MODE_NORMAL): btc_set_rfe + btc_init_cfg, the BT scoreboard read and
    WL tx-power coex disable, the coex fw setup H2Cs (monreg, slots, drv-info init/ctrl), then
    _run_coex. get_ctrl_path and _write_scbd are software / no-op on the 8922A. [SRC] coex.c:7746."""
    ant = _set_rfe(t.rfe_type, cv)
    _init_cfg(t, ant)
    t.read32(R_BE_SCOREBOARD)                 # _update_bt_scbd -> _read_scbd (mac_get_sb)
    _set_wl_txpwr_ctrl(t, WL_TX_POWER_NO_BTC_CTRL)   # _set_wl_tx_power(RTW89_BTC_WL_DEF_TX_PWR)
    _fw_set_monreg(t, h2c_ep)
    _fw_set_slots(t, h2c_ep)
    _fw_set_drv_info_init(t, h2c_ep)
    _fw_set_drv_info_ctrl(t, h2c_ep)
    _run_coex_ntfy_init(t, h2c_ep)


_CXST_OFF = 0                                    # s_def[CXST_OFF] = the first slot. core.h:2660
_TDMA_OFF = bytes(7) + bytes((0x01,))            # t_def[CXTD_OFF] with option_ctrl set by the action


def _set_policy_full(t, ep: int) -> None:
    """_set_policy(BTC_CXP_OFF_BT) at role-start: the full policy TLV pair, not a delta. TDMA is
    CXTD_OFF with option_ctrl=1; the slot TLV carries every CXST_MAX slot from s_def, CXST_OFF's
    cxtbl overridden to 0xe5555555. [SRC] coex.c:_set_policy / _append_tdma / _append_slot_v7."""
    tdma_tlv = bytes((0x00, 0x07, 0x08)) + _TDMA_OFF     # tlv v7: type TDMA, ver 7, len 8
    slots = b""
    for i, (dur, cxtbl, cxtype) in enumerate(_SLOT_DEF):
        if i == _CXST_OFF:
            cxtbl = 0xE5555555
        slots += bytes((i,)) + struct.pack("<HHI", dur, cxtype, cxtbl)
    slot_tlv = bytes((0x01, 0x07, 0x09)) + slots         # tlv v7: type SLOT, ver 7, per-record len 9
    _btc_h2c(t, ep, BTF_SET_CX_POLICY, tdma_tlv + slot_tlv, dack=True)


def ntfy_role_info(t, ep: int) -> None:
    """rtw89_btc_ntfy_role_info(BTC_ROLE_START) for the monitor vif: _run_coex re-sends the full
    OFF-BT policy (tdma + all slots) and syncs the drv scoreboard. The role/OSI drv-info were
    already staged during ntfy_init, so only the policy and scoreboard change. [SRC] mac80211.c:154."""
    _set_policy_full(t, ep)
    _cfg_sb(t, BTC_WSCB_INIT)


def ntfy_radio_state_wl_on(t, cv: int) -> None:
    """rtw89_btc_ntfy_radio_state(BTC_RFCTRL_WL_ON): fw_en_rpt is a no-op (MREG already reported),
    _write_scbd is software, then _update_bt_scbd reads the scoreboard and btc_init_cfg re-runs the
    trx-mask/PTA/ZB setup. _run_coex(NTFY_RADIO_STATE) emits nothing on this cold WL-on path.
    [SRC] coex.c:btc_ntfy_radio_state."""
    t.read32(R_BE_SCOREBOARD)                 # _update_bt_scbd -> _read_scbd
    _init_cfg(t, _set_rfe(t.rfe_type, cv))
