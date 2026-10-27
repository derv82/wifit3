"""rtlwifi core.c: table delay markers, the firmware callback, DIG defaults, and the mac80211 ops."""
from __future__ import annotations

import errno
import logging
import time
from dataclasses import dataclass
from importlib.resources import files

from . import constants as C
from .priv import RtlPriv
from .wifi import rtl_set_rfreg

logger = logging.getLogger(__name__)


def rtl_addr_delay(addr: int) -> None:
    """core.c:32."""
    if addr == 0xfe:
        time.sleep(50e-3)
    elif addr == 0xfd:
        time.sleep(5e-3)
    elif addr == 0xfc:
        time.sleep(1e-3)
    elif addr == 0xfb:
        time.sleep(50e-6)
    elif addr == 0xfa:
        time.sleep(5e-6)
    elif addr == 0xf9:
        time.sleep(1e-6)


def rtl_rfreg_delay(rtlpriv: RtlPriv, rfpath: int, addr: int, mask: int, data: int) -> None:
    """core.c:49."""
    if 0xf9 <= addr <= 0xfe:
        rtl_addr_delay(addr)
    else:
        rtl_set_rfreg(rtlpriv, rfpath, addr, mask, data)
        time.sleep(1e-6)


def request_firmware(name: str) -> bytes | None:
    """The firmware blobs ship in assets/ (linux-firmware rtlwifi/)."""
    asset = files(__package__).joinpath("assets").joinpath(name)
    if not asset.is_file():
        return None
    return asset.read_bytes()


def rtl_fw_cb(rtlpriv: RtlPriv, firmware: bytes | None) -> None:
    """core.c:72 rtl_fw_do_work (not WoWLAN)."""
    if firmware is None:
        firmware = request_firmware(rtlpriv.cfg.alt_fw_name)
        logger.info("Loading alternative firmware %s", rtlpriv.cfg.alt_fw_name)
        if firmware is None:
            logger.error("Selected firmware is not available")
            rtlpriv.max_fw_size = 0
            return
    if len(firmware) > rtlpriv.max_fw_size:
        logger.error("Firmware is too big!")
        return
    rtlpriv.rtlhal.pfirmware = bytes(firmware)
    rtlpriv.rtlhal.fwsize = len(firmware)


def rtl_dm_diginit(rtlpriv: RtlPriv, cur_igvalue: int) -> None:
    """core.c:1932."""
    dm_digtable = rtlpriv.dm_digtable
    dm_digtable.dig_enable_flag = True
    dm_digtable.dig_ext_port_stage = C.DIG_EXT_PORT_STAGE_MAX
    dm_digtable.cur_igvalue = cur_igvalue
    dm_digtable.pre_igvalue = 0
    dm_digtable.cur_sta_cstate = C.DIG_STA_DISCONNECT
    dm_digtable.presta_cstate = C.DIG_STA_DISCONNECT
    dm_digtable.curmultista_cstate = C.DIG_MULTISTA_DISCONNECT
    dm_digtable.rssi_lowthresh = C.DM_DIG_THRESH_LOW
    dm_digtable.rssi_highthresh = C.DM_DIG_THRESH_HIGH
    dm_digtable.fa_lowthresh = C.DM_FALSEALARM_THRESH_LOW
    dm_digtable.fa_highthresh = C.DM_FALSEALARM_THRESH_HIGH
    dm_digtable.rx_gain_max = C.DM_DIG_MAX
    dm_digtable.rx_gain_min = C.DM_DIG_MIN
    dm_digtable.back_val = C.DM_DIG_BACKOFF_DEFAULT
    dm_digtable.back_range_max = C.DM_DIG_BACKOFF_MAX
    dm_digtable.back_range_min = C.DM_DIG_BACKOFF_MIN
    dm_digtable.pre_cck_cca_thres = 0xff
    dm_digtable.cur_cck_cca_thres = 0x83
    dm_digtable.forbidden_igi = C.DM_DIG_MIN
    dm_digtable.large_fa_hit = 0
    dm_digtable.recover_cnt = 0
    dm_digtable.dig_min_0 = 0x25
    dm_digtable.dig_min_1 = 0x25
    dm_digtable.media_connect_0 = False
    dm_digtable.media_connect_1 = False
    rtlpriv.dm.dm_initialgain_enable = True
    dm_digtable.bt30_cur_igi = 0x32
    dm_digtable.pre_cck_pd_state = C.CCK_PD_STAGE_MAX
    dm_digtable.cur_cck_pd_state = C.CCK_PD_STAGE_LOWRSSI
    dm_digtable.pre_cck_fa_state = 0
    dm_digtable.cur_cck_fa_state = 0


