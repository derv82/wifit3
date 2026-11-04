"""Family decoder for Realtek vendor-request chips (bRequest 0x05, wValue = address)."""
from __future__ import annotations

import struct
from dataclasses import replace
from typing import Sequence

from tests.verify_pcap.usb_capture import TransferType, UsbOp
from tests.verify_pcap.verify_capture import FamilyDecoder, RegisterAccess, RegisterOp, TxOpField
from wifit3.chips.rtl8192fu import constants as C

REALTEK_USB_CMD_REQ = 0x05
RXDESC24_SIZE = 24
RXDESC24_RPT_SEL = 1 << 28
LSSI_DATA_MASK = 0xFFFFF
TX_DESCRIPTOR_CHECKSUM_OFFSET = 7 * 4
# halmac 48-byte TX descriptor (rtl8822c)
HALMAC_TX_DESC_RTS_DATA_RTY_LMT = TxOpField(dword=4, shift=18, width=6)
HALMAC_TX_DESC_SW_SEQ = TxOpField(dword=9, shift=12, width=12)

REGISTER_NAMES = {
    C.REG_RF_CTRL: "RF_CTRL", C.REG_RCR: "RCR", C.REG_BEACON_CTRL: "BEACON_CTRL", C.REG_TXPAUSE: "TXPAUSE",
    C.REG_TX_AGC_A_CCK1_MCS32: "TX_AGC_A_CCK1_MCS32", C.REG_FPGA0_XA_LSSI_PARM: "FPGA0_XA_LSSI_PARM",
    C.REG_FPGA0_XB_LSSI_PARM: "FPGA0_XB_LSSI_PARM", C.REG_RXFLTMAP0: "RXFLTMAP0", C.REG_RXFLTMAP2: "RXFLTMAP2",
    C.REG_HMTFR: "HMTFR", C.REG_HMBOX_0: "HMBOX_0",
    C.REG_MCU_FW_DL: "MCU_FW_DL", C.REG_SYS_FUNC: "SYS_FUNC", C.REG_APS_FSMCO: "APS_FSMCO",
    C.REG_EFUSE_CTRL: "EFUSE_CTRL", C.REG_SYS_CFG: "SYS_CFG", C.REG_CR: "CR",
}


def is_register_access(op: UsbOp) -> bool:
    return (op.transfer_type == TransferType.CONTROL and op.setup is not None
            and op.request == REALTEK_USB_CMD_REQ)


def register_value(op: UsbOp) -> int | None:
    if not op.data or len(op.data) > 4:
        return None
    return int.from_bytes(op.data, "little")


