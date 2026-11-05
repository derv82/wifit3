"""Family decoder for rtl8187 (RTL8187L): vendor request 0x05, wValue = address, wIndex = page or 0x8225 (RF)."""
from __future__ import annotations

import struct
from typing import Sequence

from tests.verify_pcap.usb_capture import TransferType, UsbOp
from tests.verify_pcap.verify_capture import FamilyDecoder
from wifit3.chips.rtl8187 import constants as C
from wifit3.chips.rtl8187.rtl8225 import rtl8225_chan
from wifit3.chips.rtl8187.tx import TX_HDR_SIZE

RF_WRITE_INDEX = 0x8225
RF_CHANNEL_REGISTER = 0x07
TX_HDR_FLAGS_LENGTH = 0xFFF
CHANNEL_SWITCH_LOOKAHEAD = 64
CHANNEL_BY_RF7_VALUE = {value: channel for channel, value in enumerate(rtl8225_chan, start=1)}

REGISTER_NAMES = {
    C.REG_TX_CONF: "TX_CONF", C.REG_RX_CONF: "RX_CONF", C.REG_GPIO0: "GPIO0",
    C.REG_ATIM_WND: "ATIM_WND", C.REG_BEACON_INTERVAL: "BEACON_INTERVAL",
}


def is_register_access(op: UsbOp) -> bool:
    return (op.transfer_type == TransferType.CONTROL and op.setup is not None
            and op.request == C.RTL8187_REQ_GET_REG)


def is_tx_conf_access(op: UsbOp, is_in: bool) -> bool:
    return is_register_access(op) and op.is_in == is_in and op.value == C.REG_TX_CONF and op.index == 0


def register_value(op: UsbOp) -> int | None:
    if not op.data or len(op.data) > 4:
        return None
    return int.from_bytes(op.data, "little")


class Rtl8187Decoder(FamilyDecoder):
    def describe(self, op: UsbOp) -> str:
        status = f"  status {op.status}" if op.status else ""
        if op.transfer_type != TransferType.CONTROL:
            direction = "RX" if op.is_in else "TX"
            text = f"{direction} ep 0x{op.endpoint:02x} {len(op.data)}B"
            if not op.is_in and len(op.data) >= TX_HDR_SIZE + 2:
                frame_control = struct.unpack_from("<H", op.data, TX_HDR_SIZE)[0]
                text += f" tx_hdr {op.data[:TX_HDR_SIZE].hex()} FC 0x{frame_control:04x}"
            return text + status
        assert op.setup is not None
        if not is_register_access(op):
            return f"CTRL setup {op.setup.hex()} data {op.data.hex()}{status}"
        value = register_value(op)
        if op.index == RF_WRITE_INDEX and not op.is_in and value is not None:
            return f"RF 0x{op.value:02x} <- 0x{value:04x}{status}"
        location = f"0x{op.value:04x}" if op.index == 0 else f"0x{op.value:04x}@{op.index}"
        name = REGISTER_NAMES.get(op.value, "") if op.index == 0 else ""
        width = op.length
        if op.is_in:
            if value is None:
                return f"R  {location}/{width} {name}{status}"
            return f"R  {location}/{width} {name} = 0x{value:0{width * 2}x}{status}"
        if value is None:
            return f"W  {location} {name} <{len(op.data)}B> {op.data[:8].hex()}{status}"
        return f"W  {location}/{width} {name} = 0x{value:0{width * 2}x}{status}"

    def injected_frame_at(self, ops: Sequence[UsbOp], position: int) -> bytes | None:
        op = ops[position]
        if op.transfer_type != TransferType.BULK or op.is_in or len(op.data) < TX_HDR_SIZE:
            return None
        frame_length = struct.unpack_from("<I", op.data, 0)[0] & TX_HDR_FLAGS_LENGTH
        return op.data[TX_HDR_SIZE:TX_HDR_SIZE + frame_length]

    def channel_change_at(self, ops: Sequence[UsbOp], position: int) -> int | None:
        """rtl8187_config (dev.c:1155-1179): TX_CONF read, TX_CONF write with LOOPBACK_MAC, then set_chan's
        RF 0x07 write of rtl8225_chan[channel - 1]."""
        if position + 1 >= len(ops):
            return None
        loopback_write = ops[position + 1]
        loopback_value = register_value(loopback_write)
        if not (is_tx_conf_access(ops[position], is_in=True) and is_tx_conf_access(loopback_write, is_in=False)
                and loopback_value is not None and loopback_value & C.TX_CONF_LOOPBACK_MAC):
            return None
        for lookahead in ops[position + 2:position + CHANNEL_SWITCH_LOOKAHEAD]:
            if is_tx_conf_access(lookahead, is_in=False):
                return None
            value = register_value(lookahead)
            if (is_register_access(lookahead) and not lookahead.is_in and lookahead.index == RF_WRITE_INDEX
                    and lookahead.value == RF_CHANNEL_REGISTER and value is not None):
                return CHANNEL_BY_RF7_VALUE.get(value)
        return None

    def carries_only_80211_frames(self, op: UsbOp) -> bool:
        """rtl8187 (8187L) bulk IN is RX only; it has no TX status endpoint."""
        return op.transfer_type == TransferType.BULK and op.is_in
