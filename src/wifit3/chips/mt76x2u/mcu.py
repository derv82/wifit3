"""MT76x2U in-band MCU commands.

SPDX-License-Identifier: GPL-2.0-or-later
Ported from Linux mt76 (kernel v6.18) by wifit3, 2026.

Mirrors:
  - driver_sources/mt76-source-v6.18/mt76x02_usb_mcu.c       (__mt76x02u_mcu_send_msg,
                                                          mt76x02u_mcu_wait_resp)
  - driver_sources/mt76-source-v6.18/mt76x02_mcu.c           (function_select / radio_state)
  - driver_sources/mt76-source-v6.18/mt76x02_usb_core.c      (mt76x02u_skb_dma_info — TXINFO)

Wire format on EP 0x08 (MT_EP_OUT_INBAND_CMD):

    [4B TXINFO  ][ payload (round_up to 4) ][ 4B zero pad ]

TXINFO fields (little-endian 32-bit):
  bits 15:0   LEN          = round_up(payload_len, 4)
  bits 19:16  CMD_SEQ      = sequence (0 if wait_resp=False)
  bits 26:20  CMD_TYPE     = command opcode (CMD_FUN_SET_OP, ...)
  bits 29:27  DPORT        = CPU_TX_PORT = 2
  bit  30     TYPE_CMD     = 1
"""
from __future__ import annotations

import asyncio
import logging
import struct

from .constants import EP_IN_CMD_RESP, EP_OUT_INBAND_CMD
from .transport import MT76x2UTransport

logger = logging.getLogger(__name__)

# Kernel enums. [SRC] mt76x02_mcu.h:30
CMD_FUN_SET_OP        = 1
CMD_LOAD_CR           = 2
CMD_INIT_GAIN_OP      = 3
CMD_LED_MODE_OP       = 16
CMD_POWER_SAVING_OP   = 20
CMD_SWITCH_CHANNEL_OP = 30
CMD_CALIBRATION_OP    = 31

# mt76x2-specific calibration IDs. [SRC] mt76x2/mcu.h:27
# IMPORTANT: this differs from mt76x0 (where LC=3 and RXDCOC=2). We must use
# the mt76x2 ordering for MT7612U.
MCU_CAL_R           = 1
MCU_CAL_TEMP_SENSOR = 2
MCU_CAL_RXDCOC      = 3
MCU_CAL_RC          = 4
MCU_CAL_SX_LOGEN    = 5
MCU_CAL_LC          = 6
MCU_CAL_TX_LOFT     = 7
MCU_CAL_TXIQ        = 8
MCU_CAL_TSSI        = 9
MCU_CAL_TSSI_COMP   = 10
MCU_CAL_DPD         = 11
MCU_CAL_RXIQC_FI    = 12
MCU_CAL_RXIQC_FD    = 13
MCU_CAL_PWRON       = 14
MCU_CAL_TX_SHAPING  = 15

# enum mcu_function
Q_SELECT             = 1

# enum mcu_power_mode
RADIO_OFF            = 0x30
RADIO_ON             = 0x31

# Wire-protocol constants.
_TYPE_CMD            = 1 << 30
_DPORT_SHIFT         = 27
_CPU_TX_PORT         = 2
_CMD_TYPE_SHIFT      = 20
_CMD_SEQ_SHIFT       = 16
_EVT_CMD_DONE        = 0
_RX_FCE_INFO_EVT_TYPE_SHIFT = 20
_RX_FCE_INFO_EVT_TYPE_MASK  = 0xF
_RX_FCE_INFO_CMD_SEQ_SHIFT  = 16
_RX_FCE_INFO_CMD_SEQ_MASK   = 0xF

_MCU_RESP_URB_SIZE = 1024


