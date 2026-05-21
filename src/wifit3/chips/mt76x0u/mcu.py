"""MT76x0U MCU command channel (M2).

After FW upload (M1), most chip operations go through the in-band MCU
command channel instead of raw vendor xfers:
  - host -> chip: bulk-OUT on EP 0x08 (MT_EP_OUT_INBAND_CMD) with a 4-byte
    `info` header (PORT | CMD_TYPE | CMD_SEQ | TYPE_CMD | LEN) followed by
    the command payload, padded with 4 zero bytes at the end.
  - chip -> host: bulk-IN on EP 0x85 (MT_EP_IN_CMD_RESP), buffer up to
    MCU_RESP_URB_SIZE=1024 bytes. First 4 bytes = rxfce header carrying
    CMD_SEQ (matches request) + EVT_TYPE (0=EVT_CMD_DONE on success).

Ported from `mt76x02_usb_mcu.c::__mt76x02u_mcu_send_msg` and
`mt76x02u_mcu_wait_resp`. See [SRC] data_dumps/mt76-source-v6.18/.

Per [[feedback_prefer_fork_over_base]] this is a fresh port — no imports
from chips/mt76x2u/.
"""
from __future__ import annotations

import logging
import struct
import threading
from typing import Optional

import usb.core

from .constants import (
    CMD_FUN_SET_OP,
    CMD_RANDOM_READ,
    CMD_RANDOM_WRITE,
    CPU_TX_PORT,
    Q_SELECT,
    EP_IN_CMD_RESP,
    EP_OUT_INBAND_CMD,
    EVT_CMD_DONE,
    MCU_RESP_MAX_RETRY,
    MCU_RESP_TIMEOUT_MS,
    MCU_RESP_URB_SIZE,
    MCU_SEND_TIMEOUT_MS,
    MT_MCU_MSG_CMD_SEQ_SHIFT,
    MT_MCU_MSG_CMD_TYPE_SHIFT,
    MT_MCU_MSG_LEN_MASK,
    MT_MCU_MSG_PORT_SHIFT,
    MT_MCU_MSG_TYPE_CMD,
    MT_MCU_REG_PAIRS_PER_CMD,
    MT_RX_FCE_INFO_CMD_SEQ_MASK,
    MT_RX_FCE_INFO_CMD_SEQ_SHIFT,
    MT_RX_FCE_INFO_EVT_TYPE_MASK,
    MT_RX_FCE_INFO_EVT_TYPE_SHIFT,
)
from .transport import MT76x0UTransport

logger = logging.getLogger(__name__)


class MCUError(RuntimeError):
    """MCU command failed (timeout, sequence mismatch, EVT_CMD_ERROR)."""


