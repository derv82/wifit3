import pytest

from wifit3.chips.rtl8922au import firmware, mac, phy
from wifit3.chips.rtl8922au.driver import RTL8922AUDriver
from wifit3.chips.rtl8922au.constants import (
    B_AX_WL_XTAL_SI_CMD_POLL, B_BE_ADDR_CAM_CLR, B_BE_APFM_OFFMAC, B_BE_BACAM_RST_MASK,
    B_BE_BT_DISN_EN, B_BE_HAXIDMA_IO_ST, B_BE_HCI_WLAN_IO_ST, B_BE_PTCL_BUSY, B_BE_RDY_SYSPWR,
    B_BE_WHOLE_SYS_PWR_STE_MASK, B_BE_WLMAC_PWR_STE_MASK, CHIP_CAV, MAC_AX_MAC_ON,
    MAC_AX_NOTIFY_PWR_MAJOR, MAC_AX_NOTIFY_TP_MAJOR, MAC_AX_SYS_ACT,
    R_AX_WLAN_XTAL_SI_CTRL, R_BE_C2HREG_CTRL, R_BE_H2CREG_CTRL, R_BE_HCI_OPT_CTRL,
    R_BE_IC_PWR_STATE, R_BE_SCOREBOARD, R_BE_SYS_CHIPINFO, R_BE_SYS_PW_CTRL, R_BE_SYSON_FSM_MON,
    R_BE_UDM1, R_BE_WL_BT_PWR_CTRL, RTW89_FWCMD_C2HREG_FUNC_PHY_CAP_PART1, RTW89_MAC_0,
    S_BE_BACAM_RST_ALL, R_AX_WDE_INI_STATUS, WDE_MGN_INI_RDY, R_BE_SS_CTRL, B_BE_MLO_HW_CHGLINK_EN,
    R_BE_CMAC_SHARE_ACQCHK_CFG_0, B_BE_R_MACID_ACQ_CHK_EN, R_BE_PLE_BUFMGN_CTL, B_BE_PLE_AVAL_UPD_REQ,
)

SYS_ACT = mac.field_prep(B_BE_WHOLE_SYS_PWR_STE_MASK, MAC_AX_SYS_ACT)


@pytest.fixture(autouse=True)
def _clock_1ms_per_read(mocker):
    now_us = 0

    def perf_counter():
        nonlocal now_us
        now_us += 1000
        return now_us / 1_000_000

    def sleep(seconds):
        nonlocal now_us
        now_us += round(seconds * 1_000_000)
    mocker.patch("wifit3.chips.rtl8922au.iopoll.time.perf_counter", side_effect=perf_counter)
    mocker.patch("wifit3.chips.rtl8922au.iopoll.time.sleep", side_effect=sleep)


def _transport(mocker, regs):
    t = mocker.Mock(h2c_counter=0, c2h_counter=0)
    t.read32.side_effect = lambda addr: regs.get(addr, 0)
    t.read16.side_effect = lambda addr: regs.get(addr, 0)
    t.read8.side_effect = lambda addr: regs.get(addr, 0)
    return t


def _xtal_si_writes(t):
    return [c for c in t.write32.call_args_list if c.args[0] == R_AX_WLAN_XTAL_SI_CTRL]


def test_addr_cam_init_fails_when_the_clear_never_finishes(mocker, caplog):
    t = _transport(mocker, {})
    t.read16.side_effect = lambda addr: B_BE_ADDR_CAM_CLR
    with pytest.raises(TimeoutError):
        mac.addr_cam_init(t, RTW89_MAC_0)
    assert "[ERR]ADDR_CAM reset" in caplog.text


def test_rst_bacam_fails_when_the_reset_never_finishes(mocker, caplog):
    t = _transport(mocker, {})
    t.read32.side_effect = lambda addr: mac.field_prep(B_BE_BACAM_RST_MASK, S_BE_BACAM_RST_ALL)
    with pytest.raises(TimeoutError):
        mac._rst_bacam(t)
    assert "[ERR]bacam rst timeout" in caplog.text