class McuChannel:
    """Stateful MCU command channel.

    Wraps the transport in a sequence counter + (later) a response-drainer.
    For M2 we only fire commands that the kernel sends with `wait_resp=False`
    (FUN_SET_OP + POWER_SAVING_OP), so there is no response-matching loop
    yet — that lands when M5 / M7 start needing wait-for-ack commands.
    """

    def __init__(self, transport: MT76x2UTransport):
        self.transport = transport
        self._seq = 0  # rolled by _next_seq when wait_resp=True

    async def drain_response_queue(self, max_drain: int = 16,
                                    per_read_timeout_ms: int = 20) -> int:
        """Discard any stale responses sitting in EP_IN_CMD_RESP from a
        previous session.

        On warm reattach, the chip's MCU firmware is still running and may
        have a response left over from the prior session's last command.
        Without draining, our next wait_resp command sees the stale seq
        first, retries the read, and times out (the chip processed our new
        command but already-emitted its response to the cleared queue
        position). Symptom: ~1-in-10 warm boots fails on `mcu_load_cr`
        with "MCU resp seq mismatch: got=N want=1".

        Cold boot has nothing to drain — the first read times out
        immediately (~20 ms) and we return drained=0.

        Returns the count of stale responses discarded.
        """
        drained = 0
        for _ in range(max_drain):
            try:
                data = await self.transport.async_read_bulk(
                    EP_IN_CMD_RESP, _MCU_RESP_URB_SIZE,
                    timeout_ms=per_read_timeout_ms,
                )
            except Exception:
                # Timeout → queue empty.
                break
            if not data:
                break
            drained += 1
            if len(data) >= 4:
                rxfce = struct.unpack("<I", bytes(data[:4]))[0]
                got_seq = (rxfce >> _RX_FCE_INFO_CMD_SEQ_SHIFT) & _RX_FCE_INFO_CMD_SEQ_MASK
                got_evt = (rxfce >> _RX_FCE_INFO_EVT_TYPE_SHIFT) & _RX_FCE_INFO_EVT_TYPE_MASK
                logger.debug(
                    "MCU drained stale response: seq=%d evt=%d (%d bytes)",
                    got_seq, got_evt, len(data),
                )
        return drained

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xF
        if self._seq == 0:
            self._seq = (self._seq + 1) & 0xF
        return self._seq

    def _build_frame(self, cmd: int, payload: bytes, *, seq: int = 0) -> bytes:
        """Wrap a CMD payload for bulk-OUT EP 0x08."""
        rounded = (len(payload) + 3) & ~3
        # Zero-pad payload to 4-byte align before adding tail.
        if rounded > len(payload):
            payload = payload + b"\x00" * (rounded - len(payload))
        txinfo = (
            _TYPE_CMD
            | (_CPU_TX_PORT << _DPORT_SHIFT)
            | ((cmd & 0x7F) << _CMD_TYPE_SHIFT)
            | ((seq & 0xF) << _CMD_SEQ_SHIFT)
            | (rounded & 0xFFFF)
        )
        return struct.pack("<I", txinfo) + payload + b"\x00\x00\x00\x00"

    async def send(self, cmd: int, payload: bytes,
                   wait_resp: bool = False,
                   resp_timeout_ms: int = 500) -> bool:
        """Send an in-band MCU command. Returns True on success.

        Currently only `wait_resp=False` is exercised. The wait-for-ack
        path is wired through but not battle-tested — when M5+ start needing
        it, verify EVT_CMD_DONE parsing against a fresh pcap.
        """
        seq = self._next_seq() if wait_resp else 0
        frame = self._build_frame(cmd, payload, seq=seq)
        try:
            written = await self.transport.async_write_bulk(
                EP_OUT_INBAND_CMD, frame, timeout_ms=500,
            )
        except Exception as e:
            logger.error("MCU CMD cmd=%d send failed: %s", cmd, e)
            return False
        if written != len(frame):
            logger.error("MCU CMD cmd=%d short write %d/%d",
                         cmd, written, len(frame))
            return False
        logger.trace("MCU CMD cmd=%d seq=%d sent %d bytes", cmd, seq, len(frame))

        if not wait_resp:
            return True

        # Drain EP 0x85 until we get a frame whose RX_FCE_INFO matches
        # our seq and EVT_CMD_DONE.
        deadline = asyncio.get_running_loop().time() + resp_timeout_ms / 1000
        while True:
            now = asyncio.get_running_loop().time()
            if now >= deadline:
                logger.error("MCU CMD cmd=%d seq=%d response timeout", cmd, seq)
                return False
            timeout_ms = max(10, int((deadline - now) * 1000))
            try:
                data = await self.transport.async_read_bulk(
                    EP_IN_CMD_RESP, _MCU_RESP_URB_SIZE, timeout_ms=timeout_ms,
                )
            except Exception as e:
                logger.debug("MCU resp read error (will retry): %s", e)
                continue
            if not data or len(data) < 4:
                continue
            rxfce = struct.unpack("<I", bytes(data[:4]))[0]
            got_seq = (rxfce >> _RX_FCE_INFO_CMD_SEQ_SHIFT) & _RX_FCE_INFO_CMD_SEQ_MASK
            got_evt = (rxfce >> _RX_FCE_INFO_EVT_TYPE_SHIFT) & _RX_FCE_INFO_EVT_TYPE_MASK
            if got_seq != seq:
                logger.debug("MCU resp seq mismatch: got=%d want=%d (ignored)",
                             got_seq, seq)
                continue
            if got_evt != _EVT_CMD_DONE:
                logger.error("MCU CMD cmd=%d got evt=%d (not EVT_CMD_DONE)",
                             cmd, got_evt)
                return False
            return True


