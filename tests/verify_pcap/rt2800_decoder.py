"""Family decoder for rt2800usb chips: dump text, channel-switch and TX trigger recognition."""
from __future__ import annotations

import struct
from typing import Sequence

from tests.verify_pcap.usb_capture import TransferType, UsbOp
from tests.verify_pcap.verify_capture import FamilyDecoder, InjectFrameTrigger, SetChannelTrigger, Trigger
from wifit3.chips.rt5370 import constants as C

CHANNEL_SWITCH_LOOKAHEAD = 64
RFCSR_CHANNEL_RF1 = 8
RFCSR_CHANNEL_RF3 = 9

REGISTER_NAMES = {
    C.MAC_CSR0: "MAC_CSR0", C.MAC_SYS_CTRL: "MAC_SYS_CTRL", C.PBF_SYS_CTRL: "PBF_SYS_CTRL",
    C.GPIO_CTRL: "GPIO_CTRL", C.WPDMA_GLO_CFG: "WPDMA_GLO_CFG", C.USB_DMA_CFG: "USB_DMA_CFG",
    C.EFUSE_CTRL: "EFUSE_CTRL", C.EFUSE_DATA0: "EFUSE_DATA0", C.EFUSE_DATA1: "EFUSE_DATA1",
    C.EFUSE_DATA2: "EFUSE_DATA2", C.EFUSE_DATA3: "EFUSE_DATA3", C.BBP_CSR_CFG: "BBP_CSR_CFG",
    C.RF_CSR_CFG: "RF_CSR_CFG", C.H2M_MAILBOX_CSR: "H2M_MAILBOX_CSR", C.HOST_CMD_CSR: "HOST_CMD_CSR",
    C.H2M_MAILBOX_CID: "H2M_MAILBOX_CID", C.H2M_MAILBOX_STATUS: "H2M_MAILBOX_STATUS",
    C.H2M_BBP_AGENT: "H2M_BBP_AGENT", C.H2M_INT_SRC: "H2M_INT_SRC",
    C.AUTOWAKEUP_CFG: "AUTOWAKEUP_CFG", C.RX_FILTER_CFG: "RX_FILTER_CFG", C.TX_STA_FIFO: "TX_STA_FIFO",
    C.CH_IDLE_STA: "CH_IDLE_STA", C.CH_BUSY_STA: "CH_BUSY_STA", C.CH_BUSY_STA_SEC: "CH_BUSY_STA_SEC",
    C.TX_PWR_CFG_0: "TX_PWR_CFG_0", C.TX_PWR_CFG_1: "TX_PWR_CFG_1", C.TX_PWR_CFG_2: "TX_PWR_CFG_2",
    C.TX_PWR_CFG_3: "TX_PWR_CFG_3", C.TX_PWR_CFG_4: "TX_PWR_CFG_4", C.TX_BAND_CFG: "TX_BAND_CFG",
    C.TX_PIN_CFG: "TX_PIN_CFG", C.TX_RTY_CFG: "TX_RTY_CFG", C.MAC_STATUS_CFG: "MAC_STATUS_CFG",
}


def is_register_access(op: UsbOp, request: int, address: int) -> bool:
    return (op.transfer_type == TransferType.CONTROL and op.setup is not None
            and op.request == request and op.index == address)


def register_value(op: UsbOp) -> int | None:
    if len(op.data) != 4:
        return None
    return struct.unpack("<I", op.data)[0]