def test_band1_enable_fails_when_band0_tx_stays_busy(mocker, caplog):
    t = _transport(mocker, {})
    t.read8.side_effect = lambda addr: B_BE_PTCL_BUSY
    dle_quota_change = mocker.patch.object(mac, "dle_quota_change")
    with pytest.raises(TimeoutError):
        mac.band1_enable(t, CHIP_CAV)
    dle_quota_change.assert_not_called()
    assert "[ERR]tx idle poll -110" in caplog.text


def test_band1_enable_logs_a_dle_quota_change_timeout(mocker, caplog):
    t = _transport(mocker, {R_BE_PLE_BUFMGN_CTL: B_BE_PLE_AVAL_UPD_REQ})
    with pytest.raises(TimeoutError):
        mac.band1_enable(t, CHIP_CAV)
    assert "update PLE B0 TX avail page fail -110" in caplog.text
    assert "[ERR]DLE quota change -110" in caplog.text


def test_cmac_init_logs_the_addr_cam_failure_and_reraises(mocker, caplog):
    mocker.patch.object(mac, "addr_cam_init", side_effect=TimeoutError)
    with pytest.raises(TimeoutError):
        mac.cmac_init(_transport(mocker, {}), RTW89_MAC_0, CHIP_CAV)
    assert "[ERR]CMAC0 ADDR_CAM reset -110" in caplog.text


def test_cmac_init_logs_the_rmac_failure_and_reraises(mocker, caplog):
    mocker.patch.object(mac, "addr_cam_init")
    mocker.patch.object(mac, "rmac_init", side_effect=TimeoutError)
    with pytest.raises(TimeoutError):
        mac.cmac_init(_transport(mocker, {}), RTW89_MAC_0, CHIP_CAV)
    assert "[ERR]CMAC0 RMAC init -110" in caplog.text


def test_band1_enable_logs_the_cmac1_init_failure_and_reraises(mocker, caplog):
    for name in ("dle_quota_change", "preload_init", "cmac_pwr_en", "cmac_func_en"):
        mocker.patch.object(mac, name)
    mocker.patch.object(mac, "tx_idle_poll_band", return_value=True)
    mocker.patch.object(mac, "cmac_init", side_effect=TimeoutError)
    with pytest.raises(TimeoutError):
        mac.band1_enable(_transport(mocker, {}), CHIP_CAV)
    assert "[ERR]CMAC1 init -110" in caplog.text


def test_dbcc_enable_logs_the_band1_failure_and_skips_the_notify(mocker, caplog):
    mocker.patch.object(mac, "band1_enable", side_effect=TimeoutError)
    notify = mocker.patch.object(firmware, "h2c_notify_dbcc")
    with pytest.raises(TimeoutError):
        mac.dbcc_enable(_transport(mocker, {}), CHIP_CAV, 2)
    notify.assert_not_called()
    assert "[ERR] band1_enable -110" in caplog.text


def test_trx_init_logs_the_cmac0_init_failure_and_reraises(mocker, caplog):
    mocker.patch.object(mac, "dmac_init")
    mocker.patch.object(mac, "cmac_init", side_effect=TimeoutError)
    with pytest.raises(TimeoutError):
        mac.trx_init(_transport(mocker, {}), CHIP_CAV, 2)
    assert "[ERR]CMAC0 init -110" in caplog.text


def test_trx_init_logs_the_dbcc_enable_failure_and_reraises(mocker, caplog):
    mocker.patch.object(mac, "dmac_init")
    mocker.patch.object(mac, "cmac_init")
    mocker.patch.object(mac, "dbcc_enable", side_effect=TimeoutError)
    with pytest.raises(TimeoutError):
        mac.trx_init(_transport(mocker, {}), CHIP_CAV, 2)
    assert "[ERR]dbcc_enable init -110" in caplog.text


async def test_bringup_logs_the_mac_init_failure_and_reraises(mocker, caplog):
    drv = RTL8922AUDriver()
    drv.transport = mocker.Mock()
    mocker.patch.object(drv, "_switch_usb_mode")
    mocker.patch.object(mac, "read_chip_ver", return_value={"cv": CHIP_CAV, "acv": 0, "cid": 0, "aid": 0})
    mocker.patch.object(mac, "parse_efuse_map", return_value={"mac_addr": bytes(6)})
    for name in ("mac_pwr_on", "partial_init", "parse_phycap_map", "setup_phycap", "mac_pwr_off",
                 "rfkill_polling_init", "mac_preinit"):
        mocker.patch.object(mac, name)
    mocker.patch.object(mac, "mac_init", side_effect=TimeoutError)
    with pytest.raises(TimeoutError):
        await drv._bringup()
    assert "mac init fail, ret:-110" in caplog.text


