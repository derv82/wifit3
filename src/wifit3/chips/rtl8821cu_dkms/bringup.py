"""RTL8821CU cold bring-up sequence — the canonical init the byte-for-byte gate verifies.

The cold path, in the order the wire shows:
  1. halmac mount chip-detect  (SYS_CFG2 / SYS_CFG1+1)         — chipid.mount_get_chip_info
  2. chip-version read         (SYS_CFG1 / SYS_STATUS1 / 0x68) — chipid.read_chip_version
  3. EFUSE dump + decode + parse                                — efuse.read_efuse
  4. MAC-hidden-rpt readback   (power-on + FW download + C2H)   — read_mac_hidden_rpt
The chip-info read (`rtl8821c_read_efuse`) ends by calling `hal_read_mac_hidden_rpt`, which is
what actually powers the chip and downloads firmware on this driver — so the cold FW download
lives here, not in the later `_halmac_init_hal`. Later milestones (MAC/BB/RF init, monitor
entry) extend ``cold_bringup`` past the report readback.
"""
from __future__ import annotations

from . import bb, btc, chan, chipid, dm, efuse, firmware, init, led, mac, phy, phy_cond, pwrseq, rf

REG_C2HEVT_MSG_NORMAL = 0x01A0      # [SRC] include/hal_com_reg.h:149
_C2H_DEFEATURE_RSVD = 0xFD          # [SRC] hal/hal_com_c2h.h:79 — "FW: report MAC-hidden via reg"
_C2H_MAC_HIDDEN_RPT = 0x19          # [SRC] hal/hal_com_c2h.h:67 — report-ready marker
_C2H_DBG = 0x00                     # [SRC] hal/hal_com_c2h.h:51 — "host done reading"
_MAC_HIDDEN_RPT_LEN = 8 + 5         # MAC_HIDDEN_RPT_LEN + MAC_HIDDEN_RPT_2_LEN


def power_on(t, info, already_on: bool = False) -> bool:
    """[SRC] rtl8821c_power_on halinit.c:78 + rtw_hal_power_on btc tail hal_intf.c:461.

    The APFM_ON_MAC software flag makes the power sequence idempotent: the first call runs
    hal_power_on (pre-init, card-enable switch, post-switch config) and the second skips it,
    but the BT-coex power-on setting re-runs either way (combo card). Returns the new flag.
    """
    if not already_on:
        init.pre_init_system_cfg(t)
        pwrseq.mac_pwr_switch(t, power_on=True)
        init.init_system_cfg(t)
    if info.bt_coexist:
        btc.power_on_setting(t, info.rfe_type, info.single_ant_path)
    return True


def read_mac_hidden_rpt(t, info) -> None:
    """[SRC] hal_read_mac_hidden_rpt hal_com.c:1550 (USB path).

    Power the chip on, tell the FW to report its MAC-hidden capability bits through a register
    (C2HEVT = DEFEATURE), download firmware (which powers on again — idempotent), then poll the
    report and acknowledge. The cold-boot firmware download is reached from here.
    """
    on = power_on(t, info, already_on=False)
    t.write8(REG_C2HEVT_MSG_NORMAL, _C2H_DEFEATURE_RSVD)
    firmware.fw_dl(t, info, on, power_on)
    for _ in range(800):
        if t.read8(REG_C2HEVT_MSG_NORMAL) == _C2H_MAC_HIDDEN_RPT:
            break
    else:
        raise RuntimeError("RTL8821CU: MAC-hidden report not ready")
    rpt = bytes(t.read8(REG_C2HEVT_MSG_NORMAL + 2 + i) for i in range(_MAC_HIDDEN_RPT_LEN))
    # c2h_mac_hidden_rpt_hdl [SRC] hal_com.c:1426 — PackageType = report[4] bits 4..6. It feeds
    # the phydm general-info H2C, but only on the *next* init: this report is read after the cold
    # _send_general_info already ran (inside fw_dl above), so cold sends package_type=0.
    info.package_type = (rpt[4] >> 4) & 0x07
    t.write8(REG_C2HEVT_MSG_NORMAL, _C2H_DBG)
    power_off(t, info)


REG_BT_SCOREBOARD = 0x00AA          # WiFi on/off bit cleared at power-off [SRC] hal_btcoex.c:5904


def power_off(t, info) -> None:
    """[SRC] rtw_hal_power_off hal_intf.c:475 — clear the BT-coex scoreboard WiFi bit (combo
    card) then the card-disable power switch. The probe powered the chip on only to read FW
    caps; with HW init not yet complete it powers back off (the real hal_init powers on again).
    """
    if info.bt_coexist:
        t.write16(REG_BT_SCOREBOARD, 0x8000)
    pwrseq.mac_pwr_switch(t, power_on=False)


