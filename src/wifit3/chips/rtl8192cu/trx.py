from __future__ import annotations

import errno

from . import constants as C
from .mac import IS_NORMAL_CHIP
from .priv import RtlPriv, RtlUsb


def configvertoutep(rtlpriv: RtlPriv) -> int:
    """rtl8192cu/trx.c:17, test-chip endpoint config."""
    rtlusb = rtlpriv.usb
    ep_nums = 0
    rtlusb.out_queue_sel = 0
    ep_cfg = rtlpriv.io.read8(C.REG_TEST_SIE_OPTIONAL)
    ep_cfg = (ep_cfg & C.USB_TEST_EP_MASK) >> C.USB_TEST_EP_SHIFT
    if ep_cfg in (0, 3):
        rtlusb.out_queue_sel = C.TX_SELE_HQ | C.TX_SELE_LQ
        ep_nums = 2
    elif ep_cfg in (1, 2):
        txqsele = rtlpriv.io.read8(C.REG_TEST_USB_TXQS)
        if txqsele & 0x0F:
            rtlusb.out_queue_sel = C.TX_SELE_HQ
        elif txqsele & 0xF0:
            rtlusb.out_queue_sel = C.TX_SELE_LQ
        ep_nums = 1
    return 0 if rtlusb.out_ep_nums == ep_nums else -errno.EINVAL


def configvernoutep(rtlpriv: RtlPriv) -> int:
    """rtl8192cu/trx.c:50, normal-chip endpoint config."""
    rtlusb = rtlpriv.usb
    ep_nums = 0
    rtlusb.out_queue_sel = 0
    ep_cfg = rtlpriv.io.read8(C.REG_NORMAL_SIE_EP + 1)
    if ep_cfg & C.USB_NORMAL_SIE_EP_MASK:
        rtlusb.out_queue_sel |= C.TX_SELE_HQ
        ep_nums += 1
    if (ep_cfg >> C.USB_NORMAL_SIE_EP_SHIFT) & C.USB_NORMAL_SIE_EP_MASK:
        rtlusb.out_queue_sel |= C.TX_SELE_NQ
        ep_nums += 1
    ep_cfg = rtlpriv.io.read8(C.REG_NORMAL_SIE_EP + 2)
    if ep_cfg & C.USB_NORMAL_SIE_EP_MASK:
        rtlusb.out_queue_sel |= C.TX_SELE_LQ
        ep_nums += 1
    return 0 if rtlusb.out_ep_nums == ep_nums else -errno.EINVAL


def twooutepmapping(rtlusb: RtlUsb, is_chip8: bool, bwificfg: bool) -> None:
    """rtl8192cu/trx.c:79."""
    ep_map = rtlusb.ep_mapping
    if bwificfg:
        ep_map[C.RTL_TXQ_BE] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_BK] = rtlusb.out_eps[1]
        ep_map[C.RTL_TXQ_VI] = rtlusb.out_eps[1]
        ep_map[C.RTL_TXQ_VO] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_MGT] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_BCN] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_HI] = rtlusb.out_eps[0]
    else:
        ep_map[C.RTL_TXQ_BE] = rtlusb.out_eps[1]
        ep_map[C.RTL_TXQ_BK] = rtlusb.out_eps[1]
        ep_map[C.RTL_TXQ_VI] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_VO] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_MGT] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_BCN] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_HI] = rtlusb.out_eps[0]


def threeoutepmapping(rtlusb: RtlUsb, bwificfg: bool) -> None:
    """rtl8192cu/trx.c:109."""
    ep_map = rtlusb.ep_mapping
    if bwificfg:
        ep_map[C.RTL_TXQ_BE] = rtlusb.out_eps[2]
        ep_map[C.RTL_TXQ_BK] = rtlusb.out_eps[1]
        ep_map[C.RTL_TXQ_VI] = rtlusb.out_eps[1]
        ep_map[C.RTL_TXQ_VO] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_MGT] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_BCN] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_HI] = rtlusb.out_eps[0]
    else:
        ep_map[C.RTL_TXQ_BE] = rtlusb.out_eps[2]
        ep_map[C.RTL_TXQ_BK] = rtlusb.out_eps[2]
        ep_map[C.RTL_TXQ_VI] = rtlusb.out_eps[1]
        ep_map[C.RTL_TXQ_VO] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_MGT] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_BCN] = rtlusb.out_eps[0]
        ep_map[C.RTL_TXQ_HI] = rtlusb.out_eps[0]


def oneoutepmapping(rtlusb: RtlUsb) -> None:
    """rtl8192cu/trx.c:139."""
    for queue in (C.RTL_TXQ_BE, C.RTL_TXQ_BK, C.RTL_TXQ_VI, C.RTL_TXQ_VO,
                  C.RTL_TXQ_MGT, C.RTL_TXQ_BCN, C.RTL_TXQ_HI):
        rtlusb.ep_mapping[queue] = rtlusb.out_eps[0]


def _out_ep_mapping(rtlpriv: RtlPriv) -> int:
    """rtl8192cu/trx.c:153. The 3-EP case passes ischipn as bwificfg, as the source does."""
    rtlusb = rtlpriv.usb
    bwificfg = False
    ischipn = IS_NORMAL_CHIP(rtlpriv.rtlhal.version)
    if rtlusb.out_ep_nums == 2:
        twooutepmapping(rtlusb, ischipn, bwificfg)
    elif rtlusb.out_ep_nums == 3:
        if not ischipn:
            return -errno.EINVAL
        threeoutepmapping(rtlusb, ischipn)
    elif rtlusb.out_ep_nums == 1:
        oneoutepmapping(rtlusb)
    else:
        return -errno.EINVAL
    return 0


def rtl8192cu_endpoint_mapping(rtlpriv: RtlPriv) -> int:
    """rtl8192cu/trx.c:188."""
    if IS_NORMAL_CHIP(rtlpriv.rtlhal.version):
        error = configvernoutep(rtlpriv)
    else:
        error = configvertoutep(rtlpriv)
    if error:
        return error
    return _out_ep_mapping(rtlpriv)