def test_h2c_reg_is_not_written_when_the_mailbox_stays_busy(mocker, caplog):
    t = _transport(mocker, {R_BE_H2CREG_CTRL: 1})
    with pytest.raises(TimeoutError):
        firmware.msg_reg(t, 0, 2, 0)
    t.write32.assert_not_called()
    assert "FW does not process h2c registers" in caplog.text


def test_c2h_reg_is_not_acked_when_the_mailbox_stays_empty(mocker, caplog):
    t = _transport(mocker, {})
    with pytest.raises(TimeoutError):
        firmware.msg_reg(t, 0, 2, 0)
    assert mocker.call(R_BE_C2HREG_CTRL, 0) not in t.write8.call_args_list
    assert t.c2h_counter == 0
    assert "c2h reg timeout" in caplog.text


def test_read_phycap_restores_the_efuse_state_when_the_mailbox_fails(mocker):
    t = _transport(mocker, {R_BE_IC_PWR_STATE: SYS_ACT, R_BE_H2CREG_CTRL: 1})
    with pytest.raises(TimeoutError):
        mac.read_phycap(t, 0)
    t.write32_set.assert_called_with(R_BE_WL_BT_PWR_CTRL, B_BE_BT_DISN_EN)


def test_read_phycap_fails_on_a_reply_for_the_other_part(mocker):
    t = _transport(mocker, {R_BE_IC_PWR_STATE: SYS_ACT})
    mocker.patch.object(firmware, "msg_reg",
                        return_value={"id": RTW89_FWCMD_C2HREG_FUNC_PHY_CAP_PART1, "content_len": 0,
                                      "w": [0, 0, 0, 0]})
    with pytest.raises(RuntimeError):
        mac.read_phycap(t, 0)
    t.write32_set.assert_called_with(R_BE_WL_BT_PWR_CTRL, B_BE_BT_DISN_EN)


def test_write_xtal_si_fails_when_the_command_never_completes(mocker, caplog):
    t = _transport(mocker, {R_AX_WLAN_XTAL_SI_CTRL: B_AX_WL_XTAL_SI_CMD_POLL})
    with pytest.raises(TimeoutError):
        mac.write_xtal_si(t, 0x10, 0x20, 0x30)
    assert t.read32.call_count == 49      # 50 ms of 1 ms reads + 50 us sleeps, then the last read
    assert "xtal si not ready(W)" in caplog.text


def test_read_xtal_si_fails_when_the_command_never_completes(mocker, caplog):
    t = _transport(mocker, {R_AX_WLAN_XTAL_SI_CTRL: B_AX_WL_XTAL_SI_CMD_POLL})
    with pytest.raises(TimeoutError):
        mac.read_xtal_si(t, 0x10)
    assert "xtal si not ready(R)" in caplog.text


def test_pwr_on_func_stops_at_the_first_xtal_si_error(mocker):
    t = _transport(mocker, {R_AX_WLAN_XTAL_SI_CTRL: B_AX_WL_XTAL_SI_CMD_POLL,
                            R_BE_SYS_PW_CTRL: B_BE_RDY_SYSPWR,
                            R_BE_HCI_OPT_CTRL: B_BE_HCI_WLAN_IO_ST})
    with pytest.raises(TimeoutError):
        mac.pwr_on_func(t, CHIP_CAV)
    assert len(_xtal_si_writes(t)) == 1


def test_read_chip_ver_returns_early_on_an_xtal_si_error(mocker):
    t = _transport(mocker, {R_AX_WLAN_XTAL_SI_CTRL: B_AX_WL_XTAL_SI_CMD_POLL})
    ver = mac.read_chip_ver(t)
    assert (ver["acv"], ver["cid"], ver["aid"]) == (0, 0, 0)
    assert mocker.call(R_BE_SYS_CHIPINFO) not in t.read32.call_args_list


