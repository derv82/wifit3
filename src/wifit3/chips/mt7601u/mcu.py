"""MCU inband-command transport and firmware download for MT7601U.

Ported from driver_sources/mt7601u-source-v7.2/mt7601u/mcu.c (tag v7.2).

An MCU message is a 4-byte TX descriptor followed by the payload, padded to a
4-byte boundary plus 4 trailing bytes -- the layout mt7601u_dma_skb_wrap builds
(dma.h:64). Everything above the MAC goes through here: the register-pair and
burst writers, the calibration commands, and the firmware image itself.

The firmware download (mt7601u_upload_firmware) only ever writes the chip's
volatile ILM/DLM RAM through the vendor firmware-download path. No fuse is
programmed.
"""
from __future__ import annotations

import logging
import time

from .constants import (
    ATOMIC_TSSI_SETTING,
    CMD_BURST_WRITE,
    CMD_CALIBRATION_OP,
    CMD_DONE,
    CMD_FUN_SET_OP,
    CMD_RANDOM_WRITE,
    CPU_TX_PORT,
    DMA_COMMAND,
    DMA_PACKET,
    INBAND_PACKET_MAX_LEN,
    MT_FCE_DMA_ADDR,
    MT_FCE_DMA_LEN,
    MT_MCU_COM_REG0,
    MT_MCU_COM_REG1,
    MT_MCU_DLM_OFFSET,
    MT_MCU_IVB_SIZE,
    MT_MCU_MEMMAP_WLAN,
    MT_RXD_CMD_INFO_CMD_SEQ,
    MT_RXD_CMD_INFO_EVT_TYPE,
    MT_TX_CPU_FROM_FCE_CPU_DESC_IDX,
    MT_TXD_CMD_INFO_SEQ,
    MT_TXD_CMD_INFO_TYPE,
    MT_TXD_INFO_D_PORT,
    MT_TXD_INFO_LEN,
    MT_TXD_INFO_TYPE,
    _field_get,
    _field_prep,
)
from .transport import DeviceGone, MT7601UTransport

logger = logging.getLogger(__name__)

MCU_FW_URB_MAX_PAYLOAD = 0x3800
"""mcu.c:20 -- the firmware URB's usable payload."""
MCU_RESP_URB_SIZE = 1024
"""mcu.c:22 -- the MCU response URB."""
MCU_MSG_TIMEOUT_MS = 500
"""mcu.c:137 usb_bulk_msg timeout."""
MCU_RESP_TIMEOUT_MS = 300
"""mcu.c:76 wait_for_completion_timeout for the response URB."""
MCU_RESP_RETRIES = 5
"""mcu.c:72 -- `int i = 5` in mt7601u_mcu_wait_resp."""
FW_DOWNLOAD_TIMEOUT_MS = 1000
"""mcu.c:317 -- firmware-upload completion timeout."""
FW_POLL_TIMEOUT_MS = 500
"""mcu.c:349 -- the poll between firmware chunks."""
FW_CHUNK_POLL_BIT = 1 << 31
"""mcu.c:349 -- MT_MCU_COM_REG1 BIT(31)."""
FW_RUNNING_POLL_TRIES = 100
"""mcu.c:391 -- `for (i = 100; i && !firmware_running(dev); i--)`, 10 ms apart."""

_POLL_SLEEP_S = 0.001
"""core.c:44 -- mt76_poll's udelay(10)."""


class McuTimeout(Exception):
    """An MCU command did not get its CMD_DONE response, or a firmware poll expired."""


def wrap_mcu_msg(payload: bytes, seq: int, cmd: int) -> bytes:
    """Prefix an MCU command with its TX descriptor (dma.h:58 mt7601u_dma_skb_wrap).

    Layout: | 4B TXINFO | payload | zero pad to 4B | 4B zero |, with the TXINFO
    length field carrying the padded payload length.
    """
    padded = (len(payload) + 3) & ~3
    info = (
        _field_prep(MT_TXD_CMD_INFO_SEQ, seq)
        | _field_prep(MT_TXD_CMD_INFO_TYPE, cmd)
        | _field_prep(MT_TXD_INFO_LEN, padded)
        | _field_prep(MT_TXD_INFO_D_PORT, CPU_TX_PORT)
        | _field_prep(MT_TXD_INFO_TYPE, DMA_COMMAND)
    )
    return info.to_bytes(4, "little") + payload.ljust(padded, b"\x00") + b"\x00" * 4