class MCUChannel:
    """Send commands and read responses on the MT76x0U in-band MCU channel.

    Single-threaded by design — the kernel serializes via a mutex
    (`dev->mcu.mutex`). We use a `threading.Lock` for the same purpose so
    nothing races between two send_msg callers.
    """

    def __init__(self, transport: MT76x0UTransport):
        self.t = transport
        self._msg_seq = 0   # mirrors dev->mcu.msg_seq; 4-bit, never zero when used
        self._lock = threading.Lock()

    # ---- Sequence number management ---------------------------------
    def _next_seq(self) -> int:
        """Pre-increment and mask to 4 bits, skipping zero.

        Kernel: `seq = ++dev->mcu.msg_seq & 0xf; if (!seq) seq = ++... & 0xf;`
        Zero is reserved for "no-wait-resp" cmds, so a wait_resp seq must be 1-15.
        """
        self._msg_seq = (self._msg_seq + 1) & 0xF
        if self._msg_seq == 0:
            self._msg_seq = (self._msg_seq + 1) & 0xF
        return self._msg_seq

    # ---- Low-level send + receive -----------------------------------
    def _send(self, cmd: int, payload: bytes, wait_resp: bool) -> int:
        """Build + transmit one MCU bulk-OUT message. Returns the seq used
        (0 if wait_resp is False).

        Wire format per kernel `__mt76x02u_mcu_send_msg` + `mt76x02u_skb_dma_info`
        ([SRC] mt76x02_usb_mcu.c:69, mt76x02_usb_core.c:46-62):
          [4B info header][payload, padded to 4][4B zero tail]
        info LEN field = `round_up(payload_len, 4)` (not raw payload_len).
        info = (PORT << 27) | (CMD_TYPE << 20) | (CMD_SEQ << 16) | TYPE_CMD | LEN
        """
        seq = self._next_seq() if wait_resp else 0
        # Pad payload up to a 4-byte boundary, then add the 4-byte zero tail.
        # [SRC] mt76x02_usb_core.c:60 `pad = round_up(len, 4) + 4 - len`.
        aligned_len = (len(payload) + 3) & ~3
        pad_bytes = (aligned_len - len(payload)) + 4
        info = (
            (CPU_TX_PORT << MT_MCU_MSG_PORT_SHIFT)
            | (cmd << MT_MCU_MSG_CMD_TYPE_SHIFT)
            | (seq << MT_MCU_MSG_CMD_SEQ_SHIFT)
            | MT_MCU_MSG_TYPE_CMD
            | (aligned_len & MT_MCU_MSG_LEN_MASK)
        )
        packet = struct.pack("<I", info) + payload + b"\x00" * pad_bytes
        self.t.bulk_out(EP_OUT_INBAND_CMD, packet, timeout_ms=MCU_SEND_TIMEOUT_MS)
        return seq

    def _wait_resp(self, seq: int) -> bytes:
        """Read MCU responses until one matches `seq` with EVT_CMD_DONE.

        Retries on timeout up to MCU_RESP_MAX_RETRY (kernel value=5). Returns
        the response payload (everything after the 4-byte rxfce header,
        minus the 4-byte trailing pad).
        """
        for attempt in range(MCU_RESP_MAX_RETRY):
            try:
                data = self.t.bulk_in(EP_IN_CMD_RESP, MCU_RESP_URB_SIZE,
                                      timeout_ms=MCU_RESP_TIMEOUT_MS)
            except usb.core.USBError as e:
                # Kernel only retries on -ETIMEDOUT (`if (ret == -ETIMEDOUT)
                # continue;`). Other errors propagate.
                if "timed out" in str(e).lower() or "timeout" in str(e).lower():
                    logger.warning("MCU resp: timeout attempt %d/%d, retrying",
                                   attempt + 1, MCU_RESP_MAX_RETRY)
                    continue
                raise

            if len(data) < 8:
                raise MCUError(
                    f"MCU resp: short read {len(data)} bytes < 8 (header+tail)"
                )
            rxfce = struct.unpack("<I", data[:4])[0]
            rsp_seq = (rxfce & MT_RX_FCE_INFO_CMD_SEQ_MASK) >> MT_RX_FCE_INFO_CMD_SEQ_SHIFT
            evt = (rxfce & MT_RX_FCE_INFO_EVT_TYPE_MASK) >> MT_RX_FCE_INFO_EVT_TYPE_SHIFT
            if rsp_seq == seq and evt == EVT_CMD_DONE:
                # Trim leading rxfce header (4 bytes) + trailing pad (4 bytes).
                return bytes(data[4:-4])
            logger.error(
                "MCU resp: evt=%d seq=%d (expected seq=%d, EVT_CMD_DONE=%d)",
                evt, rsp_seq, seq, EVT_CMD_DONE,
            )

        raise MCUError(f"MCU resp: no match after {MCU_RESP_MAX_RETRY} attempts "
                       f"(seq={seq})")

    # ---- Public API -------------------------------------------------
    def send_msg(self, cmd: int, payload: bytes = b"",
                 wait_resp: bool = True) -> Optional[bytes]:
        """Send one MCU command. Returns the response payload (without
        rxfce header / trailing pad), or None if wait_resp is False.
        """
        with self._lock:
            seq = self._send(cmd, payload, wait_resp)
            if not wait_resp:
                return None
            return self._wait_resp(seq)

    # ---- CMD_CALIBRATION_OP convenience -----------------------------
    def calibrate(self, cal_type: int, param: int) -> None:
        """Port of `mt76x02_mcu_calibrate` ([SRC] mt76x02_mcu.c:117-143).

        Sends `CMD_CALIBRATION_OP` with payload `<le32 type, le32 param>`,
        wait=True. The is_mt76x2e branch is mmio-only — never taken on USB.
        """
        from .constants import CMD_CALIBRATION_OP
        payload = struct.pack("<II", cal_type & 0xFFFFFFFF, param & 0xFFFFFFFF)
        self.send_msg(CMD_CALIBRATION_OP, payload, wait_resp=True)

    # ---- CMD_FUN_SET_OP convenience ---------------------------------
    def function_select(self, func: int, value: int) -> None:
        """Port of `mt76x02_mcu_function_select` — [SRC] mt76x02_mcu.c:82-99.

        Sends `CMD_FUN_SET_OP` with payload `<le32 func, le32 value>`.
        wait=False for Q_SELECT, True otherwise (per kernel:94).

        This is the FIRST MCU command Kali sends after MAC reset (Q_SELECT,
        value=1) — without it, the chip's MCU doesn't respond to subsequent
        commands. [WIRE] capture-2.pcap:423 payload `01000000 01000000`.
        """
        payload = struct.pack("<II", func & 0xFFFFFFFF, value & 0xFFFFFFFF)
        wait = func != Q_SELECT
        self.send_msg(CMD_FUN_SET_OP, payload, wait_resp=wait)

    # ---- CMD_RANDOM_WRITE / CMD_RANDOM_READ convenience --------------
    # Payload format for both is N×(u32 addr, u32 value) pairs. The wire
    # `addr` is always `base + reg` — kernel mt76x02u_mcu_{wr,rd}_rp line
    # `skb_put_le32(skb, base + data[i].reg)`. Callers pass logical regs
    # (e.g. MT_MAC_CSR0 = 0x1000) plus `base = MT_MCU_MEMMAP_WLAN`. For
    # READ the value field is ignored on send; the response carries
    # [base+reg, value] back.
    def random_write(
        self, base: int, reg_pairs: list[tuple[int, int]],
    ) -> None:
        """Write N (reg, value) pairs via CMD_RANDOM_WRITE.

        Chunks at MT_MCU_REG_PAIRS_PER_CMD (= MT_INBAND_PACKET_MAX_LEN / 8 = 24)
        pairs per command, matching kernel `mt76x02u_mcu_wr_rp`
        ([SRC] mt76x02_usb_mcu.c:132-163). Only the LAST chunk uses `wait_resp=true`
        (kernel: `cnt == n`); intermediate chunks fire-and-forget.

        Wire address = `base + reg` for each pair.
        """
        if not reg_pairs:
            return
        n = len(reg_pairs)
        i = 0
        while i < n:
            chunk = reg_pairs[i: i + MT_MCU_REG_PAIRS_PER_CMD]
            i += len(chunk)
            is_last = i == n
            payload = b"".join(
                struct.pack("<II", (base + reg) & 0xFFFFFFFF, val & 0xFFFFFFFF)
                for reg, val in chunk
            )
            self.send_msg(CMD_RANDOM_WRITE, payload, wait_resp=is_last)

    def random_read(self, base: int, regs: list[int]) -> list[int]:
        """Read N 32-bit registers via CMD_RANDOM_READ. Wire addr = base + reg.

        Returns list of values in the same order as `regs`. The kernel
        verifies `(response_addr - base) == requested_reg` and warns on
        mismatch — we do the same defensively, raising on mismatch.
        """
        if not regs:
            return []
        payload = b"".join(struct.pack("<II", (base + r) & 0xFFFFFFFF, 0) for r in regs)
        resp = self.send_msg(CMD_RANDOM_READ, payload, wait_resp=True)
        assert resp is not None
        if len(resp) < 8 * len(regs):
            raise MCUError(
                f"random_read: resp len {len(resp)} < expected {8 * len(regs)}"
            )
        values: list[int] = []
        for i, requested_reg in enumerate(regs):
            addr, val = struct.unpack("<II", resp[8 * i: 8 * i + 8])
            decoded_reg = (addr - base) & 0xFFFFFFFF
            if decoded_reg != requested_reg:
                raise MCUError(
                    f"random_read: resp[{i}] reg mismatch "
                    f"(requested 0x{requested_reg:08x}, got 0x{decoded_reg:08x} "
                    f"-- raw addr=0x{addr:08x}, base=0x{base:08x})"
                )
            values.append(val)
        return values


def mcu_init_smoke_test(mcu: MCUChannel, transport: MT76x0UTransport) -> dict:
    """M2 demoable: send one CMD_RANDOM_READ for MT_MAC_CSR0 via MCU
    (with base=MT_MCU_MEMMAP_WLAN) and compare to a direct vendor read of
    the same register. If they match, the MCU channel is round-tripping
    cleanly. The base+reg encoding matches kernel mt76x0/init.c:84.
    """
    from .constants import MT_MAC_CSR0, MT_MCU_MEMMAP_WLAN
    direct = transport.read32(MT_MAC_CSR0)
    mcu_vals = mcu.random_read(base=MT_MCU_MEMMAP_WLAN, regs=[MT_MAC_CSR0])
    via_mcu = mcu_vals[0]
    return {
        "register": "MT_MAC_CSR0",
        "addr": MT_MAC_CSR0,
        "via_vendor_read": direct,
        "via_mcu_read": via_mcu,
        "match": (direct == via_mcu),
    }