def test_rfk_pll_init_skips_the_write_when_the_xtal_si_read_fails(mocker):
    t = _transport(mocker, {R_AX_WLAN_XTAL_SI_CTRL: B_AX_WL_XTAL_SI_CMD_POLL})
    phy._rfk_pll_init(t)
    assert len(_xtal_si_writes(t)) == 1


def test_cfo_init_issues_every_xtal_si_access_when_each_fails(mocker):
    t = _transport(mocker, {R_AX_WLAN_XTAL_SI_CTRL: B_AX_WL_XTAL_SI_CMD_POLL})
    t.xtal_cap = 0x40
    phy._cfo_init(t)
    assert len(_xtal_si_writes(t)) == 4


def _no_pwr_off_notify(t, mocker):
    return mocker.call(R_BE_SCOREBOARD + 3, MAC_AX_NOTIFY_PWR_MAJOR) not in t.write8.call_args_list


def test_mac_pwr_off_stops_at_the_first_xtal_si_error_without_raising(mocker):
    t = _transport(mocker, {R_AX_WLAN_XTAL_SI_CTRL: B_AX_WL_XTAL_SI_CMD_POLL})
    mac.mac_pwr_off(t)
    assert len(_xtal_si_writes(t)) == 1
    assert _no_pwr_off_notify(t, mocker)


def test_mac_pwr_off_carries_on_when_hci_wlan_io_never_stops(mocker):
    t = _transport(mocker, {R_BE_HCI_OPT_CTRL: B_BE_HCI_WLAN_IO_ST})
    mac.mac_pwr_off(t)
    assert mocker.call(R_BE_SYS_PW_CTRL, B_BE_APFM_OFFMAC) not in t.write32_set.call_args_list
    assert _no_pwr_off_notify(t, mocker)


def test_mac_pwr_off_carries_on_when_offmac_never_clears(mocker):
    t = _transport(mocker, {R_BE_SYS_PW_CTRL: B_BE_APFM_OFFMAC})
    mac.mac_pwr_off(t)
    assert mocker.call(R_BE_UDM1, 0) not in t.write32.call_args_list
    assert _no_pwr_off_notify(t, mocker)


def test_cnv_efuse_state_warns_and_carries_on_when_sys_act_never_comes(mocker, caplog):
    t = _transport(mocker, {})
    mac.cnv_efuse_state(t, False)
    assert "failed to convert efuse state" in caplog.text


def test_efuse_read_ecv_keeps_the_cut_when_the_efuse_never_reads_ready(mocker):
    t = _transport(mocker, {R_BE_IC_PWR_STATE: SYS_ACT})
    assert mac.efuse_read_ecv(t, 5) == 5


def test_efuse_read_fw_secure_warns_and_carries_on_when_the_efuse_never_reads_ready(mocker, caplog):
    t = _transport(mocker, {R_BE_IC_PWR_STATE: SYS_ACT})
    mac.efuse_read_fw_secure(t, CHIP_CAV)
    assert "failed to dump secsel map" in caplog.text


def _patch_power_switch_on(mocker, pwr_on_func_effect):
    calls = mocker.Mock()
    for name in ("power_switch_boot_mode", "reset_pwr_state_be", "mac_pwr_off", "efuse_read_fw_secure"):
        calls.attach_mock(mocker.patch.object(mac, name), name)
    mocker.patch.object(mac, "efuse_read_ecv", return_value=CHIP_CAV)
    calls.attach_mock(mocker.patch.object(mac, "pwr_on_func", side_effect=pwr_on_func_effect),
                      "pwr_on_func")
    return calls


def test_mac_pwr_on_powers_off_and_retries_once_when_power_on_fails(mocker):
    calls = _patch_power_switch_on(mocker, [TimeoutError, None])
    t = _transport(mocker, {})
    mac.mac_pwr_on(t, CHIP_CAV)
    assert [c[0] for c in calls.mock_calls] == [
        "power_switch_boot_mode", "reset_pwr_state_be", "pwr_on_func", "mac_pwr_off",
        "power_switch_boot_mode", "reset_pwr_state_be", "pwr_on_func", "efuse_read_fw_secure"]
    t.write8.assert_called_once_with(R_BE_SCOREBOARD + 3, MAC_AX_NOTIFY_TP_MAJOR)