class Rt2800Decoder(FamilyDecoder):
    def describe(self, op: UsbOp) -> str:
        status = f"  status {op.status}" if op.status else ""
        if op.transfer_type != TransferType.CONTROL:
            direction = "RX" if op.is_in else "TX"
            text = f"{direction} ep 0x{op.endpoint:02x} {len(op.data)}B"
            frame_offset = C.TXINFO_DESC_SIZE + C.TXWI_DESC_SIZE_4WORDS
            if not op.is_in and len(op.data) >= frame_offset + 2:
                frame_control = struct.unpack_from("<H", op.data, frame_offset)[0]
                text += f" descriptor {op.data[:frame_offset].hex()} FC 0x{frame_control:04x}"
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
            return f"R  0x{address:04x} {name} = 0x{value:08x}{status}"
        if op.request == C.USB_MULTI_WRITE:
            if value is None:
                return f"W  0x{address:04x} {name} <{len(op.data)}B>{status}"
            text = f"W  0x{address:04x} {name} = 0x{value:08x}"
            if address == C.BBP_CSR_CFG:
                register = (value & C.BBP_CSR_CFG_REGNUM) >> 8
                if value & C.BBP_CSR_CFG_READ_CONTROL:
                    text += f"  (BBP{register} read)"
                else:
                    text += f"  (BBP{register} <- 0x{value & C.BBP_CSR_CFG_VALUE:02x})"
            elif address == C.RF_CSR_CFG:
                register = (value & C.RF_CSR_CFG_REGNUM) >> 8
                if value & C.RF_CSR_CFG_WRITE:
                    text += f"  (RFCSR{register} <- 0x{value & C.RF_CSR_CFG_DATA:02x})"
                else:
                    text += f"  (RFCSR{register} read)"
            return text + status
        if op.request == C.USB_DEVICE_MODE:
            return f"DEVICE_MODE wValue=0x{op.value:04x} wIndex=0x{address:04x}{status}"
        return f"CTRL setup {op.setup.hex()} data {op.data.hex()}{status}"

    def find_trigger(self, ops: Sequence[UsbOp], position: int) -> Trigger | None:
        op = ops[position]
        if op.transfer_type == TransferType.BULK and not op.is_in:
            txwi_word1 = struct.unpack_from("<I", op.data, C.TXINFO_DESC_SIZE + 4)[0]
            frame_length = (txwi_word1 & C.TXWI_W1_MPDU_TOTAL_BYTE_COUNT) >> 16
            frame_offset = C.TXINFO_DESC_SIZE + C.TXWI_DESC_SIZE_4WORDS
            return InjectFrameTrigger(op.data[frame_offset:frame_offset + frame_length])
        if position + 2 >= len(ops):
            return None
        rx_stop = ops[position + 1]
        rx_stop_value = register_value(rx_stop)
        # rt2x00mac_config(CHANGE_CHANNEL): stop RX, then the survey reads, then the RF tune.
        if not (is_register_access(op, C.USB_MULTI_READ, C.MAC_SYS_CTRL)
                and is_register_access(rx_stop, C.USB_MULTI_WRITE, C.MAC_SYS_CTRL)
                and rx_stop_value is not None and not rx_stop_value & C.MAC_SYS_CTRL_ENABLE_RX
                and is_register_access(ops[position + 2], C.USB_MULTI_READ, C.CH_IDLE_STA)):
            return None
        rf1: int | None = None
        rf3: int | None = None
        for lookahead in ops[position:position + CHANNEL_SWITCH_LOOKAHEAD]:
            value = register_value(lookahead)
            if (value is None or not is_register_access(lookahead, C.USB_MULTI_WRITE, C.RF_CSR_CFG)
                    or not value & C.RF_CSR_CFG_WRITE):
                continue
            register = (value & C.RF_CSR_CFG_REGNUM) >> 8
            if register == RFCSR_CHANNEL_RF1 and rf1 is None:
                rf1 = value & C.RF_CSR_CFG_DATA
            elif register == RFCSR_CHANNEL_RF3 and rf3 is None:
                rf3 = value & C.RF_CSR_CFG_DATA
        for channel, (channel_rf1, _channel_rf2, channel_rf3) in C.RF_VALS_3X_2G.items():
            if (channel_rf1, channel_rf3) == (rf1, rf3):
                return SetChannelTrigger(channel)
        return None

    def carries_only_80211_frames(self, op: UsbOp) -> bool:
        """rt2800usb bulk IN is RX only; TX status is polled through TX_STA_FIFO."""
        return op.transfer_type == TransferType.BULK and op.is_in
