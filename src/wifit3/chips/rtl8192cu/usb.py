"""rtlwifi usb.c: probe, adapter start/stop, the bulk-IN completion path and the bulk-OUT TX path."""
from __future__ import annotations

import errno
import logging
from dataclasses import dataclass
from typing import Callable

from . import constants as C
from .base import (
    ieee80211_has_pm,
    ieee80211_is_data,
    ieee80211_is_nullfunc,
    is_broadcast_ether_addr,
    is_multicast_ether_addr,
    rtl_action_proc,
    rtl_init_rx_config,
)
from .hw import rtl92cu_read_eeprom_info
from .mac import rtl92c_read_chip_version
from .priv import Ieee80211RxStatus, Ieee80211TxInfo, RtlPriv, RtlStats, RtlTcbDesc
from .sw import rtl92cu_init_sw_vars
from .trx import rtl8192cu_endpoint_mapping, rtl8192cu_mq_to_hwq, rtl92cu_rx_query_desc, rtl92cu_tx_fill_desc
from .wifi import is_hal_stop, set_hal_start, set_hal_stop

logger = logging.getLogger(__name__)

USB_DIR_IN = 0x80
USB_ENDPOINT_NUMBER_MASK = 0x0F
USB_ENDPOINT_XFERTYPE_MASK = 0x03
USB_ENDPOINT_XFER_BULK = 2


@dataclass(frozen=True)
class UsbEndpointDescriptor:
    bEndpointAddress: int
    bmAttributes: int


class ProbeError(Exception):
    """rtl_usb_probe failed; ``err`` is the negative errno the kernel would return."""

    def __init__(self, err: int, detail: str) -> None:
        self.err = err
        super().__init__(f"{detail} ({err})")


def _rtl_usb_init_tx(rtlpriv: RtlPriv, is_high_speed: bool) -> int:
    """usb.c:171."""
    rtlusb = rtlpriv.usb
    rtlusb.max_bulk_out_size = C.USB_HIGH_SPEED_BULK_SIZE if is_high_speed else C.USB_FULL_SPEED_BULK_SIZE
    for ep_num in rtlusb.ep_mapping:
        if not ep_num:
            return -errno.EINVAL
    return 0


def _rtl_usb_init_rx(rtlpriv: RtlPriv) -> int:
    """usb.c:213."""
    rtlpriv.usb.rx_max_size = C.RTL92C_SIZE_MAX_RX_BUFFER
    rtlpriv.usb.rx_urb_num = C.RTL92C_NUM_RX_URBS
    return 0


def _rtl_usb_init(rtlpriv: RtlPriv, endpoints: list[UsbEndpointDescriptor], is_high_speed: bool) -> int:
    """usb.c:236. Counts every OUT endpoint but records only bulk ones, as the source does."""
    rtlusb = rtlpriv.usb
    rtlusb.out_ep_nums = rtlusb.in_ep_nums = 0
    for pep_desc in endpoints:
        is_bulk = (pep_desc.bmAttributes & USB_ENDPOINT_XFERTYPE_MASK) == USB_ENDPOINT_XFER_BULK
        if pep_desc.bEndpointAddress & USB_DIR_IN:
            if is_bulk and rtlusb.in_ep_nums == 0:
                rtlusb.in_ep = pep_desc.bEndpointAddress & USB_ENDPOINT_NUMBER_MASK
            rtlusb.in_ep_nums += 1
        else:
            if rtlusb.out_ep_nums < C.RTL_USB_MAX_BULKOUT_NUM and is_bulk:
                rtlusb.out_eps[rtlusb.out_ep_nums] = pep_desc.bEndpointAddress & USB_ENDPOINT_NUMBER_MASK
            rtlusb.out_ep_nums += 1

    if rtlusb.out_ep_nums == 0:
        return -errno.EINVAL
    err = rtl8192cu_endpoint_mapping(rtlpriv)
    if err:
        return err
    err = _rtl_usb_init_tx(rtlpriv, is_high_speed)
    if err:
        return err
    return _rtl_usb_init_rx(rtlpriv)


def rtl_usb_init_sw(rtlpriv: RtlPriv) -> None:
    """usb.c:310."""
    mac = rtlpriv.mac80211
    ppsc = rtlpriv.psc
    rtlusb = rtlpriv.usb
    ppsc.inactiveps = False
    ppsc.leisure_ps = False
    ppsc.fwctrl_lps = False
    ppsc.reg_fwctrl_lps = 3
    ppsc.reg_max_lps_awakeintvl = 5
    ppsc.fwctrl_psmode = C.FW_PS_DTIM_MODE
    mac.beacon_interval = 100
    mac.min_space_cfg = 0
    mac.max_mss_density = 0
    mac.current_ampdu_density = 7
    mac.current_ampdu_factor = 3
    rtlusb.acm_method = C.EACMWAY2_SW
    rtlusb.irq_mask[0] = 0xFFFFFFFF
    rtlusb.irq_mask[1] = 0xFFFFFFFF
    rtlusb.disablehwsm = True


def rtl_usb_probe(rtlpriv: RtlPriv, endpoints: list[UsbEndpointDescriptor], is_high_speed: bool) -> None:
    """usb.c:951: chip version, EFUSE, endpoint config, sw state, firmware load."""
    rtl92c_read_chip_version(rtlpriv)
    rtl92cu_read_eeprom_info(rtlpriv)
    err = _rtl_usb_init(rtlpriv, endpoints, is_high_speed)
    if err:
        raise ProbeError(err, "_rtl_usb_init failed")
    rtl_usb_init_sw(rtlpriv)
    if rtl92cu_init_sw_vars(rtlpriv):
        raise ProbeError(-errno.ENODEV, "Can't init_sw_vars")
    rtlpriv.status |= C.BIT(C.RTL_STATUS_INTERFACE_START)