def test_mac_pwr_on_fails_when_the_retry_fails(mocker):
    calls = _patch_power_switch_on(mocker, TimeoutError)
    t = _transport(mocker, {})
    with pytest.raises(TimeoutError):
        mac.mac_pwr_on(t, CHIP_CAV)
    assert calls.pwr_on_func.call_count == 2
    calls.mac_pwr_off.assert_called_once_with(t)
    t.write8.assert_not_called()


def test_mac_init_powers_off_before_reraising(mocker):
    for name in ("partial_init", "enable_bb_rf", "sys_init"):
        mocker.patch.object(mac, name)
    mocker.patch.object(mac, "trx_init", side_effect=TimeoutError)
    mac_pwr_off = mocker.patch.object(mac, "mac_pwr_off")
    ofld_cfg = mocker.patch.object(firmware, "h2c_set_ofld_cfg")
    t = _transport(mocker, {})
    with pytest.raises(TimeoutError):
        mac.mac_init(t, 2, CHIP_CAV)
    mac_pwr_off.assert_called_once_with(t)
    ofld_cfg.assert_not_called()


def test_mac_init_powers_off_on_a_firmware_error(mocker):
    mocker.patch.object(mac, "partial_init", side_effect=RuntimeError)
    mac_pwr_off = mocker.patch.object(mac, "mac_pwr_off")
    t = _transport(mocker, {})
    with pytest.raises(RuntimeError):
        mac.mac_init(t, 2, CHIP_CAV)
    mac_pwr_off.assert_called_once_with(t)


def test_reset_pwr_state_logs_the_pmc_poll_timeout(mocker, caplog):
    t = _transport(mocker, {R_BE_SYSON_FSM_MON: 5})
    with pytest.raises(TimeoutError):
        mac.reset_pwr_state_be(t)
    assert t.read32.call_count == 1 + 1002    # 2 s of 1 ms reads + 1 ms sleeps, then the last read
    assert "[ERR]Polling WLAN PMC timeout= 5" in caplog.text


def test_reset_pwr_state_logs_the_haxi_io_poll_timeout(mocker, caplog):
    t = _transport(mocker, {R_BE_IC_PWR_STATE: mac.field_prep(B_BE_WLMAC_PWR_STE_MASK, MAC_AX_MAC_ON),
                            R_BE_HCI_OPT_CTRL: B_BE_HAXIDMA_IO_ST})
    with pytest.raises(TimeoutError):
        mac.reset_pwr_state_be(t)
    assert "[ERR]Polling HAXI IO timeout= 2" in caplog.text


def test_mac_pwr_off_clears_the_cmac_power_flags(mocker):
    mocker.patch.object(mac, "pwr_off_func")
    t = _transport(mocker, {})
    t.cmac_pwr = {0, 1}
    mac.mac_pwr_off(t)
    assert t.cmac_pwr == set()


def _bringup_driver(mocker):
    drv = RTL8922AUDriver()
    drv.transport = mocker.Mock()
    mocker.patch.object(drv, "_switch_usb_mode")
    mocker.patch.object(mac, "read_chip_ver", return_value={"cv": CHIP_CAV, "acv": 0, "cid": 0, "aid": 0})
    mocker.patch.object(mac, "parse_efuse_map", return_value={"mac_addr": bytes(6)})
    for name in ("mac_pwr_on", "partial_init", "parse_phycap_map", "setup_phycap", "mac_pwr_off",
                 "rfkill_polling_init", "mac_preinit", "mac_init"):
        mocker.patch.object(mac, name)
    return drv


async def test_bringup_logs_the_power_on_failure_and_does_not_power_off(mocker, caplog):
    drv = _bringup_driver(mocker)
    mac.mac_pwr_on.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        await drv._bringup()
    mac.partial_init.assert_not_called()
    mac.mac_pwr_off.assert_not_called()
    assert "failed to power on" in caplog.text
    assert "failed to setup chip information" in caplog.text


async def test_bringup_powers_off_when_chip_info_setup_fails(mocker, caplog):
    drv = _bringup_driver(mocker)
    mac.setup_phycap.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        await drv._bringup()
    mac.mac_pwr_off.assert_called_once_with(drv.transport)
    mac.mac_preinit.assert_not_called()
    assert "failed to setup chip information" in caplog.text