# ---------------------------------------------------------------------------
# High-level helpers.
# ---------------------------------------------------------------------------
async def function_select(mcu: McuChannel, func: int, value: int) -> bool:
    """[SRC] mt76x02_mcu.c:82 — `wait` is False only when func == Q_SELECT."""
    payload = struct.pack("<II", func, value)
    return await mcu.send(CMD_FUN_SET_OP, payload, wait_resp=(func != Q_SELECT))


async def set_radio_state(mcu: McuChannel, on: bool) -> bool:
    """[SRC] mt76x02_mcu.c:102 — fire-and-forget (no wait_resp)."""
    mode = RADIO_ON if on else RADIO_OFF
    payload = struct.pack("<II", mode, 0)
    return await mcu.send(CMD_POWER_SAVING_OP, payload, wait_resp=False)


async def mcu_init(mcu: McuChannel) -> bool:
    """[SRC] mt76x2/usb_mcu.c:246 — function_select(Q_SELECT,1) + radio_on."""
    if not await function_select(mcu, Q_SELECT, 1):
        return False
    return await set_radio_state(mcu, on=True)


async def mcu_calibrate(mcu: McuChannel, cal_id: int, value: int = 0,
                        timeout_ms: int = 1000) -> bool:
    """[SRC] mt76x02_mcu.c:117 (mt76x02_mcu_calibrate).

    Payload: `{ __le32 id; __le32 value; }` — 8 bytes total. CMD_CALIBRATION_OP
    waits for response (wait_resp=true in the kernel).
    """
    payload = struct.pack("<II", cal_id & 0xFFFFFFFF, value & 0xFFFFFFFF)
    return await mcu.send(CMD_CALIBRATION_OP, payload, wait_resp=True,
                          resp_timeout_ms=timeout_ms)


async def mcu_tssi_comp(mcu: McuChannel, pa_mode: int, cal_mode: int,
                        slope0: int, slope1: int,
                        offset0: int, offset1: int,
                        timeout_ms: int = 1000) -> bool:
    """`mt76x2_mcu_tssi_comp` — [SRC] mt76x2/mcu.c:94-108.

    Payload: `{ __le32 id; struct mt76x2_tssi_comp data; }` — id is the
    MCU_CAL_TSSI_COMP subcommand (10); data is 8 bytes packed:
      pa_mode(u8), cal_mode(u8), pad(u16=0), slope0(u8), slope1(u8),
      offset0(u8), offset1(u8). Sent on CMD_CALIBRATION_OP with wait_resp=True.
    """
    payload = struct.pack(
        "<I BBHBBBB",
        MCU_CAL_TSSI_COMP & 0xFFFFFFFF,
        pa_mode & 0xFF,
        cal_mode & 0xFF,
        0,
        slope0 & 0xFF,
        slope1 & 0xFF,
        offset0 & 0xFF,
        offset1 & 0xFF,
    )
    return await mcu.send(CMD_CALIBRATION_OP, payload, wait_resp=True,
                          resp_timeout_ms=timeout_ms)
