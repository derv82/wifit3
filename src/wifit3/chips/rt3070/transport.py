"""Chip-access transport for the Ralink RT3070.

Two layers, both ported line-by-line from the mainline kernel and confirmed
against this card's cold-boot capture by ``scripts/verify_pcap.py rt3070``:

* the **rt2x00usb USB layer** [SRC rt2x00usb.c / rt2x00usb.h] — the vendor
  control-transfer register access (``register_read``/``register_write`` and
  their multi-byte / busy-poll / device-mode siblings); and
* the **rt2800 indirect-register layer** [SRC rt2800lib.c] — BBP/RFCSR/MCU
  access and the small wait/disable helpers built on top of the USB layer.

The kernel keeps these in two files (rt2x00usb.c is the bus, rt2800lib.c the
chip). We co-locate them on one object because every bring-up module is handed
this one ``t`` and talks to the chip only through it — a thin shared base for a
future rt2x00 family extraction. The object is transport-agnostic: ``dev`` is a
real ``usb.core.Device`` at runtime and a replay shim under the pcap gate, and
the same code drives both.

Wire format (rt2x00usb, NOT Realtek's bRequest 0x05): the register *address*
rides in ``wIndex`` and ``wValue`` is 0; ``bRequest`` 6/7 = MULTI write/read,
1 = DEVICE_MODE. A 4-byte register access is a single MULTI transfer; anything
longer (the 4 KB firmware blob) is chunked to ``CSR_CACHE_SIZE`` bytes, exactly
as ``rt2x00usb_vendor_request_buff`` does.
"""
from __future__ import annotations

import errno
import logging
import os
import sys
import time

import usb.core

from ..log_trace import TRACE
from . import constants as C
from .constants import get_field, set_field

logger = logging.getLogger(__name__)

_LIBUSB_NO_DEVICE = -4        # LIBUSB_ERROR_NO_DEVICE
_TRANSPORT_FILE = os.path.normcase(__file__)


def _trace_caller() -> str:
    """First stack frame outside this transport module — the meaningful caller
    (``enable_radio_finish`` / ``config_channel`` / …) instead of an internal helper."""
    f = sys._getframe(1)
    while f is not None and os.path.normcase(f.f_code.co_filename) == _TRANSPORT_FILE:
        f = f.f_back
    return f.f_code.co_name if f is not None else "?"


def _trace_xfer(request, requesttype, value, index, data, result) -> None:
    """One TRACE line per USB control transfer: caller, op (R/W/DEV/EE), address, value.
    ``WIFIT3_LOG=trace`` only — the per-transfer preamble for diagnosing a wedge."""
    def _u32(b: bytes) -> str:
        b = bytes(b)
        return (f"=0x{int.from_bytes(b, 'little'):0{max(len(b) * 2, 2)}x}"
                if 0 < len(b) <= 4 else f"<{len(b)}B>")

    caller = _trace_caller()
    if request in (C.USB_MULTI_READ, C.USB_SINGLE_READ):
        logger.trace("[%-20s] R   0x%04x %s", caller, index, _u32(result))
    elif request in (C.USB_MULTI_WRITE, C.USB_SINGLE_WRITE):
        logger.trace("[%-20s] W   0x%04x %s", caller, index,
                     _u32(data) if not isinstance(data, int) else "")
    elif request == C.USB_DEVICE_MODE:
        logger.trace("[%-20s] DEV val=0x%04x idx=0x%04x %s", caller, value, index,
                     "(in)" if requesttype & 0x80 else "(out)")
    elif request == C.USB_EEPROM_READ:
        logger.trace("[%-20s] EEPROM <%dB>", caller, len(bytes(result)))


def _is_device_gone(err: usb.core.USBError) -> bool:
    """True only when the device is *truly* gone — retry everything else.

    Faithful to the kernel's intent (rt2x00usb_check_usb_error stops on a removed device)
    but mapped to libusb reality: only ``LIBUSB_ERROR_NO_DEVICE (-4)`` / ``ENODEV`` mean
    physically-gone. NOT ``LIBUSB_ERROR_NOT_FOUND (-5)`` / ``ENOENT`` — on Windows/WinUSB
    that's a *transient* stale-handle / mid-enumeration hiccup, not a removed device, so
    retrying it through the deadline is what recovers a warm bring-up (the kernel's literal
    ``-ENOENT`` is a URB-unlink, a different beast). Matches chips/mt76x0u's
    ``_is_fatal_usb_error``; unknown codes default to transient (the deadline bounds it)."""
    if getattr(err, "errno", None) == errno.ENODEV:
        return True
    return getattr(err, "backend_error_code", None) == _LIBUSB_NO_DEVICE


