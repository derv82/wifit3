"""RTL8822BU cold bring-up — the deterministic init the byte-for-byte gate verifies.

`cold_bringup(t)` runs the exact vendor op sequence that
`scripts/rtl8822bu_dkms/verify_pcap.py` reproduces against capture-1/2/3: chip-ID/USB-PHY,
EFUSE probe, the `hal_read_mac_hidden_rpt` cycle (power-on + FW + MAC + FW-info + C2H read +
power-off), then the real `rtl8822b_hal_init` cycle (cold power-on + FW + MAC + init_mac_register
+ config_rx_info + enable_bb_rf + BB phy-reg/AGC + crystal cap + RF-A/RF-B). Both the driver
(`driver.connect`) and the gate call this, so the hardware path and the verified path are the
same code. Returns the chip info + EFUSE data the channel tune needs (rfe_type, chip_ver).

The two `[WIRE]`-pinned stray ops (`W 0x00AA`, `W 0xFE58`) bracket the inter-cycle power-off; the
2nd cycle's `send_general_info` carries the real 2T2R get_trx_path config (rf_type 2, ant 3/3,
package 7). See RTL8822BU_DKMS.md for the per-step citations.
"""
from __future__ import annotations

from . import bb, cal, chipid, efuse, firmware, mac, phy_cond, rf, usbphy
from . import constants as const


def cold_bringup(t):
    """Run the full two-cycle cold init; return (chip_info, efuse_data)."""
    info = chipid.get_chip_info(t)         # HALMAC chip-id / cut (R 0xFC, R 0xF1)
    usbphy.phy_cfg_usb(t, info.chip_ver)   # USB3 intf-phy params
    chipid.read_chip_version(t)            # rtw chip-version (R 0xF0/0xF4/0x68)
    e = efuse.read_efuse(t)                # HALMAC physical EFUSE dump + PG parse

    # --- cycle 1: hal_read_mac_hidden_rpt (power-on + FW + MAC + FW-info + C2H + power-off) ---
    mac.power_on(t, info.chip_ver)
    t.write8(const.REG_C2HEVT_MSG_NORMAL, const.C2H_DEFEATURE_RSVD)
    firmware.download(t, firmware.load_firmware_blob())
    mac.init_mac_cfg(t)
    mac.init_mac_flow_tail(t)
    alloc = mac.set_trx_fifo_info()
    firmware.send_general_info(t, e.rfe_type, info.chip_ver,
                               alloc.rsvd_fw_txbuf_addr - alloc.rsvd_boundary, alloc.rsvd_h2cq_addr)
    firmware.read_mac_hidden_rpt(t)
    t.write16(0x00AA, 0x8000)              # [WIRE] post-C2H op before the power-off
    mac.power_off(t, info.chip_ver)
    efuse.read_phydm_trim(t)               # 3 cached PG-trim reads
    t.write8(0xFE58, 0x00)                 # [WIRE] RPWM clear before the 2nd power-on

    # --- cycle 2: rtl8822b_hal_init (the real init) ---
    mac.power_on(t, info.chip_ver)         # cold power-on (reuse)
    firmware.download(t, firmware.load_firmware_blob(), beacon=True,
                      rsvd_boundary=alloc.rsvd_boundary)
    mac.init_mac_cfg(t)
    mac.init_mac_flow_tail(t)
    firmware.send_general_info(t, e.rfe_type, info.chip_ver,
                               alloc.rsvd_fw_txbuf_addr - alloc.rsvd_boundary, alloc.rsvd_h2cq_addr,
                               rf_type=2, rf_type_drv=2, tx_ant=3, rx_ant=3, package_type=7)
    mac.init_mac_register(t)               # PHYDM MAC-reg table
    mac.config_rx_info(t)                  # DRVINFO size + RCR app-physts
    mac.enable_bb_rf(t, e.log_map[0xCA])   # turn on BB/RF clocks
    bb.phy_parameter_init(t, post=False)   # PHYDM PRE_SETTING (OFDM/CCK off)
    bb.phy_bb_config(t)                    # BB phy-reg table
    cfg = phy_cond.PhyCondConfig(cut=info.chip_ver, rfe=e.rfe_type, package=7)
    bb.phy_agc_config(t, cfg)              # BB AGC table (cut/rfe walker)
    bb.set_crystal_cap(t, e.crystal_cap)   # xtal-cap into 0x24/0x28
    rf.phy_rf_config(t, cfg)               # RF-A then RF-B radio tables
    bb.phy_parameter_init(t, post=True)    # PHYDM POST_SETTING (OFDM/CCK on)
    usbphy.init_usb_cfg(t)                 # init_interface_cfg: RX-DMA burst mode + drop-data
    t.read32(const.REG_RCR)                # hal_init tail: HW_VAR_RCR sync read-back
    cal.config_trx_mode(t)                 # config_phydm_trx_mode: 2T2R TX/RX path + RF mode
    cal.aac_check(t)                       # one-off AAC check (RF_A 0xC9) before the DM init
    cal.rfe_init(t)                        # phydm_rfe_8822b_init: RFE pin mux (DM init start)
    st = cal.DmState()                     # PHYDM software state seeded here, used by dc_cancellation
    cal.common_info_self_init(t, st, e.rfe_type)   # cck_setting + rf_path_rx_enable + SoML RxHP seed
    cal.dig_init(t, st)                    # phydm_dig_init: DIG/IGI seed (RX detection)
    cal.cck_pd_init(t)                     # phydm_cck_pd_init: CCK packet-detection threshold
    cal.env_monitor_init(t)                # phydm_env_monitor_init: NHM + CLM + FAHM env-monitor
    cal.adaptivity_init(t)                 # phydm_adaptivity_init: EDCCA seed
    cal.ra_info_init(t)                    # phydm_ra_info_init: rate-adaptation + ARFR tables
    # phydm_rssi_monitor_init is a software no-op (no wire ops).
    cal.cfo_tracking_init(t)               # phydm_cfo_tracking_init: crystal-cap-by-WiFi (0x10[6])
    cal.rf_init(t)                         # phydm_rf_init: tx-power-track init (get_swing_index 0xc1c)
    cal.dc_cancellation(t, st)             # phydm_dc_cancellation: RX DC-offset measure + cancel
    cal.tx_current_calibration(t, e.pa_bias[0], e.pa_bias[1])  # phydm_txcurrentcalibration (TxA bias)
    cal.get_pa_bias_offset(t, e.phy_map)   # phydm_get_pa_bias_offset: PG PA-bias (0x3D5/6) -> RF 0x3f
    cal.psd_init(t)                        # phydm_psd_init: PSD-tool HW params (0x910) — odm_dm_init tail
    return info, e
