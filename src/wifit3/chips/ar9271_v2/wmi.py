"""WMI command transport + the register-I/O-over-WMI mechanism.

ath9k_htc drives all MAC/BB/RF register access through WMI commands on the WMI control
endpoint. This module is the *mechanism* (ported from wmi.c + htc_drv_init.c); the init
sequence that drives it (which registers, in what order, with which writes batched) is
ported separately and calls these primitives.

Command wire format [SRC] wmi.c:286-307 + htc_hst.c:21-38:
    htc_frame_hdr(endpoint_id=ctrl_epid) | wmi_cmd_hdr(be16 command_id, be16 seq_no) | payload
sent out the REG_OUT interrupt pipe; the response returns on REG_IN with the same framing,
its body being the value(s). Events (command_id & 0x1000) are delivered out-of-band and are
skipped here while waiting for a command response [SRC] wmi.c:233.

Register access [SRC] htc_drv_init.c:234-509:
    REG_READ   payload be32(addr)                  -> be32(val)
    REG_WRITE  single  be32(addr), be32(val)
               multi   [be32 reg, be32 val] x N     (batched between enable/flush)
    REG_RMW    single  be32(addr), be32(set), be32(clr)
               multi   [be32 reg, be32 set, be32 clr] x N
"""
from __future__ import annotations

import struct

from . import constants as C
from . import htc
from .transport import AR9271Transport

WMI_GET_FW_VERSION_CMDID = 0x0003
WMI_REG_READ_CMDID = 0x0014
WMI_REG_WRITE_CMDID = 0x0015
WMI_REG_RMW_CMDID = 0x0020

WMI_EVENT_BIT = 0x1000           # command_id & 0x1000 => async event, not a cmd response

MAX_CMD_NUMBER = 62              # multi-write batch cap [SRC] wmi.h:128
MAX_RMW_CMD_NUMBER = 15          # multi-rmw batch cap   [SRC] wmi.h:129


class WMI:
    """One WMI control channel over an HTC endpoint. Stateful: the sequence counter and the
    register-write / rmw batch buffers mirror ``struct wmi`` [SRC] wmi.h:140-176."""

    def __init__(self, t: AR9271Transport, ctrl_epid: int):
        self.t = t
        self.ctrl_epid = ctrl_epid
        self.tx_seq_id = 0
        self._mwrite: list[tuple[int, int]] = []          # buffered (reg, val)
        self._mwrite_enabled = 0
        self._mrmw: list[tuple[int, int, int]] = []        # buffered (reg, set, clr)
        self._mrmw_enabled = 0

    # ---- raw command / response -------------------------------------------
    def cmd(self, command_id: int, payload: bytes) -> bytes:
        """Issue one WMI command and return its response body (htc + wmi headers stripped).
        Mirrors ath9k_wmi_cmd_issue: ++seq, frame, send on ctrl_epid [SRC] wmi.c:286-307."""
        self.tx_seq_id = (self.tx_seq_id + 1) & 0xFFFF
        hdr = struct.pack(">HH", command_id, self.tx_seq_id)
        self.t.reg_out(htc.frame(self.ctrl_epid, hdr + payload))
        return self._await_response(self.tx_seq_id)

    def _await_response(self, seq: int) -> bytes:
        """Read REG_IN frames until the command response for ``seq`` arrives, skipping any
        interleaved WMI events (command_id & 0x1000) [SRC] wmi.c:215-250."""
        while True:
            frame = self.t.reg_in()
            body = frame[C.HTC_FRAME_HDR_LEN:]
            cmd_id, rsp_seq = struct.unpack_from(">HH", body)
            if cmd_id & WMI_EVENT_BIT:
                continue                                   # async event — not our response
            return body[4:]                                # strip wmi_cmd_hdr -> value(s)

    def get_fw_version(self) -> tuple[int, int]:
        """WMI_GET_FW_VERSION — empty command, response is wmi_fw_version (be16 major, minor)
        [SRC] htc_drv_init.c:785 ath9k_init_firmware_version."""
        rsp = self.cmd(WMI_GET_FW_VERSION_CMDID, b"")
        major, minor = struct.unpack_from(">HH", rsp)
        return major, minor

    # ---- register reads ---------------------------------------------------
    def reg_read(self, addr: int) -> int:
        """ath9k_regread: be32(addr) -> be32(val) [SRC] htc_drv_init.c:234-253."""
        rsp = self.cmd(WMI_REG_READ_CMDID, struct.pack(">I", addr))
        return struct.unpack_from(">I", rsp)[0]

    def multi_reg_read(self, addrs: list[int]) -> list[int]:
        """ath9k_multi_regread: be32(addr) x N -> be32(val) x N [SRC] htc_drv_init.c:255-281."""
        rsp = self.cmd(WMI_REG_READ_CMDID, b"".join(struct.pack(">I", a) for a in addrs))
        return list(struct.unpack(">" + "I" * len(addrs), rsp[:4 * len(addrs)]))

    # ---- register writes (single + buffered multi) ------------------------
    def reg_write(self, addr: int, val: int) -> None:
        """ath9k_regwrite: buffer if a write-buffer is open, else issue a single write
        [SRC] htc_drv_init.c:346-356."""
        if self._mwrite_enabled:
            self._mwrite.append((addr, val))
            if len(self._mwrite) == MAX_CMD_NUMBER:        # full -> flush [SRC] :340
                self._flush_writes()
        else:
            self.cmd(WMI_REG_WRITE_CMDID, struct.pack(">II", addr, val))

    def enable_write_buffer(self) -> None:                 # ath9k_enable_regwrite_buffer
        self._mwrite_enabled += 1

    def write_flush(self) -> None:                         # ath9k_regwrite_flush
        self._mwrite_enabled -= 1
        if self._mwrite:
            self._flush_writes()

    def _flush_writes(self) -> None:
        """ath9k_regwrite_multi: one REG_WRITE carrying the whole batch [SRC] :283-300."""
        payload = b"".join(struct.pack(">II", reg, val) for reg, val in self._mwrite)
        self._mwrite.clear()
        self.cmd(WMI_REG_WRITE_CMDID, payload)

    # ---- register read-modify-write (single + buffered multi) -------------
    def reg_rmw(self, addr: int, set_bits: int, clr_bits: int) -> None:
        """ath9k_reg_rmw: buffer if an rmw-buffer is open, else a single RMW
        [SRC] htc_drv_init.c:383-509."""
        if self._mrmw_enabled:
            self._mrmw.append((addr, set_bits, clr_bits))
            if len(self._mrmw) == MAX_RMW_CMD_NUMBER:
                self._flush_rmw()
        else:
            self.cmd(WMI_REG_RMW_CMDID, struct.pack(">III", addr, set_bits, clr_bits))

    def enable_rmw_buffer(self) -> None:
        self._mrmw_enabled += 1

    def rmw_flush(self) -> None:
        self._mrmw_enabled -= 1
        if self._mrmw:
            self._flush_rmw()

    def _flush_rmw(self) -> None:
        payload = b"".join(struct.pack(">III", reg, s, c) for reg, s, c in self._mrmw)
        self._mrmw.clear()
        self.cmd(WMI_REG_RMW_CMDID, payload)
