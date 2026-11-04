"""Family decoder for MediaTek connac USB chips: dump text, channel-switch and TX trigger recognition."""
from __future__ import annotations

import struct
from dataclasses import replace
from typing import Sequence

from tests.verify_pcap.usb_capture import TransferType, UsbOp
from tests.verify_pcap.verify_capture import FamilyDecoder, RegisterAccess, RegisterOp, TxOpField
from wifit3.chips.mt7925au import constants as C

MCU_TXD_OFFSET = C.SDIO_HDR_SIZE
MCU_SEQ_OFFSET = MCU_TXD_OFFSET + 39
# mt76_mcu msg_seq: 1 .. 15, never 0.
MCU_SEQ_COUNT = 15
UNI_PAYLOAD_OFFSET = C.SDIO_HDR_SIZE + 48
TX_MPDU_OFFSET = C.SDIO_HDR_SIZE + C.MT_SDIO_TXD_SIZE
RXD_PKT_TYPE_SHIFT = 27
RXD_PKT_FLAG_SHIFT = 16
RXD_PKT_FLAG_NORMAL = 0x1

CONTROL_REQUEST_NAMES = {
    C.MT_VEND_READ_EXT: "R ", C.MT_VEND_WRITE_EXT: "W ",
    C.MT_VEND_DEV_MODE: "R uhw", C.MT_VEND_WRITE: "W uhw",
}
UNI_COMMAND_NAMES = {
    C.MCU_UNI_CMD_DEV_INFO_UPDATE: "DEV_INFO", C.MCU_UNI_CMD_BSS_INFO_UPDATE: "BSS_INFO",
    C.MCU_UNI_CMD_HIF_CTRL: "HIF_CTRL", C.MCU_UNI_CMD_BAND_CONFIG: "BAND_CONFIG",
    C.MCU_UNI_CMD_WSYS_CONFIG: "WSYS_CONFIG", C.MCU_UNI_CMD_CHIP_CONFIG: "CHIP_CONFIG",
    C.MCU_UNI_CMD_SET_DOMAIN_INFO: "SET_DOMAIN_INFO", C.MCU_UNI_CMD_SNIFFER: "SNIFFER",
    C.MCU_UNI_CMD_SET_POWER_LIMIT: "SET_POWER_LIMIT", C.MCU_UNI_CMD_EFUSE_CTRL: "EFUSE_CTRL",
}
LEGACY_COMMAND_NAMES = {
    C.MCU_CMD_TARGET_ADDRESS_LEN_REQ: "INIT_DOWNLOAD", C.MCU_CMD_FW_START_REQ: "FW_START",
    C.MCU_CMD_PATCH_START_REQ: "PATCH_START", C.MCU_CMD_PATCH_FINISH_REQ: "PATCH_FINISH",
    C.MCU_CMD_PATCH_SEM_CONTROL: "PATCH_SEM_CONTROL",
}


def is_uni_mcu_frame(data: bytes) -> bool:
    return struct.unpack_from("<H", data, MCU_TXD_OFFSET + 34)[0] != C.MCU_PQ_ID


def rx_packet_type(data: bytes) -> tuple[int, int]:
    """(PKT_TYPE, PKT_FLAG) of a connac3 RX descriptor."""
    rxd0 = struct.unpack_from("<I", data, 0)[0]
    return rxd0 >> RXD_PKT_TYPE_SHIFT, (rxd0 >> RXD_PKT_FLAG_SHIFT) & 0xF


def sniffer_tag(op: UsbOp) -> int | None:
    """The TLV tag of a UNI SNIFFER command on the MCU endpoint, else None."""
    data = op.data
    if (op.transfer_type != TransferType.BULK or op.endpoint != C.EP_OUT_MCU
            or len(data) < UNI_PAYLOAD_OFFSET + 13 or not is_uni_mcu_frame(data)):
        return None
    if struct.unpack_from("<H", data, MCU_TXD_OFFSET + 34)[0] != C.MCU_UNI_CMD_SNIFFER:
        return None
    return struct.unpack_from("<H", data, UNI_PAYLOAD_OFFSET + 4)[0]


def with_byte(op: UsbOp, offset: int, value: int) -> UsbOp:
    return replace(op, data=op.data[:offset] + bytes([value]) + op.data[offset + 1:])


