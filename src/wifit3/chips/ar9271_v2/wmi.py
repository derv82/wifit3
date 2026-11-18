"""WMI command transport + the register-I/O-over-WMI mechanism.

ath9k_htc drives all MAC/BB/RF register access through WMI commands on the WMI control
endpoint. This module is the *mechanism* (ported from wmi.c + htc_drv_init.c); the init
sequence that drives it (which registers, in what order, with which writes batched) is
ported separately and calls these primitives.

Command wire format [SRC] wmi.c:286-307 + htc_hst.c:21-38:
    htc_frame_hdr(endpoint_id=ctrl_epid) | wmi_cmd_hdr(be16 command_id, be16 seq_no) | payload
sent out the REG_OUT interrupt pipe; the response returns on REG_IN with the same framing,
its body being the value(s). Events (command_id & 0x1000) are delivered out-of-band:
ctrl_rx passes them to event_tasklet [SRC] wmi.c:233-239.

Register access [SRC] htc_drv_init.c:234-509:
    REG_READ   payload be32(addr)                  -> be32(val)
    REG_WRITE  single  be32(addr), be32(val)
               multi   [be32 reg, be32 val] x N     (batched between enable/flush)
    REG_RMW    single  be32(addr), be32(set), be32(clr)
               multi   [be32 reg, be32 set, be32 clr] x N
"""
from __future__ import annotations

import errno
import functools
import logging
import struct
import threading
import time
from typing import Callable

import usb.core

from wifit3.errors import is_device_gone

from . import htc
from .transport import AR9271Transport

logger = logging.getLogger(__name__)

WMI_GET_FW_VERSION_CMDID = 0x0003
WMI_DISABLE_INTR_CMDID = 0x0004
WMI_ENABLE_INTR_CMDID = 0x0005
WMI_ATH_INIT_CMDID = 0x0006
WMI_DRAIN_TXQ_ALL_CMDID = 0x000b
WMI_START_RECV_CMDID = 0x000c
WMI_STOP_RECV_CMDID = 0x000d
WMI_FLUSH_RECV_CMDID = 0x000e
WMI_SET_MODE_CMDID = 0x000f
WMI_NODE_CREATE_CMDID = 0x0010
WMI_VAP_CREATE_CMDID = 0x0013
WMI_REG_READ_CMDID = 0x0014
WMI_REG_WRITE_CMDID = 0x0015
WMI_TARGET_IC_UPDATE_CMDID = 0x0018
WMI_REG_RMW_CMDID = 0x0020

# htc opmodes [SRC] htc.h:57-62
HTC_M_STA = 1
HTC_M_IBSS = 0
HTC_M_HOSTAP = 6
HTC_M_MONITOR = 8
HTC_M_WDS = 2

WMI_EVENT_BIT = 0x1000           # command_id & 0x1000 => async event, not a cmd response

MAX_CMD_NUMBER = 62              # multi-write batch cap [SRC] wmi.h:128
MAX_RMW_CMD_NUMBER = 15          # multi-rmw batch cap   [SRC] wmi.h:129

REG_TIMEOUT_MS = 400             # htc_drv_init.c:245 ath9k_wmi_cmd(..., 100): 100 jiffies at CONFIG_HZ=250
WMI_CMD_TIMEOUT_MS = 2000        # wmi.h:198, :205 HZ*2