@dataclass(frozen=True)
class Cfg80211ChanDef:
    """struct cfg80211_chan_def, the fields rtl_op_config reads (chan->hw_value is the channel number)."""
    hw_value: int
    center_freq: int
    width: int = C.NL80211_CHAN_WIDTH_20_NOHT
    center_freq1: int = 0


def ieee80211_channel_to_frequency(chan: int) -> int:
    """net/wireless/util.c, NL80211_BAND_2GHZ."""
    if chan == 14:
        return 2484
    return 2407 + chan * 5


def cfg80211_get_chandef_type(chandef: Cfg80211ChanDef) -> int:
    """include/net/cfg80211.h."""
    if chandef.width == C.NL80211_CHAN_WIDTH_20_NOHT:
        return C.NL80211_CHAN_NO_HT
    if chandef.width == C.NL80211_CHAN_WIDTH_20:
        return C.NL80211_CHAN_HT20
    if chandef.width == C.NL80211_CHAN_WIDTH_40:
        if chandef.center_freq1 > chandef.center_freq:
            return C.NL80211_CHAN_HT40PLUS
        return C.NL80211_CHAN_HT40MINUS
    logger.warning("cfg80211_get_chandef_type: width %d has no channel type", chandef.width)
    return C.NL80211_CHAN_NO_HT


def rtl_op_add_interface(rtlpriv: RtlPriv, vif_type: int, vif_addr: bytes) -> int:
    """core.c:210, the STATION case. rtl_ips_nic_on is a no-op: usb.c:318 clears inactiveps."""
    mac = rtlpriv.mac80211
    if mac.vif is not None:
        logger.warning("vif has been set!! mac->vif = %s", mac.vif.hex(":"))
        return -errno.EOPNOTSUPP
    retry_limit = 0x30
    if vif_type != C.NL80211_IFTYPE_STATION:
        logger.error("operation mode %d is not supported!", vif_type)
        return -errno.EOPNOTSUPP
    mac.vif = bytes(vif_addr)
    mac.opmode = vif_type
    rtlpriv.cfg.ops.set_network_type(rtlpriv, vif_type)
    mac.mac_addr = bytes(vif_addr)
    rtlpriv.cfg.ops.set_hw_reg(rtlpriv, C.HW_VAR_ETHER_ADDR, mac.mac_addr)
    mac.retry_long = retry_limit
    mac.retry_short = retry_limit
    rtlpriv.cfg.ops.set_hw_reg(rtlpriv, C.HW_VAR_RETRY_LIMIT, retry_limit)
    return 0


def rtl_op_remove_interface(rtlpriv: RtlPriv) -> None:
    """core.c:319; the AP/ADHOC/MESH beacon teardown is not ported."""
    mac = rtlpriv.mac80211
    mac.vif = None
    mac.link_state = C.MAC80211_NOLINK
    mac.opmode = C.NL80211_IFTYPE_UNSPECIFIED
    rtlpriv.cfg.ops.set_network_type(rtlpriv, mac.opmode)


