"""RTL8821CU USB transport — Realtek vendor control transfers + the 8821c
register-page-switch mirror.

Every register access is a bRequest 0x05 vendor control transfer, tried up to
MAX_USBCTRL_VENDORREQ_TIMES times (usbctrl_vendorreq): read uses
bmRequestType 0xC0, write 0x40, the 16-bit register address in wValue, wIndex 0.
[SRC] include/usb_ops.h:19-22, os_dep/linux/usb_ops_linux.c:26-260.

8821c quirk reproduced here: usbctrl_vendorreq() follows every access to an
ON-section register (addr <= 0xFF or 0x1000..0x10FF) with an extra 1-byte write
to 0x4E0 carrying the low byte of the IO buffer — the read-back value for a read,
the written value for a write. [SRC] os_dep/linux/usb_ops_linux.c:171-201. This
lives at the transport layer (below the HAL), so it is dev-centric: it calls
``dev.ctrl_transfer`` directly, which lets ``scripts/rtw88_pcap_replay.ReplayDevice``
drive the real transport unchanged and byte-check the mirror against the capture.

Shares no code with the other Realtek transports by design (anti-DRY); the
mechanism is identical but the copy is independent so a change here can't regress
a sibling card.
"""
from __future__ import annotations

import errno
import logging

import usb.core
import usb.util

from wifit3.errors import is_device_gone

from .rx import RXDESC_SIZE
from .constants import (
    MAX_VENDOR_REQ_CMD_SIZE,
    ON_SEC_RANGES,
    REALTEK_USB_VENQT_CMD_IDX,
    REALTEK_USB_VENQT_CMD_REQ,
    REALTEK_USB_VENQT_READ,
    REALTEK_USB_VENQT_WRITE,
    REG_PAGE_SWITCH_CONFIRM,
)

logger = logging.getLogger(__name__)

CTRL_TIMEOUT_MS = 500  # [SRC] include/usb_ops_linux.h:22
MAX_USBCTRL_VENDORREQ_TIMES = 10  # [SRC] include/usb_ops_linux.h:29-31, autoconf.h:233-234
MAX_CONTINUAL_IO_ERR = 4          # [SRC] include/rtw_io.h:265-266
FW_START_ADDRESS = 0x1000         # [SRC] include/usb_ops_linux.h:19
RX_BUF_SIZE = 0x8000
RX_TIMEOUT_MS = 200
_BULK_TIMEOUT_MS = 1000
_WIFI_INTF_CLASS = 0xFF         # vendor-specific = the WiFi interface (intf 2); 0xE0 = the BT side


def _is_on_sec(addr: int) -> bool:
    """ON-section register: the page-switch confirm fires only for these.
    [SRC] os_dep/linux/usb_ops_linux.c:172-178 (value<0xFE00: <=0xFF or 0x1000..0x10FF)."""
    return any(lo <= addr <= hi for lo, hi in ON_SEC_RANGES)