def rtl_usb_start(rtlpriv: RtlPriv, _rtl_usb_receive: Callable[[], int]) -> int:
    """usb.c:681. ``_rtl_usb_receive`` starts the bulk-IN reader (the driver owns the USB handle)."""
    err = rtlpriv.cfg.ops.hw_init(rtlpriv)
    if not err:
        rtl_init_rx_config(rtlpriv)
        rtlpriv.usb.state = C.USB_STATE_START
        set_hal_start(rtlpriv.rtlhal)
        err = _rtl_usb_receive()
    return err


def rtl_usb_stop(rtlpriv: RtlPriv, usb_kill_anchored_urbs: Callable[[], None]) -> None:
    """usb.c:737. ``usb_kill_anchored_urbs`` idles the bulk-IN reader before the power-off."""
    set_hal_stop(rtlpriv.rtlhal)
    rtlpriv.usb.state = C.USB_STATE_STOP
    usb_kill_anchored_urbs()
    rtlpriv.cfg.ops.hw_disable(rtlpriv)


def _rtl_rx_completed(rtlpriv: RtlPriv, transfer: bytes) -> bytes | None:
    """usb.c:539: the bulk-IN buffer queued for _rtl_rx_work, or None when it is dropped."""
    if rtlpriv.usb.state == C.USB_STATE_STOP:
        return None
    size = len(transfer)
    if size < C.RTL_RX_DESC_SIZE + C.IEEE80211_HDR_SIZE:
        logger.error("Too short packet from bulk IN! (len: %d)", size)
        return None
    return transfer


def _rtl_usb_rx_process_noagg(rtlpriv: RtlPriv, skb: bytes) -> tuple[bytes, Ieee80211RxStatus] | None:
    """usb.c:411: the MPDU (FCS still attached) and rx_status ieee80211_rx gets, or None when freed.
    rtl_beacon_statistic counts only a linked STATION's beacons; led_control writes nothing."""
    rxdesc = skb[:C.RTL_RX_DESC_SIZE]
    stats = RtlStats()
    rx_status = Ieee80211RxStatus()
    skb = skb[C.RTL_RX_DESC_SIZE:]
    rtl92cu_rx_query_desc(rtlpriv, stats, rx_status, rxdesc, skb)
    skb = skb[stats.rx_drvinfo_size + stats.rx_bufshift:]
    if stats.crc:
        return None
    fc = int.from_bytes(skb[0:2], "little")
    addr1 = bytes(skb[4:10])
    unicast = False
    if not is_broadcast_ether_addr(addr1) and not is_multicast_ether_addr(addr1):
        unicast = True
        rtlpriv.stats.rxbytesunicast += len(skb)
    if ieee80211_is_data(fc) and unicast:
        rtlpriv.link_info.num_rx_inperiod += 1
    if rtl_action_proc(rtlpriv, skb, False):
        return skb, rx_status
    return None


def _rtl_rx_work(rtlpriv: RtlPriv, skb: bytes) -> tuple[bytes, Ieee80211RxStatus] | None:
    """usb.c:480. rtl92cu_interface_cfg sets no usb_rx_segregate_hdl and hw_init enables no RX
    aggregation, so each bulk-IN transfer carries one frame."""
    if rtlpriv.usb.state == C.USB_STATE_STOP:
        return None
    return _rtl_usb_rx_process_noagg(rtlpriv, skb)


def _rtl_usb_tx_preprocess(rtlpriv: RtlPriv, skb: bytes, info: Ieee80211TxInfo, hw_queue: int) -> bytes:
    """usb.c:867; returns the skb with the TX descriptor pushed in front."""
    tcb_desc = RtlTcbDesc()
    hdr = bytearray(skb)
    fc = int.from_bytes(hdr[0:2], "little")
    pda_addr = bytes(hdr[4:10])
    if rtlpriv.psc.sw_ps_enabled:
        if ieee80211_is_data(fc) and not ieee80211_is_nullfunc(fc) and not ieee80211_has_pm(fc):
            hdr[0:2] = (fc | C.IEEE80211_FCTL_PM).to_bytes(2, "little")
    rtl_action_proc(rtlpriv, bytes(hdr), True)
    if is_multicast_ether_addr(pda_addr):
        rtlpriv.stats.txbytesmulticast += len(hdr)
    elif is_broadcast_ether_addr(pda_addr):
        rtlpriv.stats.txbytesbroadcast += len(hdr)
    else:
        rtlpriv.stats.txbytesunicast += len(hdr)
    return rtl92cu_tx_fill_desc(rtlpriv, bytes(hdr), info, tcb_desc)


def _rtl_usb_transmit(rtlpriv: RtlPriv, skb: bytes, qnum: int) -> tuple[int, bytes] | None:
    """usb.c:844: the bulk-OUT endpoint and URB payload, or None when the skb is dropped."""
    if rtlpriv.usb.state == C.USB_STATE_STOP:
        logger.error("USB device is stopping...")
        return None
    ep_num = rtlpriv.usb.ep_mapping[qnum]
    return ep_num, skb


def rtl_usb_tx(rtlpriv: RtlPriv, skb: bytes, info: Ieee80211TxInfo) -> tuple[int, bytes] | None:
    """usb.c:904 (rtl_usb_tx_chk_waitq_insert is always false)."""
    if is_hal_stop(rtlpriv.rtlhal):
        return None
    fc = int.from_bytes(skb[0:2], "little")
    hw_queue = rtl8192cu_mq_to_hwq(fc, info.queue_mapping)
    skb = _rtl_usb_tx_preprocess(rtlpriv, skb, info, hw_queue)
    return _rtl_usb_transmit(rtlpriv, skb, hw_queue)
