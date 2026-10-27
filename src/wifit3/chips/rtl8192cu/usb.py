"""rtlwifi usb.c probe path and rtl8192cu/sw.c init_sw_vars."""
from __future__ import annotations

import errno
from dataclasses import dataclass

from . import constants as C
from .hw import rtl92cu_read_eeprom_info
from .mac import IS_81XXC_VENDOR_UMC_B_CUT, IS_92C_SERIAL, IS_VENDOR_UMC_A_CUT, rtl92c_read_chip_version
from .priv import RtlPriv
from .trx import rtl8192cu_endpoint_mapping

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


def rtl92cu_init_sw_vars(rtlpriv: RtlPriv) -> None:
    """rtl8192cu/sw.c:31; the firmware itself loads in hw_init."""
    rtlpriv.dm.dm_initialgain_enable = True
    rtlpriv.dm.dm_flag = 0
    rtlpriv.dm.disable_framebursting = False
    rtlpriv.dm.thermalvalue = 0
    version = rtlpriv.rtlhal.version
    if IS_VENDOR_UMC_A_CUT(version) and not IS_92C_SERIAL(version):
        rtlpriv.rtlhal.fw_name = "rtl8192cufw_A.bin"
    elif IS_81XXC_VENDOR_UMC_B_CUT(version):
        rtlpriv.rtlhal.fw_name = "rtl8192cufw_B.bin"
    else:
        rtlpriv.rtlhal.fw_name = "rtl8192cufw_TMSC.bin"


def rtl_usb_probe(rtlpriv: RtlPriv, endpoints: list[UsbEndpointDescriptor], is_high_speed: bool) -> None:
    """usb.c:951, the register-touching steps: chip version, EFUSE, endpoint config."""
    rtl92c_read_chip_version(rtlpriv)
    rtl92cu_read_eeprom_info(rtlpriv)
    err = _rtl_usb_init(rtlpriv, endpoints, is_high_speed)
    if err:
        raise ProbeError(err, "_rtl_usb_init failed")
    rtl92cu_init_sw_vars(rtlpriv)