class MT7601UMcu:
    """Sends MCU inband commands and downloads the firmware image."""

    def __init__(self, tp: MT7601UTransport):
        self.tp = tp
        self.msg_seq = 0
        self.mcu_running = False

    # ------------------------------------------------------------------
    # Message plumbing (mcu.c:110 mt7601u_mcu_msg_send)
    # ------------------------------------------------------------------

    def msg_send(self, payload: bytes, cmd: int, wait_resp: bool) -> None:
        """Send one MCU command, optionally awaiting its CMD_DONE.

        The kernel takes dev->mcu.mutex around the whole exchange; our port is
        single-threaded on the control path, so the lock is implicit.
        """
        if not self.mcu_running and cmd not in ():
            raise McuTimeout("MCU not running")

        seq = 0
        if wait_resp:
            while not seq:                       # mcu.c:127-128
                self.msg_seq = (self.msg_seq + 1) & 0xF
                seq = self.msg_seq

        msg = wrap_mcu_msg(payload, seq, cmd)
        try:
            self.tp.bulk_out(msg, MCU_MSG_TIMEOUT_MS)
        except DeviceGone as e:
            raise DeviceGone(str(e)) from e

        if wait_resp:
            self.wait_resp(seq)

    def wait_resp(self, seq: int) -> None:
        """mt7601u_mcu_wait_resp: read until CMD_DONE with our seq comes back."""
        for _attempt in range(MCU_RESP_RETRIES):
            data = self.tp.bulk_in_resp(MCU_RESP_URB_SIZE, MCU_RESP_TIMEOUT_MS)
            if len(data) < 4:
                logger.warning("Warning: MCU response short (%d bytes)", len(data))
                continue
            rxfce = int.from_bytes(data[:4], "little")
            if (_field_get(MT_RXD_CMD_INFO_CMD_SEQ, rxfce) == seq
                    and _field_get(MT_RXD_CMD_INFO_EVT_TYPE, rxfce) == CMD_DONE):
                return
            logger.error("Error: MCU resp evt:%x seq:%x-%x!",
                         _field_get(MT_RXD_CMD_INFO_EVT_TYPE, rxfce),
                         seq, _field_get(MT_RXD_CMD_INFO_CMD_SEQ, rxfce))
        raise McuTimeout(f"MCU response for seq {seq} timed out")

    def function_select(self, func: int, val: int) -> None:
        """mcu.c:155 -- {id, value} as CMD_FUN_SET_OP; func 5 waits for a response."""
        payload = func.to_bytes(4, "little") + (val & 0xFFFFFFFF).to_bytes(4, "little")
        self.msg_send(payload, CMD_FUN_SET_OP, wait_resp=(func == 5))

    def calibrate(self, cal: int, val: int) -> None:
        """mcu.c:193 -- {id, value} as CMD_CALIBRATION_OP, always response-awaiting."""
        payload = (cal & 0xFFFFFFFF).to_bytes(4, "little") + \
            (val & 0xFFFFFFFF).to_bytes(4, "little")
        self.msg_send(payload, CMD_CALIBRATION_OP, wait_resp=True)

    def tssi_read_kick(self, use_hvga: int) -> None:
        """mcu.c:173 -- kick a TSSI read; a no-op unless the MCU is running."""
        if not self.mcu_running:
            return
        self.function_select(ATOMIC_TSSI_SETTING, use_hvga)

    # ------------------------------------------------------------------
    # Register writers (mcu.c:210 / mcu.c:239)
    # ------------------------------------------------------------------

    def write_reg_pairs(self, base: int, pairs: list[tuple[int, int]]) -> None:
        """CMD_RANDOM_WRITE the (offset, value) table at ``base``.

        The kernel chunks at INBAND_PACKET_MAX_LEN / 8 pairs per command and
        only response-awaits the final chunk.
        """
        max_vals = INBAND_PACKET_MAX_LEN // 8
        for start in range(0, len(pairs), max_vals):
            chunk = pairs[start:start + max_vals]
            # Interleaved (address, value) pairs -- mcu.c:227-230 writes both
            # into the same skb in one loop, not all addresses then all values.
            payload = b"".join(
                (base + reg).to_bytes(4, "little") + (val & 0xFFFFFFFF).to_bytes(4, "little")
                for reg, val in chunk)
            self.msg_send(payload, CMD_RANDOM_WRITE, wait_resp=(start + len(chunk) == len(pairs)))

    def burst_write_regs(self, offset: int, values: list[int]) -> None:
        """CMD_BURST_WRITE ``values`` sequentially from ``offset``.

        Each command carries a destination address then the words; the kernel
        chunks at INBAND_PACKET_MAX_LEN / 4 - 1 because one slot holds the address.
        """
        max_regs = INBAND_PACKET_MAX_LEN // 4 - 1
        done = 0
        while done < len(values):
            chunk = values[done:done + max_regs]
            payload = (MT_MCU_MEMMAP_WLAN + offset + done * 4).to_bytes(4, "little")
            payload += b"".join((v & 0xFFFFFFFF).to_bytes(4, "little") for v in chunk)
            done += len(chunk)
            self.msg_send(payload, CMD_BURST_WRITE, wait_resp=(done == len(values)))

    # ------------------------------------------------------------------
    # Firmware download (mcu.c:283 __mt7601u_dma_fw)
    # ------------------------------------------------------------------

    def firmware_running(self) -> bool:
        """mcu.c:24 -- the MCU reports 1 in MT_MCU_COM_REG0 once firmware is up."""
        return self.tp.rr(MT_MCU_COM_REG0) == 1

    def _dma_fw_chunk(self, data: bytes, dst_addr: int) -> None:
        """One firmware chunk (mcu.c:283), sized to the padded transfer length."""
        length = len(data)
        padded = (length + 3) & ~3

        # The kernel writes the DMA descriptor inline ahead of the data, then
        # points the FCE at the destination and the length.
        reg = (_field_prep(MT_TXD_INFO_TYPE, DMA_PACKET)
               | _field_prep(MT_TXD_INFO_D_PORT, CPU_TX_PORT)
               | _field_prep(MT_TXD_INFO_LEN, length))
        # mcu.c:298 zeroes 8 bytes past the data, but the transfer length is only
        # MT_DMA_HDR_LEN + len + 4 (mcu.c:310): the final 4 stay behind as the next
        # chunk's staging.
        buf = (reg.to_bytes(4, "little") + data + b"\x00" * 8)[:4 + padded + 4]

        self.tp.fce_wr(MT_FCE_DMA_ADDR, dst_addr)
        self.tp.fce_wr(MT_FCE_DMA_LEN, (padded << 16) & 0xFFFFFFFF)
        self.tp.bulk_out_inband_fw(buf)

        # Bump the descriptor index so the chip consumes the chunk.
        val = (self.tp.rr(MT_TX_CPU_FROM_FCE_CPU_DESC_IDX) + 1) & 0xFFFFFFFF
        self.tp.wr(MT_TX_CPU_FROM_FCE_CPU_DESC_IDX, val)

    def dma_fw(self, data: bytes, dst_addr: int) -> None:
        """Upload ``data`` in MCU_FW_URB_MAX_PAYLOAD chunks (mcu.c:336)."""
        if not data:
            return
        n = min(MCU_FW_URB_MAX_PAYLOAD, len(data))
        self._dma_fw_chunk(data[:n], dst_addr)

        if not self._poll_mcu_com_reg1():
            raise McuTimeout("firmware chunk not consumed")

        self.dma_fw(data[n:], dst_addr + n)

    def _poll_mcu_com_reg1(self) -> bool:
        """mt76_poll_msec(dev, MT_MCU_COM_REG1, BIT(31), BIT(31), 500) (mcu.c:349)."""
        timeout = FW_POLL_TIMEOUT_MS // 10
        while True:
            if self.tp.rr(MT_MCU_COM_REG1) & FW_CHUNK_POLL_BIT:
                return True
            if timeout <= 0:
                return False
            timeout -= 1
            time.sleep(0.010)

    def upload_firmware(self, fw: bytes) -> None:
        """Push ILM then DLM, then boot the MCU (mcu.c:356).

        ILM starts after the 64-byte IVB that the header already carries; DLM goes
        to MT_MCU_DLM_OFFSET.
        """
        ilm_len = int.from_bytes(fw[0:4], "little") - MT_MCU_IVB_SIZE
        ivb = fw[32:32 + MT_MCU_IVB_SIZE]
        dlm_len = int.from_bytes(fw[4:8], "little")

        logger.info("loading FW - ILM %u + IVB %u", ilm_len, MT_MCU_IVB_SIZE)
        self.dma_fw(fw[32 + MT_MCU_IVB_SIZE:32 + MT_MCU_IVB_SIZE + ilm_len], MT_MCU_IVB_SIZE)

        logger.info("loading FW - DLM %u", dlm_len)
        ilm_end = 32 + MT_MCU_IVB_SIZE + ilm_len
        self.dma_fw(fw[ilm_end:ilm_end + dlm_len], MT_MCU_DLM_OFFSET)

        # The IVB is handed over in one OUT transfer with the device-mode request.
        self.tp.vendor_request(0x01, 0x00, 0x12, 0, ivb, len(ivb))

        for _ in range(FW_RUNNING_POLL_TRIES):
            if self.firmware_running():
                logger.info("Firmware running!")
                return
            time.sleep(0.010)
        raise McuTimeout("firmware did not start")