class Rtl8821cuTransport:
    def __init__(self, dev: usb.core.Device, bulk_out_ep: int = 0x04):
        self.dev = dev
        self._bulk_out_ep = bulk_out_ep
        self._in_ep = None  # bulk-IN (RX) endpoint, probed lazily
        # hal->LastHMEBoxNum: the HMEBOX index rtw_halmac_send_h2c rotates through (mod 4),
        # reset to 0 after each FW download. [SRC] hal_halmac.c:4128.
        self.last_hme_box = 0
        # hal->current_band_type: the band the last channel tune left the RF in (None until the
        # first tune switches it). need_switch_band keys the band-switch sub-step off it.
        self.current_band = None
        # *dm->channel: the channel of the last tune (None until the first). The halrf thermal
        # callback reads it to pick the per-band delta-swing table.
        self.current_channel = None
        # cali_info reset pending: a band switch ran phy_set_bb_swing -> odm_clear_txpowertracking_
        # state, which resets the thermal-tracking baseline; the next thermal callback applies it.
        self.thermal_reset_pending = False
        # dm->cck_new_agc: the CCK-AGC report-format latch (0xa9c[17]) phydm reads once at
        # odm_dm_init; the RX RSSI decode picks the CCK formula on it. Set by dm.py at init.
        self.cck_new_agc = False
        # dm->cck_agc_report_type: old-AGC CCK LNA-gain table selector (1 = 16-entry BTG table,
        # 0 = 8-entry table); phydm_cck_lna_bit_num_chk sets 1 iff default_rf_set==BTG. Set by
        # dm.py at init; defaults to 1 (the reference/BTG table). [SRC] phydm.c:180
        self.cck_agc_report_type = 1
        self.continual_io_error = 0
        self.surprise_removed = False

    # --- vendor control transfers (with the ON-section page-switch mirror) ---
    def usbctrl_vendorreq(self, reqtype: int, addr: int, pdata: bytearray) -> None:
        """[SRC] os_dep/linux/usb_ops_linux.c:26-205: up to 10 tries (one from 0x1000 up), each failure
        logged, then the 0x4E0 mirror. ENODEV or 5 failures in a row mark the device removed and raise."""
        if self.surprise_removed:  # RTW_CANNOT_IO, :58-61
            raise usb.core.USBError("surprise removed", errno=errno.ENODEV)
        value = addr & 0xFFFF
        is_read = reqtype == REALTEK_USB_VENQT_READ
        length = len(pdata)
        for vendorreq_times in range(1, MAX_USBCTRL_VENDORREQ_TIMES + 1):
            io_buf = bytearray(length) if is_read else bytearray(pdata)
            try:
                result = self.dev.ctrl_transfer(reqtype, REALTEK_USB_VENQT_CMD_REQ, value,
                                                REALTEK_USB_VENQT_CMD_IDX,
                                                length if is_read else pdata, CTRL_TIMEOUT_MS)
            except usb.core.USBError as e:
                status = -(e.errno or errno.EIO)
                if is_device_gone(e):
                    self.surprise_removed = True
            else:
                status = len(result) if is_read else result
                if is_read and status > 0:
                    io_buf[:status] = bytes(result)
                if status == length:
                    if is_read:
                        pdata[:] = io_buf
                    self.continual_io_error = 0
                    break
            if length in (1, 2, 4):
                logger.info("reg 0x%x, usb %s %u fail, status:%d value=0x%x, vendorreq_times:%d",
                            value, "read" if is_read else "write", length, status,
                            int.from_bytes(pdata, "little"), vendorreq_times)
            else:
                logger.info("reg 0x%x, usb %s %u fail, status:%d, vendorreq_times:%d", value,
                            "read" if is_read else "write", length, status, vendorreq_times)
            if is_read and status > 0:
                pdata[:] = io_buf
            self.continual_io_error += 1
            if self.continual_io_error > MAX_CONTINUAL_IO_ERR:
                logger.info("[ERROR] continual_io_error:%d > %d", self.continual_io_error,
                            MAX_CONTINUAL_IO_ERR)  # core/rtw_io.c:470
                self.surprise_removed = True
                break
            if value >= FW_START_ADDRESS:
                break
        if _is_on_sec(value):
            self._mirror(io_buf[0])
        if self.surprise_removed and status != length:
            raise usb.core.USBError("surprise removed", errno=errno.ENODEV)

    def _mirror(self, low_byte: int) -> None:
        """The post-access 1-byte write to 0x4E0. 0x4E0 is not itself ON-section,
        so it does not recurse. [SRC] os_dep/linux/usb_ops_linux.c:183-200."""
        try:
            status = self.dev.ctrl_transfer(REALTEK_USB_VENQT_WRITE, REALTEK_USB_VENQT_CMD_REQ,
                                            REG_PAGE_SWITCH_CONFIRM, REALTEK_USB_VENQT_CMD_IDX,
                                            bytes([low_byte & 0xFF]), CTRL_TIMEOUT_MS)
        except usb.core.USBError as e:
            status = -(e.errno or errno.EIO)
        if status == 1:
            self.continual_io_error = 0
        else:
            logger.info("reg 0x%x, usb write 1 fail, status:%d", REG_PAGE_SWITCH_CONFIRM, status)

    def _read(self, addr: int, length: int) -> bytes:
        pdata = bytearray(length)
        self.usbctrl_vendorreq(REALTEK_USB_VENQT_READ, addr, pdata)
        return bytes(pdata)

    def read8(self, addr: int) -> int:
        return int.from_bytes(self._read(addr, 1), "little")

    def read16(self, addr: int) -> int:
        return int.from_bytes(self._read(addr, 2), "little")

    def read32(self, addr: int) -> int:
        return int.from_bytes(self._read(addr, 4), "little")

    def writeN(self, addr: int, data: bytes) -> None:
        data = bytes(data)
        if len(data) > MAX_VENDOR_REQ_CMD_SIZE:
            raise ValueError(f"vendor write {len(data)} > {MAX_VENDOR_REQ_CMD_SIZE} B")
        self.usbctrl_vendorreq(REALTEK_USB_VENQT_WRITE, addr, bytearray(data))

    def write8(self, addr: int, val: int) -> None:
        self.writeN(addr, (val & 0xFF).to_bytes(1, "little"))

    def write16(self, addr: int, val: int) -> None:
        self.writeN(addr, (val & 0xFFFF).to_bytes(2, "little"))

    def write32(self, addr: int, val: int) -> None:
        self.writeN(addr, (val & 0xFFFFFFFF).to_bytes(4, "little"))

    # --- bulk OUT (FW/TX) / bulk IN (RX) -------------------------------------
    def bulk_out(self, data: bytes) -> None:
        """usb_write_port_not_xmitframe (FW download, H2C). [SRC] hal/rtl8821c/usb/rtl8821cu_halmac.c:57-128."""
        if self.surprise_removed:  # not in the C, which has no RTW_CANNOT check here
            raise usb.core.USBError("surprise removed", errno=errno.ENODEV)
        self.dev.write(self._bulk_out_ep, data, _BULK_TIMEOUT_MS)

    def usb_write_port(self, data: bytes) -> None:
        """TX: a failed write drops the frame. [SRC] os_dep/linux/usb_ops_linux.c:567-700."""
        if self.surprise_removed:  # RTW_CANNOT_TX, :581-589
            raise usb.core.USBError("surprise removed", errno=errno.ENODEV)
        try:
            self.dev.write(self._bulk_out_ep, data, _BULK_TIMEOUT_MS)
        except usb.core.USBError as e:
            logger.info("usb_write_port, status=%d", -(e.errno or errno.EIO))  # :673
            if is_device_gone(e):
                self.surprise_removed = True
                raise

    def _bulk_in_ep(self) -> int:
        if self._in_ep is not None:
            return self._in_ep
        cfg = self.dev.get_active_configuration()
        # This is a combo BT+WiFi card: the WiFi data path is the vendor-specific interface
        # (bInterfaceClass 0xFF); interfaces 0/1 are Bluetooth (0xE0) with their own bulk-IN
        # (0x82). Pick the WiFi interface's bulk-IN (0x84), never the BT one.
        for intf in cfg:
            if intf.bInterfaceClass != _WIFI_INTF_CLASS:
                continue
            for ep in intf:
                if (usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN
                        and usb.util.endpoint_type(ep.bmAttributes)
                        == usb.util.ENDPOINT_TYPE_BULK):
                    self._in_ep = ep.bEndpointAddress
                    return self._in_ep
        raise RuntimeError("RTL8821CU: no bulk-IN endpoint on the WiFi (vendor) interface")

    def bulk_in(self, size: int = RX_BUF_SIZE, timeout: int = RX_TIMEOUT_MS):
        """One blocking bulk-IN read. None on a benign timeout (quiet channel)."""
        if self.surprise_removed:  # RTW_CANNOT_RX. usb_ops_linux.c:1054
            raise usb.core.USBError("surprise removed", errno=errno.ENODEV)
        try:
            data = bytes(self.dev.read(self._bulk_in_ep(), size, timeout))
        except usb.core.USBTimeoutError:
            return None
        except usb.core.USBError as e:
            if (getattr(e, "errno", None) in (110, 10060)
                    or "timed out" in str(e).lower() or "timeout" in str(e).lower()):
                return None
            raise
        if len(data) >= RXDESC_SIZE:  # usb_read_port_complete. usb_ops_linux.c:987
            self.continual_io_error = 0
        return data

    def close(self) -> None:
        usb.util.dispose_resources(self.dev)