class RT3070Transport:
    def __init__(self, dev: usb.core.Device, timeout_ms: int = C.REGISTER_TIMEOUT_FIRMWARE):
        self.dev = dev
        self.timeout_ms = timeout_ms

    # =====================================================================
    # rt2x00usb USB layer  [SRC rt2x00usb.c, rt2x00usb.h]
    # =====================================================================
    def _vendor_request(self, requesttype, request, value, index, data_or_length):
        """One vendor control transfer, with the kernel's transient-error retry
        [SRC rt2x00usb.c:45-80 rt2x00usb_vendor_request]: ``do { usb_control_msg }
        while (time_before(jiffies, expire))`` — retry on ANY USB error except
        'device gone', each attempt at half the timeout, until the wall-clock
        deadline. This is what recovers a warm/replug bring-up where the chip's
        control endpoint briefly NAKs/stalls mid-firmware-boot (the extra boot-signal
        write seen in capture-2/3). Under the pcap gate the replay never errors, so
        the loop runs exactly once and the gate is unaffected."""
        per_attempt = max(1, self.timeout_ms // 2)        # kernel uses timeout/2 per try
        deadline = time.monotonic() + self.timeout_ms / 1000.0
        attempt = 0
        while True:
            attempt += 1
            try:
                result = self.dev.ctrl_transfer(requesttype, request, value, index,
                                                data_or_length, per_attempt)
                if attempt > 1:
                    logger.info("rt3070: vendor req 0x%02x off 0x%04x recovered on "
                                "attempt %d", request, index, attempt)
                if logger.isEnabledFor(TRACE):
                    _trace_xfer(request, requesttype, value, index, data_or_length, result)
                return result
            except usb.core.USBError as e:
                if _is_device_gone(e) or time.monotonic() >= deadline:
                    if attempt > 1:
                        logger.warning("rt3070: vendor req 0x%02x off 0x%04x failed after "
                                       "%d attempts: %s", request, index, attempt, e)
                    raise
                time.sleep(0.001)                          # avoid a busy-spin on fast-fail

    def register_multiread(self, offset: int, length: int) -> bytes:
        """MULTI_READ chunked to CSR_CACHE_SIZE [SRC rt2x00usb.c:114-143
        rt2x00usb_vendor_request_buff]."""
        out = bytearray()
        off = offset
        remaining = length
        while remaining > 0:
            bsize = min(C.CSR_CACHE_SIZE, remaining)
            data = self._vendor_request(C.USB_VENDOR_REQUEST_IN, C.USB_MULTI_READ,
                                        0, off, bsize)
            out += bytes(data)
            off += bsize
            remaining -= bsize
        return bytes(out)

    def register_multiwrite(self, offset: int, data: bytes) -> None:
        data = bytes(data)
        off = offset
        pos = 0
        remaining = len(data)
        while remaining > 0:
            bsize = min(C.CSR_CACHE_SIZE, remaining)
            self._vendor_request(C.USB_VENDOR_REQUEST_OUT, C.USB_MULTI_WRITE,
                                 0, off, data[pos:pos + bsize])
            off += bsize
            pos += bsize
            remaining -= bsize

    def register_read(self, offset: int) -> int:
        """32-bit register read [SRC rt2x00usb.h:186-194 rt2x00usb_register_read]."""
        return int.from_bytes(self.register_multiread(offset, 4), "little")

    def register_write(self, offset: int, value: int) -> None:
        """32-bit register write [SRC rt2x00usb.h:242-250 rt2x00usb_register_write]."""
        self.register_multiwrite(offset, (value & 0xFFFFFFFF).to_bytes(4, "little"))

    def regbusy_read(self, offset: int, field_mask: int,
                     count: int = C.REGISTER_USB_BUSY_COUNT) -> tuple[bool, int]:
        """Poll ``offset`` until ``field_mask`` clears [SRC rt2x00usb.c:145-168
        rt2x00usb_regbusy_read]. Returns (not_busy, last_value)."""
        reg = 0
        for _ in range(count):
            reg = self.register_read(offset)
            if not get_field(reg, field_mask):
                return True, reg
            # kernel udelay(REGISTER_BUSY_DELAY) here; replay/HW needs no spin
        return False, 0xFFFFFFFF

    def eeprom_read(self, length: int) -> bytes:
        """One-shot EEPROM read (wValue=wIndex=0) [SRC rt2x00usb.h:170-176].

        #TODO untestable: this AWUS036NH is an EFUSE card, so the chip never
        takes the USB_EEPROM_READ path (see eeprom.read_eeprom_efuse). Ported
        faithfully for a future 93C66-EEPROM rt2x00 member; no wire exercises it.
        """
        return bytes(self._vendor_request(C.USB_VENDOR_REQUEST_IN, C.USB_EEPROM_READ,
                                          0, 0, length))

    def autorun_detect(self) -> int:
        """1 if the NIC is in AutoRun mode (skip FW upload), else 0 [SRC
        rt2800usb.c:176-203 rt2800usb_autorun_detect].

        Uses USB_DEVICE_MODE with the magic USB_MODE_AUTORUN in wValue — a
        different request than register_read, so it cannot be expressed through
        it.
        """
        data = self._vendor_request(C.USB_VENDOR_REQUEST_IN, C.USB_DEVICE_MODE,
                                    C.USB_MODE_AUTORUN, 0, 4)
        fw_mode = int.from_bytes(bytes(data), "little")
        return 1 if (fw_mode & 0x00000003) == 2 else 0

    def device_mode_sw(self, value: int, offset: int = 0) -> None:
        """USB_DEVICE_MODE write with no data phase — the FW-load and reset
        signals [SRC rt2x00usb.h:149-158 rt2x00usb_vendor_request_sw]."""
        self._vendor_request(C.USB_VENDOR_REQUEST_OUT, C.USB_DEVICE_MODE,
                             value, offset, b"")

    # =====================================================================
    # rt2800 indirect-register layer  [SRC rt2800lib.c]
    # =====================================================================
    def bbp_write(self, word: int, value: int) -> None:
        """[SRC rt2800lib.c:83-106 rt2800_bbp_write]"""
        ok, reg = self.regbusy_read(C.BBP_CSR_CFG, C.BBP_CSR_CFG_BUSY)
        if ok:
            reg = 0
            reg = set_field(reg, C.BBP_CSR_CFG_VALUE, value)
            reg = set_field(reg, C.BBP_CSR_CFG_REGNUM, word)
            reg = set_field(reg, C.BBP_CSR_CFG_BUSY, 1)
            reg = set_field(reg, C.BBP_CSR_CFG_READ_CONTROL, 0)
            reg = set_field(reg, C.BBP_CSR_CFG_BBP_RW_MODE, 1)
            self.register_write(C.BBP_CSR_CFG, reg)

    def bbp_read(self, word: int) -> int:
        """[SRC rt2800lib.c:108-140 rt2800_bbp_read]"""
        ok, reg = self.regbusy_read(C.BBP_CSR_CFG, C.BBP_CSR_CFG_BUSY)
        if ok:
            reg = 0
            reg = set_field(reg, C.BBP_CSR_CFG_REGNUM, word)
            reg = set_field(reg, C.BBP_CSR_CFG_BUSY, 1)
            reg = set_field(reg, C.BBP_CSR_CFG_READ_CONTROL, 1)
            reg = set_field(reg, C.BBP_CSR_CFG_BBP_RW_MODE, 1)
            self.register_write(C.BBP_CSR_CFG, reg)
            ok, reg = self.regbusy_read(C.BBP_CSR_CFG, C.BBP_CSR_CFG_BUSY)
        return get_field(reg, C.BBP_CSR_CFG_VALUE)

    def rfcsr_write(self, word: int, value: int) -> None:
        """RFCSR write, default (non-MT7620) path [SRC rt2800lib.c:142-181
        rt2800_rfcsr_write]."""
        ok, reg = self.regbusy_read(C.RF_CSR_CFG, C.RF_CSR_CFG_BUSY)
        if ok:
            reg = 0
            reg = set_field(reg, C.RF_CSR_CFG_DATA, value)
            reg = set_field(reg, C.RF_CSR_CFG_REGNUM, word)
            reg = set_field(reg, C.RF_CSR_CFG_WRITE, 1)
            reg = set_field(reg, C.RF_CSR_CFG_BUSY, 1)
            self.register_write(C.RF_CSR_CFG, reg)

    def rfcsr_read(self, word: int) -> int:
        """RFCSR read, default (non-MT7620) path [SRC rt2800lib.c:223-273
        rt2800_rfcsr_read]."""
        ok, reg = self.regbusy_read(C.RF_CSR_CFG, C.RF_CSR_CFG_BUSY)
        if ok:
            reg = 0
            reg = set_field(reg, C.RF_CSR_CFG_REGNUM, word)
            reg = set_field(reg, C.RF_CSR_CFG_WRITE, 0)
            reg = set_field(reg, C.RF_CSR_CFG_BUSY, 1)
            self.register_write(C.RF_CSR_CFG, reg)
            ok, reg = self.regbusy_read(C.RF_CSR_CFG, C.RF_CSR_CFG_BUSY)
        return get_field(reg, C.RF_CSR_CFG_DATA)

    def rfcsr_write_bank(self, bank: int, reg: int, value: int) -> None:
        """[SRC rt2800lib.c:183-187 rt2800_rfcsr_write_bank]"""
        self.rfcsr_write(reg | (bank << 6), value)

    def mcu_request(self, command: int, token: int, arg0: int, arg1: int) -> None:
        """Host->MCU mailbox command [SRC rt2800lib.c:515-546 rt2800_mcu_request].

        Note the OWNER-clear poll's last value is carried into the mailbox
        write (the kernel does not zero ``reg`` before setting the fields).
        """
        ok, reg = self.regbusy_read(C.H2M_MAILBOX_CSR, C.H2M_MAILBOX_CSR_OWNER)
        if ok:
            reg = set_field(reg, C.H2M_MAILBOX_CSR_OWNER, 1)
            reg = set_field(reg, C.H2M_MAILBOX_CSR_CMD_TOKEN, token)
            reg = set_field(reg, C.H2M_MAILBOX_CSR_ARG0, arg0)
            reg = set_field(reg, C.H2M_MAILBOX_CSR_ARG1, arg1)
            self.register_write(C.H2M_MAILBOX_CSR, reg)

            reg = 0
            reg = set_field(reg, C.HOST_CMD_CSR_HOST_COMMAND, command)
            self.register_write(C.HOST_CMD_CSR, reg)

    def wait_csr_ready(self) -> bool:
        """Wait for stable MAC_CSR0 [SRC rt2800lib.c:549-563 rt2800_wait_csr_ready].
        Returns True on ready (kernel returns 0)."""
        for _ in range(C.REGISTER_BUSY_COUNT):
            reg = self.register_read(C.MAC_CSR0)
            if reg and reg != 0xFFFFFFFF:
                return True
        return False

    def wait_wpdma_ready(self) -> bool:
        """Wait for WPDMA TX/RX idle [SRC rt2800lib.c:566-587
        rt2800_wait_wpdma_ready]. Returns True on ready."""
        for _ in range(C.REGISTER_BUSY_COUNT):
            reg = self.register_read(C.WPDMA_GLO_CFG)
            if (not get_field(reg, C.WPDMA_GLO_CFG_TX_DMA_BUSY)
                    and not get_field(reg, C.WPDMA_GLO_CFG_RX_DMA_BUSY)):
                return True
        return False

    def wait_bbp_rf_ready(self) -> bool:
        """Wait for BBP/RF not-busy in MAC_STATUS_CFG [SRC rt2800lib.c:2225-2241
        rt2800_wait_bbp_rf_ready]. Returns True on ready."""
        for _ in range(C.REGISTER_BUSY_COUNT):
            reg = self.register_read(C.MAC_STATUS_CFG)
            if not get_field(reg, C.MAC_STATUS_CFG_BBP_RF_BUSY):
                return True
        return False

    def wait_bbp_ready(self) -> bool:
        """Reactivate + wait for the BBP after FW load [SRC rt2800lib.c:2243-2265
        rt2800_wait_bbp_ready]. Returns True once BBP0 reads a sane value."""
        self.register_write(C.H2M_BBP_AGENT, 0)
        self.register_write(C.H2M_MAILBOX_CSR, 0)
        for _ in range(C.REGISTER_BUSY_COUNT):
            value = self.bbp_read(0)
            if value != 0xFF and value != 0x00:
                return True
        return False

    def disable_wpdma(self) -> None:
        """[SRC rt2800lib.c:589-600 rt2800_disable_wpdma]"""
        reg = self.register_read(C.WPDMA_GLO_CFG)
        reg = set_field(reg, C.WPDMA_GLO_CFG_ENABLE_TX_DMA, 0)
        reg = set_field(reg, C.WPDMA_GLO_CFG_TX_DMA_BUSY, 0)
        reg = set_field(reg, C.WPDMA_GLO_CFG_ENABLE_RX_DMA, 0)
        reg = set_field(reg, C.WPDMA_GLO_CFG_RX_DMA_BUSY, 0)
        reg = set_field(reg, C.WPDMA_GLO_CFG_TX_WRITEBACK_DONE, 1)
        self.register_write(C.WPDMA_GLO_CFG, reg)