class WMI:
    """One WMI control channel over an HTC endpoint. Stateful: the sequence counter and the
    register-write / rmw batch buffers mirror ``struct wmi`` [SRC] wmi.h:140-176."""

    def __init__(self, t: AR9271Transport, ctrl_epid: int):
        self.t = t
        self.ctrl_epid = ctrl_epid
        self.tx_seq_id = 0
        self.last_seq_id = 0
        self.cmd_rsp_buf = b""
        self.cmd_wait = threading.Event()
        self._command_lock = threading.RLock()
        self._wmi_lock = threading.Lock()
        self._mwrite: list[tuple[int, int]] = []          # buffered (reg, val)
        self._mwrite_enabled = 0
        self._mrmw: list[tuple[int, int, int]] = []        # buffered (reg, set, clr)
        self._mrmw_enabled = 0
        # Verify-harness-only hook: reproduce async-producer ops (the LED-blink timer's GPIO
        # writes) the kernel interleaved into the command stream, so the shared seq counter
        # stays aligned. None in production (the real LED is a timer). Called (command_id,
        # payload) at the head of every cmd, before the seq is taken.
        self.async_injector = None
        self.event_tasklet: Callable[[int, bytes], None] | None = None
        t.htc_rx_msg = functools.partial(htc.rx_msg, ctrl_epid=ctrl_epid, ctrl_rx=self.ctrl_rx)

    # ---- raw command / response -------------------------------------------
    def cmd(self, command_id: int, payload: bytes, timeout_ms: int = WMI_CMD_TIMEOUT_MS) -> bytes:
        """ath9k_wmi_cmd: send one command and return its response body. A failure raises the USBError
        where the C returns an error code; no reply within ``timeout_ms`` raises USBTimeoutError."""
        with self._command_lock:
            if self.async_injector is not None:
                self.async_injector(command_id, payload)
            self.tx_seq_id = (self.tx_seq_id + 1) & 0xFFFF
            hdr = struct.pack(">HH", command_id, self.tx_seq_id)
            with self._wmi_lock:
                self.cmd_wait.clear()
                self.last_seq_id = self.tx_seq_id
            try:
                self.t.reg_out(htc.frame(self.ctrl_epid, hdr + payload))
            except usb.core.USBError:
                logger.debug("WMI failure for: 0x%04x", command_id)   # wmi.c:364
                raise
            if not self._wait_for_completion(timeout_ms):
                logger.debug("Timeout waiting for WMI command: 0x%04x", command_id)   # wmi.c:350
                with self._wmi_lock:
                    self.last_seq_id = 0
                if self.t.removed:
                    raise usb.core.USBError("device removed", errno=errno.ENODEV)
                raise usb.core.USBTimeoutError("WMI command timeout", errno=errno.ETIMEDOUT)
            return self.cmd_rsp_buf

    def ctrl_rx(self, body: bytes) -> None:
        """ath9k_wmi_ctrl_rx: the reply to the last command completes it; events go to
        event_tasklet; anything else is dropped."""
        if len(body) < 4:
            return
        cmd_id, seq = struct.unpack_from(">HH", body)
        if cmd_id & WMI_EVENT_BIT:
            if self.event_tasklet is not None:
                self.event_tasklet(cmd_id, body[4:])
            return
        with self._wmi_lock:
            if seq != self.last_seq_id:
                return
            self.cmd_rsp_buf = body[4:]
            self.cmd_wait.set()

    def _wait_for_completion(self, timeout_ms: int) -> bool:
        """wait_for_completion_timeout. With no REG_IN reader (the verify_pcap replay), read REG_IN
        here and pass each frame on as the reader would."""
        if self.t.reg_in_thread is not None:
            return self.cmd_wait.wait(timeout_ms / 1000)
        deadline = time.monotonic() + timeout_ms / 1000
        while not self.cmd_wait.is_set():
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                return False
            try:
                frame = self.t.reg_in(remaining_ms)
            except usb.core.USBError as e:
                if is_device_gone(e):
                    raise
                continue
            self.t.htc_rx_msg(frame)
        return True

    def vap_create(self, index: int, opmode: int, macaddr: bytes, ath_cap: int = 0,
                   rtsthreshold: int = 0) -> None:
        """WMI_VAP_CREATE [SRC] htc_drv_main.c — struct ath9k_htc_target_vif (12 bytes):
        index, opmode, myaddr[6], ath_cap, rtsthreshold(be16), pad."""
        hvif = struct.pack(">BB6sBHB", index, opmode, bytes(macaddr), ath_cap, rtsthreshold, 0)
        self.cmd(WMI_VAP_CREATE_CMDID, hvif)

    def node_create(self, macaddr: bytes, bssid: bytes, sta_index: int, vif_index: int,
                    is_vif_sta: int, maxampdu: int, flags: int = 0, htcap: int = 0) -> None:
        """WMI_NODE_CREATE [SRC] htc_drv_main.c — struct ath9k_htc_target_sta (22 bytes):
        macaddr[6], bssid[6], sta_index, vif_index, is_vif_sta, flags/htcap/maxampdu(be16), pad."""
        tsta = struct.pack(">6s6sBBBHHHB", bytes(macaddr), bytes(bssid), sta_index, vif_index,
                           is_vif_sta, flags, htcap, maxampdu, 0)
        self.cmd(WMI_NODE_CREATE_CMDID, tsta)

    def update_cap_target(self, tx_chainmask: int, enable_coex: int = 0) -> None:
        """ath9k_htc_update_cap_target [SRC] htc_drv_main.c:574 — push HW caps to the target:
        ampdu limit 0xffff, 0xff subframes, coex flag, tx chainmask (struct is 8 bytes incl.
        one tail pad byte)."""
        tcap = struct.pack(">IBBBB", 0xFFFF, 0xFF, enable_coex, tx_chainmask, 0)
        self.cmd(WMI_TARGET_IC_UPDATE_CMDID, tcap)

    def get_fw_version(self) -> tuple[int, int]:
        """WMI_GET_FW_VERSION — empty command, response is wmi_fw_version (be16 major, minor)
        [SRC] htc_drv_init.c:785 ath9k_init_firmware_version."""
        rsp = self.cmd(WMI_GET_FW_VERSION_CMDID, b"")
        major, minor = struct.unpack_from(">HH", rsp.ljust(4, b"\x00"))   # wmi.c:209-210
        return major, minor

    def cmd_or_none(self, command_id: int, payload: bytes,
                    timeout_ms: int = WMI_CMD_TIMEOUT_MS) -> bytes | None:
        """``cmd`` for the C callers that ignore ath9k_wmi_cmd's return: a failure other than
        device-gone returns None (its -ETIMEDOUT / error return [SRC] wmi.c:349-365)."""
        try:
            return self.cmd(command_id, payload, timeout_ms)
        except usb.core.USBError as e:
            if is_device_gone(e):
                raise
            return None

    # ---- register reads ---------------------------------------------------
    def reg_read(self, addr: int) -> int:
        """ath9k_regread: be32(addr) -> be32(val) [SRC] htc_drv_init.c:234-253."""
        rsp = self.cmd_or_none(WMI_REG_READ_CMDID, struct.pack(">I", addr), REG_TIMEOUT_MS)
        if rsp is None:
            logger.debug("REGISTER READ FAILED: (0x%04x)", addr)   # htc_drv_init.c:246-250
            return 0xFFFFFFFF
        return struct.unpack_from(">I", rsp.ljust(4, b"\x00"))[0]          # wmi.c:209-210

    def multi_reg_read(self, addrs: list[int]) -> list[int]:
        """ath9k_multi_regread: be32(addr) x N -> be32(val) x N [SRC] htc_drv_init.c:255-281."""
        rsp = self.cmd_or_none(WMI_REG_READ_CMDID, b"".join(struct.pack(">I", a) for a in addrs),
                               REG_TIMEOUT_MS)
        if rsp is None:
            # htc_drv_init.c:274-276; the C then returns its uninitialised tmpval
            logger.debug("Multiple REGISTER READ FAILED (count: %d)", len(addrs))
            return [0xFFFFFFFF] * len(addrs)
        n = 4 * len(addrs)
        return list(struct.unpack(">" + "I" * len(addrs), rsp.ljust(n, b"\x00")[:n]))

    # ---- register writes (single + buffered multi) ------------------------
    def reg_write(self, addr: int, val: int) -> None:
        """ath9k_regwrite: buffer if a write-buffer is open, else issue a single write
        [SRC] htc_drv_init.c:346-356."""
        if self._mwrite_enabled:
            self._mwrite.append((addr, val))
            if len(self._mwrite) == MAX_CMD_NUMBER:        # full -> flush [SRC] :340
                self._flush_writes()
        else:
            rsp = self.cmd_or_none(WMI_REG_WRITE_CMDID, struct.pack(">II", addr, val), REG_TIMEOUT_MS)
            if rsp is None:
                logger.debug("REGISTER WRITE FAILED:(0x%04x)", addr)   # htc_drv_init.c:317-320

    def enable_write_buffer(self) -> None:                 # ath9k_enable_regwrite_buffer
        self._mwrite_enabled += 1

    def write_flush(self) -> None:                         # ath9k_regwrite_flush
        self._mwrite_enabled -= 1
        if self._mwrite:
            self._flush_writes()

    def _flush_writes(self) -> None:
        """ath9k_regwrite_multi: one REG_WRITE carrying the whole batch [SRC] :283-300."""
        payload = b"".join(struct.pack(">II", reg, val) for reg, val in self._mwrite)
        count = len(self._mwrite)
        self._mwrite.clear()
        rsp = self.cmd_or_none(WMI_REG_WRITE_CMDID, payload, REG_TIMEOUT_MS)
        if rsp is None:
            logger.debug("REGISTER WRITE FAILED, multi len: %d", count)   # htc_drv_init.c:294-299

    # ---- register read-modify-write (single + buffered multi) -------------
    def reg_rmw(self, addr: int, set_bits: int, clr_bits: int) -> None:
        """ath9k_reg_rmw: buffer if an rmw-buffer is open, else a single RMW
        [SRC] htc_drv_init.c:383-509."""
        if self._mrmw_enabled:
            self._mrmw.append((addr, set_bits, clr_bits))
            if len(self._mrmw) == MAX_RMW_CMD_NUMBER:
                self._flush_rmw()
        else:
            rsp = self.cmd_or_none(WMI_REG_RMW_CMDID, struct.pack(">III", addr, set_bits, clr_bits),
                                   REG_TIMEOUT_MS)
            if rsp is None:
                logger.debug("REGISTER RMW FAILED:(0x%04x)", addr)   # htc_drv_init.c:483-486

    def enable_rmw_buffer(self) -> None:
        self._mrmw_enabled += 1

    def rmw_flush(self) -> None:
        self._mrmw_enabled -= 1
        if self._mrmw:
            self._flush_rmw()

    def _flush_rmw(self) -> None:
        payload = b"".join(struct.pack(">III", reg, s, c) for reg, s, c in self._mrmw)
        count = len(self._mrmw)
        self._mrmw.clear()
        rsp = self.cmd_or_none(WMI_REG_RMW_CMDID, payload, REG_TIMEOUT_MS)
        if rsp is None:
            logger.debug("REGISTER RMW FAILED, multi len: %d", count)   # htc_drv_init.c:443-448