def describe_mcu_command(data: bytes) -> str:
    if len(data) < UNI_PAYLOAD_OFFSET:
        return f"MCU {len(data)}B {data.hex()}"
    seq = data[MCU_SEQ_OFFSET]
    if not is_uni_mcu_frame(data):
        cid = data[MCU_TXD_OFFSET + 36]
        name = LEGACY_COMMAND_NAMES.get(cid, f"cmd 0x{cid:02x}")
        return f"MCU {name} seq {seq} {len(data)}B"
    cid = struct.unpack_from("<H", data, MCU_TXD_OFFSET + 34)[0]
    option = data[MCU_TXD_OFFSET + 43]
    payload = data[UNI_PAYLOAD_OFFSET:]
    text = f"MCU UNI {UNI_COMMAND_NAMES.get(cid, f'0x{cid:02x}')} seq {seq} option 0x{option:02x} {len(data)}B"
    if len(payload) >= 6:
        text += f" tag {struct.unpack_from('<H', payload, 4)[0]}"
    if cid == C.MCU_UNI_CMD_SNIFFER and struct.unpack_from("<H", payload, 4)[0] == C.UNI_SNIFFER_CONFIG:
        text += f" channel {payload[12]}"
    return text + f" payload {payload[:24].hex()}"


class MediatekConnacDecoder(FamilyDecoder):
    def describe(self, op: UsbOp) -> str:
        status = f"  status {op.status}" if op.status else ""
        data = op.data
        if op.transfer_type == TransferType.CONTROL:
            assert op.setup is not None
            address = (op.value << 16) | op.index
            name = CONTROL_REQUEST_NAMES.get(op.request)
            if name is None:
                return f"CTRL setup {op.setup.hex()} data {data.hex()}{status}"
            if len(data) == 4:
                return f"{name} 0x{address:08x} = 0x{struct.unpack('<I', data)[0]:08x}{status}"
            return f"{name} 0x{address:08x}{status}"
        if op.is_in:
            if len(data) < 4:
                return f"IN ep 0x{op.endpoint:02x} {len(data)}B{status}"
            packet_type, packet_flag = rx_packet_type(data)
            if packet_type == C.PKT_TYPE_RX_EVENT and len(data) > C.MT7925_RXD_SEQ_OFF:
                return (f"MCU EVENT ep 0x{op.endpoint:02x} eid 0x{data[C.MT7925_RXD_EID_OFF]:02x} "
                        f"seq {data[C.MT7925_RXD_SEQ_OFF]} flag {packet_flag} {len(data)}B{status}")
            return f"RX ep 0x{op.endpoint:02x} type {packet_type} {len(data)}B{status}"
        if op.endpoint == C.EP_OUT_MCU:
            return describe_mcu_command(data) + status
        if op.endpoint == C.EP_OUT_FW and len(data) >= 4:
            return f"FW/DATA ep 0x04 {len(data)}B tx_bytes {struct.unpack_from('<H', data, 0)[0]}{status}"
        if op.endpoint == C.EP_OUT_HCCA and len(data) >= TX_MPDU_OFFSET + 2:
            frame_control = struct.unpack_from("<H", data, TX_MPDU_OFFSET)[0]
            return (f"TX ep 0x09 {len(data)}B txd {data[C.SDIO_HDR_SIZE:TX_MPDU_OFFSET].hex()} "
                    f"FC 0x{frame_control:04x}{status}")
        return f"OUT ep 0x{op.endpoint:02x} {len(data)}B{status}"

    def injected_frame_at(self, ops: Sequence[UsbOp], position: int) -> bytes | None:
        op = ops[position]
        if op.transfer_type != TransferType.BULK or op.is_in:
            return None
        data = op.data
        if op.endpoint == C.EP_OUT_HCCA and len(data) >= TX_MPDU_OFFSET:
            tx_bytes = struct.unpack_from("<H", data, 0)[0]
            return data[TX_MPDU_OFFSET:C.SDIO_HDR_SIZE + tx_bytes]
        return None

    def channel_change_at(self, ops: Sequence[UsbOp], position: int) -> int | None:
        op = ops[position]
        if op.transfer_type != TransferType.BULK or op.is_in:
            return None
        if sniffer_tag(op) != C.UNI_SNIFFER_ENABLE:
            return None
        # mt7925_change_chanctx (main.c:1781): sniffer enable, then the sniffer config carrying the channel.
        for later in ops[position + 1:]:
            if later.transfer_type == TransferType.BULK and later.endpoint == C.EP_OUT_MCU:
                if sniffer_tag(later) == C.UNI_SNIFFER_CONFIG:
                    return later.data[UNI_PAYLOAD_OFFSET + 12]
                return None
        return None

    def register_ops(self, op: UsbOp) -> list[RegisterOp]:
        """A UNI MCU command only, as one WRITE of its command id valued its payload, little-endian."""
        data = op.data
        if (op.transfer_type != TransferType.BULK or op.endpoint != C.EP_OUT_MCU
                or len(data) < UNI_PAYLOAD_OFFSET or not is_uni_mcu_frame(data)):
            return []
        command_id = struct.unpack_from("<H", data, MCU_TXD_OFFSET + 34)[0]
        return [RegisterOp(RegisterAccess.WRITE, command_id, int.from_bytes(data[UNI_PAYLOAD_OFFSET:], "little"))]

    def skip_capture_only_ops(self, ops: list[UsbOp], positions: set[int]) -> tuple[list[UsbOp], set[int]]:
        """Each later MCU command and its reply renumbered back by the skipped MCU commands before it; a skipped
        command's reply is waived with it."""
        skipped_count = 0
        skipped_seqs: set[int] = set()
        renumbered_seq_by_seq: dict[int, int] = {}
        waived_positions = set(positions)
        renumbered_ops: list[UsbOp] = []
        for position, op in enumerate(ops):
            if (op.transfer_type == TransferType.BULK and op.endpoint == C.EP_OUT_MCU
                    and len(op.data) > MCU_SEQ_OFFSET):
                seq = op.data[MCU_SEQ_OFFSET]
                if position in positions:
                    skipped_count += 1
                    skipped_seqs.add(seq)
                else:
                    skipped_seqs.discard(seq)
                    renumbered_seq_by_seq[seq] = (seq - 1 - skipped_count) % MCU_SEQ_COUNT + 1
                    op = with_byte(op, MCU_SEQ_OFFSET, renumbered_seq_by_seq[seq])
            elif (op.transfer_type == TransferType.BULK and op.is_in and len(op.data) > C.MT7925_RXD_SEQ_OFF
                  and rx_packet_type(op.data)[0] == C.PKT_TYPE_RX_EVENT):
                seq = op.data[C.MT7925_RXD_SEQ_OFF]
                if seq in skipped_seqs:
                    skipped_seqs.discard(seq)
                    waived_positions.add(position)
                elif seq in renumbered_seq_by_seq:
                    op = with_byte(op, C.MT7925_RXD_SEQ_OFF, renumbered_seq_by_seq[seq])
            renumbered_ops.append(op)
        return renumbered_ops, waived_positions

    def with_tx_op_fields(self, op: UsbOp, fields: list[TxOpField], source: UsbOp) -> UsbOp:
        """A TX op on the HCCA endpoint with ``source``'s bits in each field; the connac TXD has no checksum."""
        if (op.transfer_type != TransferType.BULK or op.is_in or op.endpoint != C.EP_OUT_HCCA
                or len(source.data) != len(op.data)):
            return op
        data = bytearray(op.data)
        for field in fields:
            if 4 * field.dword + 4 > len(data):
                continue
            mask = ((1 << field.width) - 1) << field.shift
            dword = struct.unpack_from("<I", data, 4 * field.dword)[0]
            source_dword = struct.unpack_from("<I", source.data, 4 * field.dword)[0]
            struct.pack_into("<I", data, 4 * field.dword, (dword & ~mask) | (source_dword & mask))
        return replace(op, data=bytes(data))

    def carries_only_80211_frames(self, op: UsbOp) -> bool:
        """connac3 bulk IN: PKT_TYPE_RX_EVENT is an MCU reply (kept) unless PKT_FLAG marks a tunnelled frame."""
        if op.transfer_type != TransferType.BULK or len(op.data) < 4:
            return False
        packet_type, packet_flag = rx_packet_type(op.data)
        return packet_type != C.PKT_TYPE_RX_EVENT or packet_flag == RXD_PKT_FLAG_NORMAL
