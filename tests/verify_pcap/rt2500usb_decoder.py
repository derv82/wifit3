"""Family decoder for rt2500usb (RT2570): 16-bit CSRs, BBP via PHY_CSR7, RF via PHY_CSR9/10."""
from __future__ import annotations

import struct
from typing import Sequence

from tests.verify_pcap.usb_capture import TransferType, UsbOp
from tests.verify_pcap.verify_capture import FamilyDecoder
from wifit3.chips.rt2500usb import constants as C
from wifit3.chips.rt2500usb.chan import RF_VALS_2525E

CHANNEL_SWITCH_LOOKAHEAD = 32

REGISTER_NAMES = {
    C.MAC_CSR0: "MAC_CSR0", C.TXRX_CSR2: "TXRX_CSR2", C.PHY_CSR7: "PHY_CSR7", C.PHY_CSR8: "PHY_CSR8",
    C.PHY_CSR9: "PHY_CSR9", C.PHY_CSR10: "PHY_CSR10",
}


def is_register_access(op: UsbOp, request: int, address: int) -> bool:
    return (op.transfer_type == TransferType.CONTROL and op.setup is not None
            and op.request == request and op.index == address)


def register_value(op: UsbOp) -> int | None:
    if len(op.data) != 2:
        return None
    return struct.unpack("<H", op.data)[0]


class Rt2500usbDecoder(FamilyDecoder):
    """rf_vals: channel -> (rf1, rf2, rf3, rf4), the rt2500usb.c rf_vals table of the captured card's RF chip."""

    def __init__(self, rf_vals: dict[int, tuple[int, int, int, int]] = RF_VALS_2525E) -> None:
        self.rf_vals = rf_vals

    def describe(self, op: UsbOp) -> str:
        status = f"  status {op.status}" if op.status else ""
        if op.transfer_type != TransferType.CONTROL:
            direction = "RX" if op.is_in else "TX"
            text = f"{direction} ep 0x{op.endpoint:02x} {len(op.data)}B"
            if not op.is_in and len(op.data) >= C.TXD_DESC_SIZE + 2:
                frame_control = struct.unpack_from("<H", op.data, C.TXD_DESC_SIZE)[0]
                text += f" descriptor {op.data[:C.TXD_DESC_SIZE].hex()} FC 0x{frame_control:04x}"
            return text + status
        assert op.setup is not None
        address = op.index
        name = REGISTER_NAMES.get(address, "")
        value = register_value(op)
        if op.request == C.USB_MULTI_READ:
            if op.status == 0 and not op.data:
                return f"R  0x{address:04x} {name}"
            if value is None:
                return f"R  0x{address:04x} {name} <{op.length}B> {op.data.hex()}{status}"
            return f"R  0x{address:04x} {name} = 0x{value:04x}{status}"
        if op.request == C.USB_MULTI_WRITE:
            if value is None:
                return f"W  0x{address:04x} {name} <{len(op.data)}B> {op.data.hex()}{status}"
            text = f"W  0x{address:04x} {name} = 0x{value:04x}"
            if address == C.PHY_CSR7:
                register = (value & C.PHY_CSR7_REG_ID) >> 8
                if value & C.PHY_CSR7_READ_CONTROL:
                    text += f"  (BBP{register} read)"
                else:
                    text += f"  (BBP{register} <- 0x{value & C.PHY_CSR7_DATA:02x})"
            return text + status
        if op.request == C.USB_SINGLE_WRITE:
            return f"SW 0x{address:04x} {name} = 0x{op.value:04x}{status}"
        if op.request == C.USB_DEVICE_MODE:
            return f"DEVICE_MODE wValue=0x{op.value:04x} wIndex=0x{address:04x}{status}"
        if op.request == C.USB_EEPROM_READ:
            return f"EEPROM_READ <{op.length}B> {op.data.hex()}{status}"
        return f"CTRL setup {op.setup.hex()} data {op.data.hex()}{status}"

    def injected_frame_at(self, ops: Sequence[UsbOp], position: int) -> bytes | None:
        op = ops[position]
        if op.transfer_type == TransferType.BULK and not op.is_in and len(op.data) >= C.TXD_DESC_SIZE:
            txd_word0 = struct.unpack_from("<I", op.data, 0)[0]
            frame_length = (txd_word0 & C.TXD_W0_DATABYTE_COUNT) >> 16
            return op.data[C.TXD_DESC_SIZE:C.TXD_DESC_SIZE + frame_length]
        return None

    def channel_change_at(self, ops: Sequence[UsbOp], position: int) -> int | None:
        """rt2x00mac_config(CHANGE_CHANNEL): TXRX_CSR2 read, write with DISABLE_RX, then the RF writes; a channel is
        its rf1, rf2 and rf4 in consecutive PHY_CSR9 writes (rt2500usb.c:582-611 rt2500usb_config_channel)."""
        if position + 1 >= len(ops):
            return None
        rx_stop = ops[position + 1]
        rx_stop_value = register_value(rx_stop)
        if not (is_register_access(ops[position], C.USB_MULTI_READ, C.TXRX_CSR2)
                and is_register_access(rx_stop, C.USB_MULTI_WRITE, C.TXRX_CSR2)
                and rx_stop_value is not None and rx_stop_value & C.TXRX_CSR2_DISABLE_RX):
            return None
        rf_values: list[int] = []
        for lookahead in ops[position + 2:position + CHANNEL_SWITCH_LOOKAHEAD]:
            value = register_value(lookahead)
            if value is not None and is_register_access(lookahead, C.USB_MULTI_WRITE, C.PHY_CSR9):
                rf_values.append(value)
        for start in range(len(rf_values) - 3):
            rf1, rf2, _rf3, rf4 = rf_values[start:start + 4]
            for channel, (channel_rf1, channel_rf2, _channel_rf3, channel_rf4) in self.rf_vals.items():
                if (rf1, rf2, rf4) == (channel_rf1 & 0xFFFF, channel_rf2 & 0xFFFF, channel_rf4 & 0xFFFF):
                    return channel
        return None

    def carries_only_80211_frames(self, op: UsbOp) -> bool:
        """rt2500usb bulk IN is RX only; the TX status is the bulk-OUT URB's completion."""
        return op.transfer_type == TransferType.BULK and op.is_in