class RealtekDecoder(FamilyDecoder):
    """Defaults are the Jaguar (rtl88xxau) values; rf_sipi_address or rf_base_address locates path A RF writes.
    tx_descriptor_checksum_size: the descriptor bytes the dword 7 checksum covers (rtl8812a_cal_txdesc_chksum: 32)."""

    def __init__(self, hop_start_register: int = 0x0454, rf_sipi_address: int | None = 0xC90,
                 rf_base_address: int | None = None, tx_descriptor_size: int = 40,
                 tx_descriptor_checksum_size: int = 32,
                 report_type_dword_and_mask: tuple[int, int] = (2, RXDESC24_RPT_SEL),
                 rx_packet_alignment: int = 8) -> None:
        self.hop_start_register = hop_start_register
        self.rf_sipi_address = rf_sipi_address
        self.rf_base_address = rf_base_address
        self.tx_descriptor_size = tx_descriptor_size
        self.tx_descriptor_checksum_size = tx_descriptor_checksum_size
        self.report_type_dword_and_mask = report_type_dword_and_mask
        self.rx_packet_alignment = rx_packet_alignment

    def describe(self, op: UsbOp) -> str:
        status = f"  status {op.status}" if op.status else ""
        if op.transfer_type != TransferType.CONTROL:
            direction = "RX" if op.is_in else "TX"
            text = f"{direction} ep 0x{op.endpoint:02x} {len(op.data)}B"
            if op.is_in:
                text += f" [{' '.join(self.rx_packet_kinds(op))}]"
            elif len(op.data) >= self.tx_descriptor_size + 2:
                frame_control = struct.unpack_from("<H", op.data, self.tx_descriptor_size)[0]
                text += f" descriptor {op.data[:self.tx_descriptor_size].hex()} FC 0x{frame_control:04x}"
            return text + status
        assert op.setup is not None
        if not is_register_access(op):
            return f"CTRL setup {op.setup.hex()} data {op.data.hex()}{status}"
        address = op.value
        name = REGISTER_NAMES.get(address, "")
        width = op.length
        value = register_value(op)
        if op.is_in:
            if value is None:
                return f"R  0x{address:04x}/{width} {name}{status}"
            return f"R  0x{address:04x}/{width} {name} = 0x{value:0{width * 2}x}{status}"
        if value is None:
            return f"W  0x{address:04x} {name} <{len(op.data)}B> {op.data[:8].hex()}{status}"
        text = f"W  0x{address:04x}/{width} {name} = 0x{value:0{width * 2}x}"
        rf_write = self.rf_write(op)
        if rf_write is not None:
            text += f"  (RF 0x{rf_write[0]:02x} <- 0x{rf_write[1]:05x})"
        return text + status

    def injected_frame_at(self, ops: Sequence[UsbOp], position: int) -> bytes | None:
        op = ops[position]
        if op.transfer_type == TransferType.BULK and not op.is_in:
            return op.data[self.tx_descriptor_size:]
        return None

    def channel_change_at(self, ops: Sequence[UsbOp], position: int) -> int | None:
        """A hop opens on a read of hop_start_register; its channel is the last path A RF 0x18 write before
        the next hop's read (a band switch reads hop_start_register again before any RF 0x18 write)."""
        op = ops[position]
        if not (is_register_access(op) and op.is_in and op.value == self.hop_start_register):
            return None
        channel = None
        for later_op in ops[position + 1:]:
            if (channel is not None and is_register_access(later_op) and later_op.is_in
                    and later_op.value == self.hop_start_register):
                break
            rf_write = self.rf_write(later_op)
            if rf_write is not None and rf_write[0] == C.RF6052_REG_MODE_AG:
                channel = rf_write[1] & 0xFF
        return channel

    def rf_write(self, op: UsbOp) -> tuple[int, int] | None:
        """(RF register, value) of a path A RF write, through the SIPI register or the direct window, else None."""
        if not is_register_access(op) or op.is_in:
            return None
        value = register_value(op)
        if value is None:
            return None
        if op.value == self.rf_sipi_address:
            return value >> C.FPGA0_LSSI_PARM_ADDR_SHIFT, value & LSSI_DATA_MASK
        if self.rf_base_address is not None and 0 <= op.value - self.rf_base_address < 0x400:
            return (op.value - self.rf_base_address) >> 2, value & LSSI_DATA_MASK
        return None

    def register_ops(self, op: UsbOp) -> list[RegisterOp]:
        """One read or write of wValue per vendor request; its value is its data, little-endian (None for a read the
        driver requests: no reply yet)."""
        if not is_register_access(op):
            return []
        if op.is_in:
            return [RegisterOp(RegisterAccess.READ, op.value, register_value(op))]
        return [RegisterOp(RegisterAccess.WRITE, op.value, int.from_bytes(op.data, "little"))]

    def with_tx_op_fields(self, op: UsbOp, fields: list[TxOpField], source: UsbOp) -> UsbOp:
        """The dword 7 [15:0] XOR checksum changes by a descriptor field's changed halfwords (fields in its coverage)."""
        if (op.transfer_type != TransferType.BULK or op.is_in or len(op.data) < self.tx_descriptor_size
                or len(source.data) != len(op.data)):
            return op
        data = bytearray(op.data)
        checksum_change = 0
        for field in fields:
            if 4 * field.dword + 4 > len(data):
                continue
            mask = ((1 << field.width) - 1) << field.shift
            dword = struct.unpack_from("<I", data, 4 * field.dword)[0]
            source_dword = struct.unpack_from("<I", source.data, 4 * field.dword)[0]
            new_dword = (dword & ~mask) | (source_dword & mask)
            struct.pack_into("<I", data, 4 * field.dword, new_dword)
            if 4 * field.dword < self.tx_descriptor_checksum_size:
                changed_bits = dword ^ new_dword
                checksum_change ^= (changed_bits & 0xFFFF) ^ (changed_bits >> 16)
        checksum = struct.unpack_from("<H", data, TX_DESCRIPTOR_CHECKSUM_OFFSET)[0]
        struct.pack_into("<H", data, TX_DESCRIPTOR_CHECKSUM_OFFSET, checksum ^ checksum_change)
        return replace(op, data=bytes(data))

    def carries_only_80211_frames(self, op: UsbOp) -> bool:
        """Bulk IN with no rxdesc24 rpt_sel packet; C2H (rpt_sel) stays in the replay for the driver."""
        return op.transfer_type == TransferType.BULK and "C2H" not in self.rx_packet_kinds(op)

    def rx_packet_kinds(self, op: UsbOp) -> list[str]:
        """"RX" or "C2H" per rxdesc24 packet in a bulk-IN buffer (rtl8xxxu_parse_rxdesc24 replay); a packet
        is C2H when its report_type_dword_and_mask bits are nonzero."""
        report_type_dword, report_type_mask = self.report_type_dword_and_mask
        data = op.data
        kinds: list[str] = []
        offset = 0
        while offset + RXDESC24_SIZE <= len(data):
            dw0 = struct.unpack_from("<I", data, offset)[0]
            report_type = struct.unpack_from("<I", data, offset + 4 * report_type_dword)[0] & report_type_mask
            packet_length = dw0 & 0x3FFF
            driver_info_size = ((dw0 >> 16) & 0xF) * 8
            shift = (dw0 >> 24) & 0x3
            kinds.append("C2H" if report_type else "RX")
            packet_size = packet_length + driver_info_size + shift + RXDESC24_SIZE
            offset += (packet_size + self.rx_packet_alignment - 1) // self.rx_packet_alignment * self.rx_packet_alignment
        return kinds