async def test_bringup_powers_off_when_the_firmware_file_is_missing(mocker, caplog):
    drv = _bringup_driver(mocker)
    mac.partial_init.side_effect = FileNotFoundError
    with pytest.raises(FileNotFoundError):
        await drv._bringup()
    mac.mac_pwr_off.assert_called_once_with(drv.transport)
    assert "failed to setup chip information" in caplog.text


async def test_bringup_logs_the_mac_preinit_failure_and_reraises(mocker, caplog):
    drv = _bringup_driver(mocker)
    mac.mac_preinit.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        await drv._bringup()
    mac.mac_init.assert_not_called()
    assert "mac preinit fail, ret: -110" in caplog.text


def test_dle_init_logs_when_wde_never_reads_ready(mocker, caplog):
    t = _transport(mocker, {})
    with pytest.raises(TimeoutError):
        mac.dle_init(t, "DBCC")
    assert "[ERR]WDE cfg ready" in caplog.text


def test_dle_init_logs_when_ple_never_reads_ready(mocker, caplog):
    t = _transport(mocker, {R_AX_WDE_INI_STATUS: WDE_MGN_INI_RDY})
    with pytest.raises(TimeoutError):
        mac.dle_init(t, "DBCC")
    assert "[ERR]PLE cfg ready" in caplog.text


def test_partial_init_logs_a_dle_pre_init_failure(mocker, caplog):
    mocker.patch.object(mac, "dle_init", side_effect=TimeoutError)
    t = _transport(mocker, {})
    with pytest.raises(TimeoutError):
        mac.partial_init(t, 2, CHIP_CAV)
    assert "[ERR]DLE pre init -110" in caplog.text


def _patch_dmac_init_steps(mocker):
    for step in ("dle_init", "hfc_init", "sta_sch_init", "mpdu_proc_init", "sec_eng_init",
                 "txpktctrl_init", "mlo_init"):
        mocker.patch.object(mac, step)


def test_dmac_init_logs_a_dle_init_failure(mocker, caplog):
    _patch_dmac_init_steps(mocker)
    mac.dle_init.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        mac.dmac_init(_transport(mocker, {}))
    assert "[ERR]DLE init -110" in caplog.text


def test_dmac_init_logs_a_sta_sch_init_failure(mocker, caplog):
    _patch_dmac_init_steps(mocker)
    mac.sta_sch_init.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        mac.dmac_init(_transport(mocker, {}))
    assert "[ERR]STA SCH init -110" in caplog.text


def test_dmac_init_logs_a_mlo_init_failure(mocker, caplog):
    _patch_dmac_init_steps(mocker)
    mac.mlo_init.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        mac.dmac_init(_transport(mocker, {}))
    assert "[ERR]MLO init -110" in caplog.text


def test_sta_sch_init_logs_when_the_scheduler_never_reads_done(mocker, caplog):
    t = _transport(mocker, {})
    with pytest.raises(TimeoutError):
        mac.sta_sch_init(t)
    assert "[ERR]STA scheduler init" in caplog.text


def test_mlo_init_still_enables_change_link_and_acq_check_on_a_timeout(mocker, caplog):
    t = _transport(mocker, {})
    with pytest.raises(TimeoutError):
        mac.mlo_init(t)
    t.write32_set.assert_any_call(R_BE_SS_CTRL, B_BE_MLO_HW_CHGLINK_EN)
    t.write32_set.assert_any_call(R_BE_CMAC_SHARE_ACQCHK_CFG_0, B_BE_R_MACID_ACQ_CHK_EN)
    assert "[MLO]mlo_init_be: MLO init polling timeout" in caplog.text


def test_trx_init_logs_a_dmac_init_failure(mocker, caplog):
    mocker.patch.object(mac, "dmac_init", side_effect=TimeoutError)
    with pytest.raises(TimeoutError):
        mac.trx_init(_transport(mocker, {}), CHIP_CAV, 2)
    assert "[ERR]DMAC init -110" in caplog.text


async def test_bringup_logs_the_mac_init_firmware_status_error(mocker, caplog):
    drv = _bringup_driver(mocker)
    mac.mac_init.side_effect = firmware.Rtw89Error(-22, "fw checksum fail")
    with pytest.raises(firmware.Rtw89Error):
        await drv._bringup()
    assert "mac init fail, ret:-22" in caplog.text
