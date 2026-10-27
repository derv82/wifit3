"""Monitor-mode bring-up after hw_init: configure_filter, rtl_op_config tunes, the watchdog, active monitor."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from wifit3.chips.rtl8192cu import SUPPORTED_IDS
from wifit3.chips.rtl8192cu import constants as C
from wifit3.chips.rtl8192cu.base import rtl_watchdog_wq_callback
from wifit3.chips.rtl8192cu.core import Cfg80211ChanDef, ieee80211_channel_to_frequency, rtl_op_config
from wifit3.chips.rtl8192cu.driver import RTL8192CUDriver
from wifit3.chips.rtl8192cu.priv import RtlPriv
from wifit3.chips.rtl8192cu.usb import UsbEndpointDescriptor, rtl_usb_probe, rtl_usb_start

from .fake import FAKE_MAC, NORMAL_SIE_EP_2OUT, SYS_CFG_88C_TSMC, FakeChip, probed_efuse

EP_2OUT = [UsbEndpointDescriptor(a, t) for a, t in ((0x81, 2), (0x02, 2), (0x03, 2), (0x84, 3))]
LSSI_A, LSSI_B = C.RFPGA0_XA_LSSIPARAMETER, C.RFPGA0_XB_LSSIPARAMETER
SPOOFED = bytes.fromhex("020000000001")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)


def _started(sys_cfg=SYS_CFG_88C_TSMC) -> tuple[RtlPriv, FakeChip]:
    io = FakeChip({C.REG_SYS_CFG: sys_cfg, C.REG_9346CR: C.EEPROM_EN, **NORMAL_SIE_EP_2OUT}, probed_efuse())
    priv = RtlPriv(io)
    rtl_usb_probe(priv, EP_2OUT, True)
    assert rtl_usb_start(priv, lambda: 0) == 0
    io.log.clear()
    return priv, io


def _rf_writes(io: FakeChip, lssi: int) -> list[tuple[int, int]]:
    return [((v >> 20) & 0x3F, v & 0xFFFFF) for v in io.writes(lssi)]


def _tune(priv: RtlPriv, channel: int, width=C.NL80211_CHAN_WIDTH_20_NOHT, center_freq1=0) -> None:
    chandef = Cfg80211ChanDef(channel, ieee80211_channel_to_frequency(channel), width, center_freq1)
    rtl_op_config(priv, C.IEEE80211_CONF_CHANGE_CHANNEL, chandef)


async def test_connect_brings_up_monitor_and_starts_the_watchdog(monkeypatch):
    entry = next(d for d in SUPPORTED_IDS if (d.vid, d.pid) == (0x0BDA, 0x8176))
    driver = RTL8192CUDriver.from_usb_device(MagicMock(), entry)
    io = FakeChip({C.REG_SYS_CFG: SYS_CFG_88C_TSMC, C.REG_9346CR: C.EEPROM_EN, **NORMAL_SIE_EP_2OUT},
                  probed_efuse())
    driver.priv = RtlPriv(io)
    endpoints = [SimpleNamespace(bEndpointAddress=e.bEndpointAddress, bmAttributes=e.bmAttributes) for e in EP_2OUT]
    driver.transport = MagicMock(interface=endpoints,
                                 **{"is_high_speed.return_value": True, "bulk_read.return_value": None})

    assert await driver.connect() is True
    await asyncio.sleep(0)
    await asyncio.sleep(0.05)

    assert driver.mac_address == FAKE_MAC.hex(":")
    rcr = C.RCR_APM | C.RCR_AM | C.RCR_ADF | C.RCR_AB | C.RCR_APPFCS | C.RCR_APP_ICV | C.RCR_AMF \
        | C.RCR_HTC_LOC_CTRL | C.RCR_APP_MIC | C.RCR_APP_PHYSTS | C.RCR_ACRC32
    assert io.writes(C.REG_RCR)[-1] == rcr | C.RCR_ACF | C.RCR_AAP
    assert driver.priv.phy.current_channel == 1
    assert C.REG_RCR in io.addrs("R")
    await driver.set_channel(11)
    assert driver.priv.phy.current_channel == 11
    with pytest.raises(ValueError):
        await driver.set_channel(36)
    assert driver.transport.bulk_read.call_args.args[:2] == (0x81, C.RTL92C_SIZE_MAX_RX_BUFFER)
    await driver.close()
    assert driver._watchdog_task is None and driver._rx_reader is None
    assert io.writes(C.REG_RSV_CTRL)[-1] == 0x0E
    driver.transport.release.assert_called()


def test_tune_writes_txpower_then_rf_chnlbw_then_channel_access_then_bw():
    priv, io = _started()
    _tune(priv, 6)
    first = [a for k, a, _w, _v in io.log if k == "W"]
    assert first.index(C.RTXAGC_A_CCK1_MCS32) < first.index(LSSI_A) < first.index(C.REG_SLOT) \
        < first.index(C.REG_BWOPMODE)
    rf_a = _rf_writes(io, LSSI_A)
    assert rf_a == [(C.RF_CHNLBW, 6), (C.RF_CHNLBW, 0x400 | 6)]
    assert priv.phy.rfreg_chnlval[0] & 0xFFF == 0x400 | 6
    assert io.writes(C.REG_SLOT) == [0]
    assert io.writes(C.REG_EDCA_BE_PARAM) == io.writes(C.REG_EDCA_VO_PARAM) == [0]
    for reg in (C.REG_SIFS_CCK + 1, C.REG_SIFS_OFDM + 1, C.REG_SPEC_SIFS + 1, C.REG_T2T_SIFS + 1):
        assert io.writes(reg) == [0x0a]
    assert io.writes(C.REG_BWOPMODE)[-1] & C.BW_OPMODE_20MHZ
    assert _rf_writes(io, LSSI_B) == []


def test_ofdm_txpower_follows_efuse_and_rf_type():
    priv, io = _started()
    _tune(priv, 1)
    efuse = priv.efuse
    cck = efuse.txpwrlevel_cck[0][0]
    ofdm = efuse.txpwrlevel_ht40_1s[0][0]
    legacy = ofdm + efuse.txpwr_legacyhtdiff[0][0]
    mcs = (ofdm + efuse.txpwr_ht20diff[0][0]) & 0xFF
    assert (io.writes(C.RTXAGC_A_CCK1_MCS32)[-1] >> 8) & 0xFF == min(cck, C.RF6052_MAX_TX_PWR)
    assert io.writes(C.RTXAGC_A_RATE18_06) == [min(legacy, 0x3F) * 0x01010101]
    assert io.writes(C.RTXAGC_A_MCS03_MCS00) == [min(mcs, 0x3F) * 0x01010101]
    top = min(mcs, 0x3F)
    assert io.writes(0xc90) == [max(top - 8, 0)] and io.writes(0xc92) == [max(max(top - 16, 0) - 6, 0)]
    assert io.writes(0xc98) and not io.writes(0xc9c)


def test_2t2r_tunes_both_paths_and_derives_the_mcs15_offsets():
    priv, io = _started(sys_cfg=C.TYPE_ID)
    assert priv.phy.rf_type == C.RF_2T2R
    _tune(priv, 3)
    assert _rf_writes(io, LSSI_B)[0][0] == C.RF_CHNLBW
    assert priv.phy.rfreg_chnlval[1] & 0x3FF == 3
    assert io.writes(C.RTXAGC_A_MCS15_MCS12) and io.writes(0xc90)


def test_umc_b_cut_channel_6_patches_rf_rx_g1():
    priv, io = _started(sys_cfg=C.VENDOR_ID | 0x1000)
    _tune(priv, 6)
    assert (C.RF_RX_G1, 0x00255) in _rf_writes(io, LSSI_A)
    io.log.clear()
    _tune(priv, 7)
    assert (C.RF_RX_G1, 0x00255) not in _rf_writes(io, LSSI_A)
    assert C.RF_RX_G1 in [a for a, _v in _rf_writes(io, LSSI_A)]


@pytest.mark.parametrize("center_offset, prime_sc, wide_chan", [(10, C.PRIME_CHNL_OFFSET_LOWER, 8),
                                                               (-10, C.PRIME_CHNL_OFFSET_UPPER, 4)])
def test_ht40_switches_bandwidth_and_tunes_the_wide_channel(center_offset, prime_sc, wide_chan):
    priv, io = _started()
    primary = ieee80211_channel_to_frequency(6)
    _tune(priv, 6, C.NL80211_CHAN_WIDTH_40, primary + center_offset)
    assert priv.phy.current_chan_bw == C.HT_CHANNEL_WIDTH_20_40 and priv.phy.current_channel == wide_chan
    assert priv.mac80211.cur_40_prime_sc == prime_sc
    assert not io.writes(C.REG_BWOPMODE)[-1] & C.BW_OPMODE_20MHZ
    assert io.writes(C.REG_RRSR + 2)[-1] >> 5 == prime_sc
    assert io.writes(C.RFPGA0_RFMOD)[-1] & C.BRFMOD
    assert io.writes(0x818)
    assert _rf_writes(io, LSSI_A)[-1] == (C.RF_CHNLBW, priv.phy.rfreg_chnlval[0])
    assert priv.phy.rfreg_chnlval[0] & 0xC00 == 0


def test_dynamic_txpower_level1_overrides_tx_agc():
    priv, io = _started()
    priv.dm.dynamic_txhighpower_lvl = C.TXHIGHPWRLEVEL_LEVEL1
    _tune(priv, 1)
    assert io.writes(C.RTXAGC_A_RATE18_06) == [0x14141414]
    assert (io.writes(C.RTXAGC_A_CCK1_MCS32)[-1] >> 8) & 0xFF == 0x10


def test_watchdog_alternates_thermal_trigger_and_tracking():
    priv, io = _started()
    rtl_watchdog_wq_callback(priv)
    reads = io.addrs("R")
    assert reads[0] == C.REG_RCR
    assert reads[1:5] == [C.ROFDM_PHYCOUNTER1, C.ROFDM_PHYCOUNTER2, C.ROFDM_PHYCOUNTER3, C.ROFDM0_FRAMESYNC]
    assert C.RFPGA0_XCD_RFINTERFACESW in reads and priv.reg_init
    assert _rf_writes(io, LSSI_A) == [(C.RF_T_METER, 0x60)]
    assert priv.dm.tm_trigger == 1
    io.log.clear()
    rtl_watchdog_wq_callback(priv)
    assert C.RFPGA0_XCD_RFINTERFACESW not in io.addrs("R")
    assert C.RFPGA0_XA_HSSIPARAMETER2 in io.addrs("R") and priv.dm.tm_trigger == 0


def test_watchdog_skips_dm_when_the_rcr_reports_fw_power_save():
    priv, io = _started()
    io.regs[C.REG_RCR] = 0x00070000
    rtl_watchdog_wq_callback(priv)
    assert io.addrs("R") == [C.REG_RCR]


def test_watchdog_is_a_no_op_until_the_hal_starts():
    io = FakeChip()
    priv = RtlPriv(io)
    rtl_watchdog_wq_callback(priv)
    assert io.log == []


def test_dig_resets_initial_gain_when_the_sta_state_changes():
    priv, io = _started()
    priv.dm_digtable.presta_cstate = C.DIG_STA_CONNECT
    rtl_watchdog_wq_callback(priv)
    assert io.writes(C.ROFDM0_XAAGCCORE1) and io.writes(C.ROFDM0_XBAGCCORE1)
    assert priv.dm_digtable.cur_igvalue == 0x1f and priv.dm_digtable.pre_igvalue == 0x1f


def test_cck_pd_threshold_written_when_its_state_changes():
    priv, io = _started()
    priv.dm_digtable.pre_cck_pd_state = C.CCK_PD_STAGE_HIGHRSSI
    rtl_watchdog_wq_callback(priv)
    assert io.writes(C.RCCK0_CCA)[-1] & 0xFF0000 == 0x830000


def test_rf_saving_enters_save_mode_on_strong_rssi():
    priv, io = _started()
    priv.dm.entry_min_undec_sm_pwdb = 40
    priv.dm_pstable.pre_rfstate = C.RF_NORMAL
    rtl_watchdog_wq_callback(priv)
    assert priv.dm_pstable.cur_rfstate == C.RF_SAVE
    assert len(io.writes(0x818)) == 2


def test_92c_skips_rf_saving():
    priv, io = _started(sys_cfg=C.TYPE_ID)
    rtl_watchdog_wq_callback(priv)
    assert C.RFPGA0_XCD_RFINTERFACESW not in io.addrs("R")


async def _connected_driver() -> tuple[RTL8192CUDriver, FakeChip]:
    priv, io = _started()
    entry = next(d for d in SUPPORTED_IDS if (d.vid, d.pid) == (0x0BDA, 0x8176))
    driver = RTL8192CUDriver.from_usb_device(MagicMock(), entry)
    driver.priv = priv
    driver._started = True
    return driver, io


async def test_active_monitor_sets_station_msr_and_macid_without_bssid_checks():
    driver, io = await _connected_driver()
    io.regs[C.MSR] = 0x00
    assert await driver.enter_active_monitor(SPOOFED) == SPOOFED
    assert io.writes(C.MSR) == [C.MSR_INFRA]
    assert bytes(io.writes(C.REG_MACID + i)[-1] for i in range(6)) == SPOOFED
    assert io.writes(C.REG_RL) == [0x30 << C.RETRY_LIMIT_SHORT_SHIFT | 0x30]
    assert not io.writes(C.REG_RCR)[-1] & (C.RCR_CBSSID_DATA | C.RCR_CBSSID_BCN)
    assert io.writes(C.REG_BCNTCFG + 1) == [0x66]
    await driver.enter_active_monitor(FAKE_MAC)
    assert io.writes(C.MSR)[-1] == C.MSR_INFRA and io.writes(C.REG_MACID)[-1] == FAKE_MAC[0]
    io.log.clear()
    await driver.exit_active_monitor()
    assert io.writes(C.MSR) == [C.MSR_NOLINK]
    assert driver.priv.mac80211.vif is None


async def test_rx_acks_toggle_the_ack_subtype_in_the_ctrl_filter():
    driver, io = await _connected_driver()
    await driver.enable_rx_acks()
    assert io.writes(C.REG_RXFLTMAP1) == [C.BIT(13)]
    io.regs[C.REG_RXFLTMAP1] = C.BIT(13)
    await driver.disable_rx_acks()
    assert io.writes(C.REG_RXFLTMAP1)[-1] == 0