def rtl_op_config(rtlpriv: RtlPriv, changed: int, chandef: Cfg80211ChanDef) -> int:
    """core.c:580. IDLE runs rtl_ips_nic_off/on, which do nothing on USB outside STATION
    (inactiveps is cleared, usb.c:318); PS needs swctrl_lps, never set here."""
    rtlphy = rtlpriv.phy
    mac = rtlpriv.mac80211
    if changed & C.IEEE80211_CONF_CHANGE_CHANNEL:
        width = chandef.width
        channel_type = C.NL80211_CHAN_NO_HT
        wide_chan = chandef.hw_value
        if width < C.NL80211_CHAN_WIDTH_80:
            channel_type = cfg80211_get_chandef_type(chandef)
        if width >= C.NL80211_CHAN_WIDTH_80:
            if width == C.NL80211_CHAN_WIDTH_80:
                center = chandef.center_freq1
                primary = chandef.center_freq
                rtlphy.current_chan_bw = C.HT_CHANNEL_WIDTH_80
                mac.bw_80 = True
                mac.bw_40 = True
                if center > primary:
                    mac.cur_80_prime_sc = C.PRIME_CHNL_OFFSET_LOWER
                    if center - primary == 10:
                        mac.cur_40_prime_sc = C.PRIME_CHNL_OFFSET_UPPER
                        wide_chan += 2
                    elif center - primary == 30:
                        mac.cur_40_prime_sc = C.PRIME_CHNL_OFFSET_LOWER
                        wide_chan += 6
                else:
                    mac.cur_80_prime_sc = C.PRIME_CHNL_OFFSET_UPPER
                    if primary - center == 10:
                        mac.cur_40_prime_sc = C.PRIME_CHNL_OFFSET_LOWER
                        wide_chan -= 2
                    elif primary - center == 30:
                        mac.cur_40_prime_sc = C.PRIME_CHNL_OFFSET_UPPER
                        wide_chan -= 6
        elif channel_type in (C.NL80211_CHAN_HT20, C.NL80211_CHAN_NO_HT):
            mac.cur_40_prime_sc = C.PRIME_CHNL_OFFSET_DONT_CARE
            rtlphy.current_chan_bw = C.HT_CHANNEL_WIDTH_20
            mac.bw_40 = False
            mac.bw_80 = False
        elif channel_type == C.NL80211_CHAN_HT40MINUS:
            mac.cur_40_prime_sc = C.PRIME_CHNL_OFFSET_UPPER
            rtlphy.current_chan_bw = C.HT_CHANNEL_WIDTH_20_40
            mac.bw_40 = True
            mac.bw_80 = False
            wide_chan -= 2
        elif channel_type == C.NL80211_CHAN_HT40PLUS:
            mac.cur_40_prime_sc = C.PRIME_CHNL_OFFSET_LOWER
            rtlphy.current_chan_bw = C.HT_CHANNEL_WIDTH_20_40
            mac.bw_40 = True
            mac.bw_80 = False
            wide_chan += 2
        else:
            mac.bw_40 = False
            mac.bw_80 = False
            logger.error("switch case %#x not processed", channel_type)
        if wide_chan <= 0:
            wide_chan = 1
        rtlphy.current_channel = wide_chan & 0xFF
        rtlpriv.cfg.ops.switch_channel(rtlpriv)
        rtlpriv.cfg.ops.set_channel_access(rtlpriv)
        rtlpriv.cfg.ops.set_bw_mode(rtlpriv, channel_type)
    return 0


def rtl_op_configure_filter(rtlpriv: RtlPriv, changed_flags: int, new_flags: int) -> int:
    """core.c:775; returns ``*new_flags`` masked to RTL_SUPPORTED_FILTERS."""
    mac = rtlpriv.mac80211
    maps = C.HalCfgMaps
    update_rcr = False
    new_flags &= C.RTL_SUPPORTED_FILTERS
    if changed_flags == 0:
        return new_flags
    if changed_flags & C.FIF_ALLMULTI:
        if new_flags & C.FIF_ALLMULTI:
            mac.rx_conf |= maps.MAC_RCR_AM | maps.MAC_RCR_AB
        else:
            mac.rx_conf &= ~(maps.MAC_RCR_AM | maps.MAC_RCR_AB)
        update_rcr = True
    if changed_flags & C.FIF_FCSFAIL:
        if new_flags & C.FIF_FCSFAIL:
            mac.rx_conf |= maps.MAC_RCR_ACRC32
        else:
            mac.rx_conf &= ~maps.MAC_RCR_ACRC32
        update_rcr = True
    if changed_flags & C.FIF_BCN_PRBRESP_PROMISC and mac.link_state >= C.MAC80211_LINKED:
        if mac.opmode not in (C.NL80211_IFTYPE_AP, C.NL80211_IFTYPE_MESH_POINT):
            rtlpriv.cfg.ops.set_chk_bssid(rtlpriv, not new_flags & C.FIF_BCN_PRBRESP_PROMISC)
            update_rcr = False
    if changed_flags & C.FIF_CONTROL:
        if new_flags & C.FIF_CONTROL:
            mac.rx_conf |= maps.MAC_RCR_ACF
        else:
            mac.rx_conf &= ~maps.MAC_RCR_ACF
        update_rcr = True
    if changed_flags & C.FIF_OTHER_BSS:
        if new_flags & C.FIF_OTHER_BSS:
            mac.rx_conf |= maps.MAC_RCR_AAP
        else:
            mac.rx_conf &= ~maps.MAC_RCR_AAP
        update_rcr = True
    mac.rx_conf &= 0xFFFFFFFF
    if update_rcr:
        rtlpriv.cfg.ops.set_hw_reg(rtlpriv, C.HW_VAR_RCR, mac.rx_conf)
    return new_flags