def hal_init(t, info) -> None:
    """[SRC] _halmac_init_hal hal_halmac.c:3576 — the real HW init `airmon-ng start` triggers
    (via rtw_hal_init). The probe powered the chip off after reading FW caps, so this re-runs
    the full bring-up: power on, download FW, init the MAC flow, send the FW general/phydm info,
    then init the MAC registers / RX-info / BB+RF / interface (later milestones).

    Unlike the cold MAC-hidden FW download, ``not_xmitframe_fw_dl`` is 0 here, so the reserved-
    page chunks take the full ``update_txdesc`` descriptor (``full=True``). The H2C general-info
    path is byte-identical to cold (``update_txdesc_h2c_pkt`` == the minimal H2C descriptor)."""
    power_on(t, info, already_on=False)
    firmware.download_fw(t, info, full=True, rsvd_boundary=mac.txff_pages()["boundary"])
    mac.init_mac_flow(t, info)
    firmware.send_general_info(t, info)
    mac.init_mac_register(t)
    mac.config_rx_info(t)
    # rtl8821c_phy_init: enable BB/RF, then the PHYDM BB parameter tables. The table walker keys
    # on the PHYDM-transformed rfe/package (rfe>>3, package override), not the hal->* values.
    cfg = phy_cond.PhyCondConfig(cut=info.chip_ver, rfe=info.phydm_rfe_type,
                                 package=info.phydm_package_type)
    bb.init_bb_rf(t)
    bb.phy_parameter_init(t, post=False)
    bb.init_bb_reg(t, cfg, info.default_rf_set, info.crystal_cap)
    rf.config_radioa(t, cfg)
    bb.phy_parameter_init(t, post=True)
    mac.init_interface_cfg(t)
    # rtl8821c_hal_init tail (after _halmac_init_hal): the driver misc/monitor RX-filter setup,
    # then the PHYDM dynamic-mechanism init (DIG/CCK-PD/adaptivity/...).
    mac.hal_init_misc(t)
    dm.phy_init_haldm(t, info)
    # rtl8821c_hal_init tail after phy_init_haldm: beamforming MU-MIMO/TXBF defaults.
    mac.phy_bf_init(t)
    # rtl8821c_hal_init tail: BT-coex HAL init (combo card -> rtw_btcoex_HAL_Initialize).
    if info.bt_coexist:
        btc.hal_init(t, info)
    # rtl8821cu_hal_init (USB wrapper) tail: hal_init_misc enables the cosmetic WL activity LED.
    led.cfg_wl_led(t)


def iface_init(t, info) -> None:
    """[SRC] rtw_hal_iface_init hal_intf.c:521 — runs after rtw_hal_init returns. Programs the
    interface MAC into REG_MACID (HW_VAR_MAC_ADDR), then enables the HW port (BCN_CTRL).
    `rtw_led_control(POWER_ON)` between hal_init and here is wire-silent (the LED was already put
    in SW-control by hal_init_misc)."""
    mac.set_mac_addr(t, efuse.mac_address(info))
    mac.hw_port_enable(t)


def init_hw_mlme_ext(t, info) -> None:
    """[SRC] init_hw_mlme_ext rtw_mlme_ext.c:1279 — sync driver/HW state: enable RX BAR, then the
    first channel/bandwidth set (current_channel/bw were forced invalid so this always runs)."""
    mac.enable_rx_bar(t)
    # set_channel_bwmode(cur_channel): the default airmon channel is 1 (2.4 GHz, 20 MHz).
    chan.set_channel(t, info, 1)


def set_monitor_mode(t, info) -> None:
    """The airmon vif setopmode sequence after the channel tune ([SRC] hw_var_set_opmode
    rtl8821c_ops.c:1002): set STATION net-type on the primary vif, then MONITOR (promiscuous RCR +
    all-open RXFLTMAP) on the monitor vif — the driver-side RX-enable for monitor capture."""
    mac.set_opmode_station(t, efuse.mac_address(info))
    mac.set_opmode_monitor(t)


def cold_bringup(t) -> None:
    """The cold init the driver's connect() runs, in the order the wire shows. See module
    docstring. Verified byte-for-byte by ``scripts/verify_pcap.py rtl8821cu_dkms``."""
    _, chip_ver = chipid.mount_get_chip_info(t)
    chipid.read_chip_version(t)
    info = efuse.read_efuse(t)
    info.chip_ver = chip_ver
    read_mac_hidden_rpt(t, info)
    efuse.read_phydm_trim(t, info.phys_map)
    phy.init_hw_info_by_rfe(t, info)
    hal_init(t, info)
    iface_init(t, info)
    init_hw_mlme_ext(t, info)
    set_monitor_mode(t, info)
