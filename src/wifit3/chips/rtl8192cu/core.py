"""rtlwifi core.c: table delay markers, the firmware callback, DIG defaults."""
from __future__ import annotations

import logging
import time
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
