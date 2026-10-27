"""rtlwifi usb.c: probe and adapter start."""
from __future__ import annotations

import errno
from dataclasses import dataclass

from . import constants as C
from .base import rtl_init_rx_config
from .hw import rtl92cu_read_eeprom_info
from .mac import rtl92c_read_chip_version
from .priv import RtlPriv
from .sw import rtl92cu_init_sw_vars
from .trx import rtl8192cu_endpoint_mapping
from .wifi import set_hal_start

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


def rtl_usb_start(rtlpriv: RtlPriv) -> int:
    """usb.c:681. _rtl_usb_receive (bulk-IN URBs) belongs to the RX path, not here."""
    err = rtlpriv.cfg.ops.hw_init(rtlpriv)
    if not err:
        rtl_init_rx_config(rtlpriv)
        set_hal_start(rtlpriv.rtlhal)
    return err
