"""Remove the owner's airspace from a driver usbmon capture (pcapng) so it can be shared publicly.

    uv run python -m wifit3.scripts.clean_pcap <capture.pcap> <cleaned.pcap>
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import struct
import zlib
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

PCAPNG_ENHANCED_PACKET_BLOCK = 6
EPB_FIXED_SIZE = 28

USBMON_HEADER_SIZE = 64
URB_SUBMIT = 0x53
URB_COMPLETE = 0x43
TRANSFER_CONTROL = 2
TRANSFER_BULK = 3
USB_DIR_IN = 0x80
USB_TYPE_MASK = 0x60
USB_TYPE_VENDOR = 0x40

FCS_SIZE = 4
MIN_FRAME_SIZE = 10
FRAME_TYPE_MANAGEMENT = 0
FRAME_TYPE_CONTROL = 1
FRAME_TYPE_DATA = 2
SUBTYPE_BEACON = 8
CONTROL_SUBTYPES_WITHOUT_ADDR2 = (0xC, 0xD)
# Offset of the first element after the fixed fields, per management subtype.
MANAGEMENT_ELEMENTS_OFFSET = {0: 4, 1: 6, 2: 10, 3: 6, 4: 0, 5: 12, 8: 12}
ELEMENT_ID_SSID = 0
# Shorter ESSIDs are only replaced inside SSID elements: searching the body for them would hit unrelated bytes.
MIN_SSID_LENGTH_SEARCHED_IN_BODY = 4
MAX_KEPT_UNICAST_DESTINATIONS = 5

VENDOR_REQUEST_REALTEK = 0x05
VENDOR_REQUESTS_RALINK = (0x06, 0x07)
VENDOR_REQUEST_RALINK_WRITE = 0x06
VENDOR_REQUEST_RALINK_READ = 0x07
VENDOR_REQUESTS_CONNAC2 = (0x63, 0x66)
VENDOR_REQUEST_ATH9K_FIRMWARE_DOWNLOAD = 0x30


@dataclass
class UsbPacket:
    """One usbmon record (64-byte mmapped header + payload) from a pcapng Enhanced Packet Block."""
    block: bytes
    header: bytearray
    data: bytes
    changed: bool = False

    @property
    def urb_type(self) -> int:
        return self.header[8]

    @property
    def transfer_type(self) -> int:
        return self.header[9]

    @property
    def endpoint(self) -> int:
        return self.header[10]

    @property
    def device(self) -> int:
        return self.header[11]

    @property
    def request_type(self) -> int:
        return self.header[40]

    @property
    def request(self) -> int:
        return self.header[41]

    @property
    def value(self) -> int:
        return struct.unpack_from("<H", self.header, 42)[0]

    @property
    def index(self) -> int:
        return struct.unpack_from("<H", self.header, 44)[0]

    @property
    def is_vendor_control_submit(self) -> bool:
        return (self.transfer_type == TRANSFER_CONTROL and self.urb_type == URB_SUBMIT
                and self.request_type & USB_TYPE_MASK == USB_TYPE_VENDOR)

    def replace_data(self, data: bytes) -> None:
        """Swap the payload; the URB length and captured length follow it."""
        self.data = data
        self.changed = True


class RxUnitKind(Enum):
    FRAME = 1
    NOT_A_FRAME = 2
    SPLIT_FRAME = 3


@dataclass
class RxUnit:
    """One packet inside a bulk-IN transfer: descriptor + payload, without inter-packet padding."""
    kind: RxUnitKind
    data: bytearray
    frame_start: int = 0
    frame_end: int = 0
    hardware_error: bool = False


@dataclass
class ControlTransfer:
    request: int
    is_read: bool
    value: int
    index: int
    data: bytes


def vendor_control_transfers(card_packets: list[UsbPacket], requests: tuple[int, ...]) -> list[ControlTransfer]:
    """Vendor control transfers in submit order; a read carries the data of its completion."""
    transfers = []
    pending_reads: dict[bytes, ControlTransfer] = {}
    for packet in card_packets:
        urb_id = bytes(packet.header[0:8])
        if packet.is_vendor_control_submit and packet.request in requests:
            transfer = ControlTransfer(packet.request, bool(packet.request_type & USB_DIR_IN), packet.value,
                                       packet.index, packet.data)
            transfers.append(transfer)
            if transfer.is_read:
                pending_reads[urb_id] = transfer
        elif (packet.transfer_type == TRANSFER_CONTROL and packet.urb_type == URB_COMPLETE
              and urb_id in pending_reads):
            pending_reads.pop(urb_id).data = packet.data
    return transfers


def mac_register_values(writes: list[tuple[int, bytes]], mac_register: int) -> set[bytes]:
    """Every value the 6-byte MAC register holds after a write that reaches its last byte."""
    register = bytearray(6)
    written = [False] * 6
    values: set[bytes] = set()
    for address, data in writes:
        for position, byte in enumerate(data):
            offset = address + position - mac_register
            if 0 <= offset < 6:
                register[offset] = byte
                written[offset] = True
        if address <= mac_register + 5 < address + len(data) and all(written) and any(register):
            values.add(bytes(register))
    return values


class CaptureLayout(ABC):
    """How one chip family packs 802.11 frames into its bulk transfers."""
    ADAPTER_MAC_SOURCE = ""

    @abstractmethod
    def split_rx(self, buffer: bytes) -> list[RxUnit] | None:
        """The packets of one bulk-IN transfer, or None when it does not parse."""

    def join_rx(self, units: list[RxUnit]) -> bytes:
        """Rebuild a bulk-IN transfer from the units kept."""
        if not units:
            return b""
        return bytes(units[0].data)

    @abstractmethod
    def tx_frame_span(self, buffer: bytes) -> tuple[int, int] | None:
        """(start, end) of the 802.11 frame in a bulk-OUT transfer, or None if it carries none."""

    @abstractmethod
    def adapter_macs(self, card_packets: list[UsbPacket]) -> set[bytes]:
        """The adapter's own MAC address(es), as the host programs or reads them."""


class RealtekRxdesc24Layout(CaptureLayout):
    """rtl8xxxu gen2, the vendor DKMS drivers and rtw88: 24-byte RX descriptor, 8-byte aligned aggregates."""
    ADAPTER_MAC_SOURCE = ("vendor control writes to REG_MACID 0x0610, and every 6-byte run of the efuse "
                          "logical map decoded from REG_EFUSE_CTRL 0x30 reads")
    RX_DESC_SIZE = 24
    REG_MACID = 0x0610
    REG_EFUSE_CTRL = 0x0030
    EFUSE_CTRL_READ_DONE = 1 << 31
    EFUSE_MAP_SIZE = 512
    # The 8 TID queues, HIGH and MGNT.
    FRAME_QUEUE_SELECTS = frozenset(range(8)) | {0x11, 0x12}
    MIN_TX_DESC_SIZE = 32

    def split_rx(self, buffer: bytes) -> list[RxUnit] | None:
        if len(buffer) < self.RX_DESC_SIZE:
            return None
        units = []
        offset = 0
        while len(buffer) - offset >= self.RX_DESC_SIZE:
            dw0, _dw1, dw2 = struct.unpack_from("<III", buffer, offset)
            packet_length = dw0 & 0x3FFF
            frame_start = self.RX_DESC_SIZE + ((dw0 >> 16) & 0xF) * 8 + ((dw0 >> 24) & 0x3)
            unit_end = offset + frame_start + packet_length
            if unit_end > len(buffer):
                return None
            data = bytearray(buffer[offset:unit_end])
            if dw2 & (1 << 28):
                units.append(RxUnit(RxUnitKind.NOT_A_FRAME, data))
            else:
                units.append(RxUnit(RxUnitKind.FRAME, data, frame_start, frame_start + packet_length,
                                    bool(dw0 & (1 << 14))))
            offset = (unit_end + 7) // 8 * 8
        return units

    def join_rx(self, units: list[RxUnit]) -> bytes:
        buffer = bytearray()
        for unit in units:
            buffer += bytes(-len(buffer) % 8)
            buffer += unit.data
        return bytes(buffer)

    def tx_frame_span(self, buffer: bytes) -> tuple[int, int] | None:
        if len(buffer) < self.MIN_TX_DESC_SIZE:
            return None
        dw0, dw1 = struct.unpack_from("<II", buffer, 0)
        packet_size = dw0 & 0xFFFF
        desc_size = (dw0 >> 16) & 0xFF
        queue_select = (dw1 >> 8) & 0x1F
        if (queue_select not in self.FRAME_QUEUE_SELECTS or desc_size < self.MIN_TX_DESC_SIZE
                or packet_size < MIN_FRAME_SIZE or desc_size + packet_size > len(buffer)):
            return None
        return desc_size, desc_size + packet_size

    def adapter_macs(self, card_packets: list[UsbPacket]) -> set[bytes]:
        transfers = vendor_control_transfers(card_packets, (VENDOR_REQUEST_REALTEK,))
        macs = mac_register_values([(transfer.value, transfer.data) for transfer in transfers
                                    if not transfer.is_read], self.REG_MACID)
        physical_efuse: dict[int, int] = {}
        for transfer in transfers:
            if transfer.is_read and transfer.value == self.REG_EFUSE_CTRL and len(transfer.data) == 4:
                efuse_ctrl = struct.unpack("<I", transfer.data)[0]
                if efuse_ctrl & self.EFUSE_CTRL_READ_DONE:
                    physical_efuse[(efuse_ctrl >> 8) & 0x3FF] = efuse_ctrl & 0xFF
        logical_efuse = bytearray(b"\xff" * self.EFUSE_MAP_SIZE)
        address = 0
        while physical_efuse.get(address, 0xFF) != 0xFF:
            header = physical_efuse[address]
            address += 1
            if header & 0x1F == 0x0F:
                extended_header = physical_efuse.get(address, 0xFF)
                address += 1
                section = ((header & 0xE0) >> 5) | ((extended_header & 0xF0) >> 1)
                word_disabled = extended_header & 0x0F
            else:
                section = header >> 4
                word_disabled = header & 0x0F
            map_address = section * 8
            for word in range(4):
                if not word_disabled & (1 << word):
                    for map_byte in (map_address, map_address + 1):
                        if map_byte < len(logical_efuse):
                            logical_efuse[map_byte] = physical_efuse.get(address, 0xFF)
                        address += 1
                map_address += 2
        # The MAC sits at a chip-specific offset of the logical map, so every 6-byte run counts.
        for offset in range(len(logical_efuse) - 5):
            window = bytes(logical_efuse[offset:offset + 6])
            if is_unicast(window) and any(window):
                macs.add(window)
        return macs


class Rtl8187Layout(CaptureLayout):
    """rtl8187 (hard MAC): one frame per transfer, RX descriptor trails the frame."""
    ADAPTER_MAC_SOURCE = ("vendor control writes to MAC 0xFF00, and EEPROM words 7..9 decoded from the "
                          "93C46 bit-banging on EEPROM_CMD 0xFF50")
    RX_TRAILER_SIZES = (16, 20)
    TX_HEADER_SIZES = (12, 32)
    RX_FLAG_CRC32_ERROR = 1 << 13
    REG_MAC = 0xFF00
    REG_EEPROM_CMD = 0xFF50
    EEPROM_CMD_READ = 1 << 0
    EEPROM_CMD_WRITE = 1 << 1
    EEPROM_CMD_CK = 1 << 2
    EEPROM_CMD_CS = 1 << 3
    EEPROM_READ_OPCODE_BITS = [1, 1, 0]
    EEPROM_MAC_WORDS = (7, 8, 9)

    def split_rx(self, buffer: bytes) -> list[RxUnit] | None:
        for trailer_size in self.RX_TRAILER_SIZES:
            if len(buffer) < trailer_size + MIN_FRAME_SIZE:
                continue
            flags = struct.unpack_from("<I", buffer, len(buffer) - trailer_size)[0]
            frame_length = flags & 0x0FFF
            if frame_length + trailer_size == len(buffer):
                return [RxUnit(RxUnitKind.FRAME, bytearray(buffer), 0, frame_length,
                               bool(flags & self.RX_FLAG_CRC32_ERROR))]
        return None

    def tx_frame_span(self, buffer: bytes) -> tuple[int, int] | None:
        if len(buffer) < 4:
            return None
        frame_length = struct.unpack_from("<I", buffer, 0)[0] & 0x0FFF
        for header_size in self.TX_HEADER_SIZES:
            if header_size + frame_length == len(buffer) and frame_length >= MIN_FRAME_SIZE:
                return header_size, len(buffer)
        return None

    def adapter_macs(self, card_packets: list[UsbPacket]) -> set[bytes]:
        transfers = vendor_control_transfers(card_packets, (VENDOR_REQUEST_REALTEK,))
        macs = mac_register_values([(transfer.value, transfer.data) for transfer in transfers
                                    if not transfer.is_read and transfer.index == 0], self.REG_MAC)
        eeprom_words: dict[int, int] = {}
        command_bits: list[int] = []
        data_bits: list[int] = []
        clock = False
        rising_edge_data_in: int | None = None
        for transfer in transfers:
            if transfer.value != self.REG_EEPROM_CMD or transfer.index != 0 or not transfer.data:
                continue
            register = transfer.data[0]
            if transfer.is_read:
                if rising_edge_data_in is not None:
                    data_bits.append(1 if register & self.EEPROM_CMD_READ else 0)
                    rising_edge_data_in = None
                continue
            if rising_edge_data_in is not None:
                command_bits.append(rising_edge_data_in)
                rising_edge_data_in = None
            if not register & self.EEPROM_CMD_CS:
                # command_bits[0] is the clock pulse that opens the session, before the opcode.
                if len(data_bits) == 16 and command_bits[1:4] == self.EEPROM_READ_OPCODE_BITS:
                    word = 0
                    for bit in command_bits[4:]:
                        word = (word << 1) | bit
                    value = 0
                    for bit in data_bits:
                        value = (value << 1) | bit
                    eeprom_words[word] = value
                command_bits = []
                data_bits = []
            elif register & self.EEPROM_CMD_CK and not clock:
                rising_edge_data_in = 1 if register & self.EEPROM_CMD_WRITE else 0
            clock = bool(register & self.EEPROM_CMD_CK)
        if all(word in eeprom_words for word in self.EEPROM_MAC_WORDS):
            macs.add(b"".join(struct.pack("<H", eeprom_words[word]) for word in self.EEPROM_MAC_WORDS))
        return macs


class Rt2800Layout(CaptureLayout):
    """rt2800usb: RXINFO + RXWI + frame + RXD, one frame per transfer; TXINFO + TXWI + frame."""
    ADAPTER_MAC_SOURCE = ("vendor control writes to MAC_ADDR_DW0 0x1008, and EEPROM bytes 4..9 from "
                          "USB_EEPROM_READ or the EFUSE_CTRL 0x0580 / EFUSE_DATA 0x0590..0x059C reads")
    RXINFO_SIZE = 4
    RXD_SIZE = 4
    TXINFO_SIZE = 4
    RXD_CRC_ERROR = 1 << 8
    RXD_L2PAD = 1 << 14
    MAC_CSR0 = 0x1000
    MAC_ADDR_DW0 = 0x1008
    SILICON_RT5592 = 0x5592
    VENDOR_REQUEST_EEPROM_READ = 0x09
    EFUSE_CTRL = 0x0580
    # The efuse returns a 16-byte block in EFUSE_DATA3..DATA0 order.
    EFUSE_DATA_BLOCK_OFFSETS = {0x059C: 0, 0x0598: 4, 0x0594: 8, 0x0590: 12}
    EEPROM_MAC_OFFSET = 4

    def __init__(self, silicon: int):
        self.rxwi_size = 24 if silicon == self.SILICON_RT5592 else 16
        self.txwi_size = 20 if silicon == self.SILICON_RT5592 else 16

    def split_rx(self, buffer: bytes) -> list[RxUnit] | None:
        if len(buffer) < self.RXINFO_SIZE + self.rxwi_size + self.RXD_SIZE:
            return None
        packet_length = struct.unpack_from("<I", buffer, 0)[0] & 0xFFFF
        rxd_offset = self.RXINFO_SIZE + packet_length
        if rxd_offset + self.RXD_SIZE > len(buffer):
            return None
        mpdu_length = (struct.unpack_from("<I", buffer, self.RXINFO_SIZE)[0] >> 16) & 0x0FFF
        rxd = struct.unpack_from("<I", buffer, rxd_offset)[0]
        frame_start = self.RXINFO_SIZE + self.rxwi_size
        frame_end = frame_start + mpdu_length
        if rxd & self.RXD_L2PAD:
            frame_end += 2
        if frame_end > rxd_offset:
            return None
        return [RxUnit(RxUnitKind.FRAME, bytearray(buffer), frame_start, frame_end,
                       bool(rxd & self.RXD_CRC_ERROR))]

    def tx_frame_span(self, buffer: bytes) -> tuple[int, int] | None:
        frame_start = self.TXINFO_SIZE + self.txwi_size
        if len(buffer) < frame_start:
            return None
        mpdu_length = (struct.unpack_from("<I", buffer, self.TXINFO_SIZE + 4)[0] >> 16) & 0x0FFF
        if mpdu_length < MIN_FRAME_SIZE or frame_start + mpdu_length > len(buffer):
            return None
        return frame_start, frame_start + mpdu_length

    def adapter_macs(self, card_packets: list[UsbPacket]) -> set[bytes]:
        transfers = vendor_control_transfers(
            card_packets, (VENDOR_REQUEST_RALINK_WRITE, VENDOR_REQUEST_RALINK_READ, self.VENDOR_REQUEST_EEPROM_READ))
        macs = mac_register_values([(transfer.index, transfer.data) for transfer in transfers
                                    if transfer.request == VENDOR_REQUEST_RALINK_WRITE], self.MAC_ADDR_DW0)
        eeprom: dict[int, int] = {}
        efuse_block: int | None = None
        for transfer in transfers:
            if transfer.request == self.VENDOR_REQUEST_EEPROM_READ and transfer.is_read:
                for position, byte in enumerate(transfer.data):
                    eeprom[transfer.index + position] = byte
            elif (transfer.request == VENDOR_REQUEST_RALINK_WRITE and transfer.index == self.EFUSE_CTRL
                  and len(transfer.data) == 4):
                efuse_block = ((struct.unpack("<I", transfer.data)[0] >> 17) & 0x1FF) * 2
            elif (transfer.request == VENDOR_REQUEST_RALINK_READ and efuse_block is not None
                  and transfer.index in self.EFUSE_DATA_BLOCK_OFFSETS and len(transfer.data) == 4):
                for position, byte in enumerate(transfer.data):
                    eeprom[efuse_block + self.EFUSE_DATA_BLOCK_OFFSETS[transfer.index] + position] = byte
        mac_offsets = range(self.EEPROM_MAC_OFFSET, self.EEPROM_MAC_OFFSET + 6)
        if all(offset in eeprom for offset in mac_offsets):
            macs.add(bytes(eeprom[offset] for offset in mac_offsets))
        return macs


class Connac2Layout(CaptureLayout):
    """MediaTek connac2 USB (mt7921au): RXD groups + frame, one per transfer; SDIO header + 64-byte TXD."""
    ADAPTER_MAC_SOURCE = "GET_NIC_CAPAB firmware reply (MAC address TLV) on the RX pipe"
    PKT_TYPE_NORMAL = 2
    PKT_TYPE_RX_EVENT = 7
    RXD1_GROUP_1 = 1 << 11
    RXD1_GROUP_2 = 1 << 12
    RXD1_GROUP_3 = 1 << 13
    RXD1_GROUP_4 = 1 << 14
    RXD1_GROUP_5 = 1 << 15
    RXD1_FCS_ERROR = 1 << 27
    TX_HEADER_SIZE = 4 + 64
    HDR_FORMAT_802_11 = 2
    EVENT_NIC_CAPABILITY = 0xEC
    NIC_CAPAB_PAYLOAD_OFFSET = 36
    NIC_CAP_MAC_ADDR = 0x07

    def split_rx(self, buffer: bytes) -> list[RxUnit] | None:
        if len(buffer) < 24:
            return None
        rxd0, rxd1, rxd2 = struct.unpack_from("<III", buffer, 0)
        packet_type = (rxd0 >> 27) & 0x1F
        is_mcu_frame = packet_type == self.PKT_TYPE_RX_EVENT and (rxd0 >> 16) & 0xF == 0x1
        if packet_type != self.PKT_TYPE_NORMAL and not is_mcu_frame:
            return [RxUnit(RxUnitKind.NOT_A_FRAME, bytearray(buffer))]
        words = 6
        if rxd1 & self.RXD1_GROUP_4:
            words += 4
        if rxd1 & self.RXD1_GROUP_1:
            words += 4
        if rxd1 & self.RXD1_GROUP_2:
            words += 2
        if rxd1 & self.RXD1_GROUP_3:
            words += 2
            if rxd1 & self.RXD1_GROUP_5:
                words += 18
        frame_start = words * 4 + 2 * ((rxd2 >> 14) & 0x3)
        frame_end = min(rxd0 & 0xFFFF, len(buffer))
        if frame_start >= frame_end:
            return None
        return [RxUnit(RxUnitKind.FRAME, bytearray(buffer), frame_start, frame_end,
                       bool(rxd1 & self.RXD1_FCS_ERROR))]

    def tx_frame_span(self, buffer: bytes) -> tuple[int, int] | None:
        if len(buffer) < self.TX_HEADER_SIZE:
            return None
        sdio_header, txd0, txd1 = struct.unpack_from("<III", buffer, 0)
        frame_length = sdio_header - (self.TX_HEADER_SIZE - 4)
        if (txd0 & 0xFFFF != sdio_header or (txd1 >> 16) & 0x3 != self.HDR_FORMAT_802_11
                or frame_length < MIN_FRAME_SIZE or self.TX_HEADER_SIZE + frame_length > len(buffer)):
            return None
        return self.TX_HEADER_SIZE, self.TX_HEADER_SIZE + frame_length

    def adapter_macs(self, card_packets: list[UsbPacket]) -> set[bytes]:
        macs: set[bytes] = set()
        for packet in card_packets:
            reply = packet.data
            if (packet.transfer_type != TRANSFER_BULK or not packet.endpoint & USB_DIR_IN
                    or len(reply) < self.NIC_CAPAB_PAYLOAD_OFFSET + 4
                    or (struct.unpack_from("<I", reply, 0)[0] >> 27) != self.PKT_TYPE_RX_EVENT
                    or reply[28] != self.EVENT_NIC_CAPABILITY):
                continue
            offset = self.NIC_CAPAB_PAYLOAD_OFFSET
            element_count = struct.unpack_from("<H", reply, offset)[0]
            offset += 4
            for _ in range(element_count):
                if offset + 8 > len(reply):
                    break
                tlv_type, tlv_length = struct.unpack_from("<II", reply, offset)
                offset += 8
                if tlv_type == self.NIC_CAP_MAC_ADDR and tlv_length >= 6 and offset + 6 <= len(reply):
                    macs.add(bytes(reply[offset:offset + 6]))
                offset += tlv_length
        return macs


class AthHtcLayout(CaptureLayout):
    """ath9k_htc (AR9271): HIF stream of HTC packets, 4-byte aligned, a packet may span two transfers."""
    ADAPTER_MAC_SOURCE = "WMI_VAP_CREATE_CMDID (myaddr) on the register-out pipe 0x04"
    RX_STREAM_TAG = 0x4E00
    TX_STREAM_TAG = 0x697E
    HTC_FRAME_HDR_SIZE = 8
    HTC_FLAGS_RECV_TRAILER = 0x02
    RX_STATUS_SIZE = 40
    TX_MGMT_HDR_SIZE = 8
    TX_FRAME_HDR_SIZE = 12
    MGMT_ENDPOINT_ID = 5
    # rs_status bits the hardware sets on a frame it could not receive: CRC, PHY, FIFO, corrupt desc.
    RX_STATUS_ERRORS = 0x01 | 0x02 | 0x04 | 0x40
    REG_OUT_ENDPOINT = 0x04
    WMI_VAP_CREATE_CMDID = 0x13

    def __init__(self) -> None:
        self.split_frame_bytes_pending = 0

    def split_rx(self, buffer: bytes) -> list[RxUnit] | None:
        units = []
        offset = 0
        if self.split_frame_bytes_pending:
            offset = min(self.split_frame_bytes_pending, len(buffer))
            self.split_frame_bytes_pending -= offset
            units.append(RxUnit(RxUnitKind.SPLIT_FRAME, bytearray(buffer[:offset])))
        while offset + 4 <= len(buffer):
            packet_length, tag = struct.unpack_from("<HH", buffer, offset)
            if tag != self.RX_STREAM_TAG:
                return None
            unit_end = offset + 4 + packet_length
            padded_end = unit_end + (-packet_length % 4)
            if unit_end > len(buffer):
                self.split_frame_bytes_pending = padded_end - len(buffer)
                units.append(RxUnit(RxUnitKind.SPLIT_FRAME, bytearray(buffer[offset:])))
                return units
            data = bytearray(buffer[offset:unit_end])
            frame_start = 4 + self.HTC_FRAME_HDR_SIZE + self.RX_STATUS_SIZE
            if len(data) < frame_start:
                units.append(RxUnit(RxUnitKind.NOT_A_FRAME, data))
            else:
                frame_end = len(data)
                if data[5] & self.HTC_FLAGS_RECV_TRAILER:
                    frame_end -= data[8]
                data_length = struct.unpack_from(">H", data, frame_start - self.RX_STATUS_SIZE + 8)[0]
                status = data[frame_start - self.RX_STATUS_SIZE + 10]
                if frame_start + data_length != frame_end:
                    return None
                units.append(RxUnit(RxUnitKind.FRAME, data, frame_start, frame_end,
                                    bool(status & self.RX_STATUS_ERRORS)))
            offset = padded_end
        return units

    def join_rx(self, units: list[RxUnit]) -> bytes:
        buffer = bytearray()
        for unit in units:
            buffer += unit.data
            buffer += bytes(-len(buffer) % 4)
        return bytes(buffer)

    def tx_frame_span(self, buffer: bytes) -> tuple[int, int] | None:
        if len(buffer) < 4 + self.HTC_FRAME_HDR_SIZE:
            return None
        packet_length, tag = struct.unpack_from("<HH", buffer, 0)
        if tag != self.TX_STREAM_TAG or 4 + packet_length > len(buffer):
            return None
        if buffer[4] == self.MGMT_ENDPOINT_ID:
            frame_start = 4 + self.HTC_FRAME_HDR_SIZE + self.TX_MGMT_HDR_SIZE
        else:
            frame_start = 4 + self.HTC_FRAME_HDR_SIZE + self.TX_FRAME_HDR_SIZE
        if 4 + packet_length - frame_start < MIN_FRAME_SIZE:
            return None
        return frame_start, 4 + packet_length

    def adapter_macs(self, card_packets: list[UsbPacket]) -> set[bytes]:
        macs: set[bytes] = set()
        for packet in card_packets:
            command = packet.data
            if (packet.urb_type == URB_SUBMIT and packet.endpoint == self.REG_OUT_ENDPOINT
                    and len(command) >= self.HTC_FRAME_HDR_SIZE + 4 + 8
                    and struct.unpack_from(">H", command, self.HTC_FRAME_HDR_SIZE)[0]
                    == self.WMI_VAP_CREATE_CMDID):
                payload_start = self.HTC_FRAME_HDR_SIZE + 4
                macs.add(bytes(command[payload_start + 2:payload_start + 8]))
        return macs


def detect_layout(card_packets: list[UsbPacket]) -> CaptureLayout:
    """Pick the chip family from the card's vendor control traffic."""
    requests = Counter(packet.request for packet in card_packets if packet.is_vendor_control_submit)
    if VENDOR_REQUEST_ATH9K_FIRMWARE_DOWNLOAD in requests:
        return AthHtcLayout()
    top_request = requests.most_common(1)[0][0]
    if top_request == VENDOR_REQUEST_REALTEK:
        writes = [transfer for transfer in vendor_control_transfers(card_packets, (VENDOR_REQUEST_REALTEK,))
                  if not transfer.is_read]
        csr_writes = [transfer for transfer in writes if transfer.value >= Rtl8187Layout.REG_MAC]
        if len(csr_writes) * 2 > len(writes):
            return Rtl8187Layout()
        return RealtekRxdesc24Layout()
    if top_request in VENDOR_REQUESTS_CONNAC2:
        return Connac2Layout()
    if top_request in VENDOR_REQUESTS_RALINK:
        for transfer in vendor_control_transfers(card_packets, (VENDOR_REQUEST_RALINK_READ,)):
            if transfer.index == Rt2800Layout.MAC_CSR0 and len(transfer.data) == 4:
                silicon = struct.unpack("<I", transfer.data)[0] >> 16
                # rt2800 silicon ids are 0x2xxx/0x3xxx/0x5xxx/0x6xxx; 0x7xxx is a MediaTek mt76.
                if silicon >> 12 in (2, 3, 5, 6):
                    return Rt2800Layout(silicon)
                break
    raise SystemExit(f"unsupported capture structure (vendor requests {dict(requests)})")


def frame_address_offsets(frame: bytes | bytearray, frame_end: int) -> list[int]:
    """Offsets of the address fields present in an 802.11 header."""
    frame_type = (frame[0] >> 2) & 0x3
    subtype = frame[0] >> 4
    if frame_type == FRAME_TYPE_CONTROL and subtype in CONTROL_SUBTYPES_WITHOUT_ADDR2:
        offsets = [4]
    elif frame_type == FRAME_TYPE_CONTROL:
        offsets = [4, 10]
    elif frame_type == FRAME_TYPE_DATA and frame[1] & 0x3 == 0x3:
        offsets = [4, 10, 16, 24]
    elif frame_type in (FRAME_TYPE_MANAGEMENT, FRAME_TYPE_DATA):
        offsets = [4, 10, 16]
    else:
        offsets = []
    return [offset for offset in offsets if offset + 6 <= frame_end]


def is_unicast(mac: bytes | bytearray) -> bool:
    return not mac[0] & 0x01


def frame_end_without_fcs(frame: bytes | bytearray) -> int:
    """Length of the frame minus its FCS when it ends in a valid one."""
    if (len(frame) >= MIN_FRAME_SIZE + FCS_SIZE
            and zlib.crc32(frame[:-FCS_SIZE]) == struct.unpack_from("<I", frame, len(frame) - FCS_SIZE)[0]):
        return len(frame) - FCS_SIZE
    return len(frame)


def ssid_element_spans(frame: bytes | bytearray, frame_end: int) -> list[tuple[int, int]]:
    """(start, end) of each non-hidden SSID element payload in an unprotected management frame."""
    subtype = frame[0] >> 4
    if ((frame[0] >> 2) & 0x3 != FRAME_TYPE_MANAGEMENT or subtype not in MANAGEMENT_ELEMENTS_OFFSET
            or frame[1] & 0x40):
        return []
    spans = []
    position = 24 + MANAGEMENT_ELEMENTS_OFFSET[subtype]
    while position + 2 <= frame_end:
        element_end = position + 2 + frame[position + 1]
        if element_end > frame_end:
            break
        if frame[position] == ELEMENT_ID_SSID and any(frame[position + 2:element_end]):
            spans.append((position + 2, element_end))
        position = element_end
    return spans


class FrameCleaner:
    """Rewrites one 802.11 frame in place: keyed-hash MACs and ESSIDs, zeroed data bodies, fresh FCS."""

    def __init__(self, mac_replacements: dict[bytes, bytes], ssid_replacements: dict[bytes, bytes]):
        self.mac_replacements = mac_replacements
        self.ssid_replacements = ssid_replacements
        self.ssids_searched_in_body = sorted((ssid for ssid in ssid_replacements
                                              if len(ssid) >= MIN_SSID_LENGTH_SEARCHED_IN_BODY),
                                             key=len, reverse=True)

    def clean(self, frame: bytearray) -> None:
        frame_end = frame_end_without_fcs(frame)
        has_fcs = frame_end != len(frame)
        for offset in frame_address_offsets(frame, frame_end):
            mac = bytes(frame[offset:offset + 6])
            if mac in self.mac_replacements:
                frame[offset:offset + 6] = self.mac_replacements[mac]

        frame_type = (frame[0] >> 2) & 0x3
        if frame_type == FRAME_TYPE_DATA:
            header_length = 24
            if frame[1] & 0x3 == 0x3:
                header_length += 6
            if (frame[0] >> 4) & 0x8:
                header_length += 2
                if frame[1] & 0x80:
                    header_length += 4
            frame[header_length:frame_end] = bytes(max(0, frame_end - header_length))
        elif frame_type == FRAME_TYPE_MANAGEMENT and frame_end > 24:
            body = bytes(frame[24:frame_end])
            for mac, fake in self.mac_replacements.items():
                body = body.replace(mac, fake)
            for ssid in self.ssids_searched_in_body:
                body = body.replace(ssid, self.ssid_replacements[ssid])
            frame[24:frame_end] = body
            for start, end in ssid_element_spans(frame, frame_end):
                ssid = bytes(frame[start:end])
                if ssid in self.ssid_replacements:
                    frame[start:end] = self.ssid_replacements[ssid]

        if has_fcs:
            struct.pack_into("<I", frame, frame_end, zlib.crc32(frame[:frame_end]))


def fake_mac(key: bytes, mac: bytes) -> bytes:
    """Keyed-hash MAC with the locally-administered bit set and the multicast bit clear."""
    digest = bytearray(hmac.new(key, b"mac" + mac, hashlib.sha256).digest()[:6])
    digest[0] = (digest[0] & 0xFC) | 0x02
    return bytes(digest)


def fake_ssid(key: bytes, ssid: bytes) -> bytes:
    """Keyed-hash ESSID of the same length, in lowercase hex characters."""
    digest = hmac.new(key, b"ssid" + ssid, hashlib.sha256).hexdigest().encode()
    return (digest * (len(ssid) // len(digest) + 1))[:len(ssid)]


def clean_pcapng(capture: bytes, key: bytes) -> bytes:
    """The cleaned pcapng; prints what was kept, dropped and rewritten."""
    blocks: list[bytes] = []
    packets: list[UsbPacket] = []
    packet_block_index: list[int] = []
    offset = 0
    while offset + 12 <= len(capture):
        block_type, block_length = struct.unpack_from("<II", capture, offset)
        block = capture[offset:offset + block_length]
        if block_type == PCAPNG_ENHANCED_PACKET_BLOCK:
            captured_length = struct.unpack_from("<I", block, 20)[0]
            record = block[EPB_FIXED_SIZE:EPB_FIXED_SIZE + captured_length]
            if len(record) >= USBMON_HEADER_SIZE:
                packets.append(UsbPacket(block, bytearray(record[:USBMON_HEADER_SIZE]),
                                         record[USBMON_HEADER_SIZE:]))
                packet_block_index.append(len(blocks))
        blocks.append(block)
        offset += block_length

    vendor_requests_by_device = Counter(packet.device for packet in packets if packet.is_vendor_control_submit)
    if not vendor_requests_by_device:
        raise SystemExit("no vendor control traffic: no Wi-Fi adapter found")
    card = vendor_requests_by_device.most_common(1)[0][0]
    card_packets = [packet for packet in packets if packet.device == card]
    layout = detect_layout(card_packets)
    adapter_macs = layout.adapter_macs(card_packets)

    rx_packets = [packet for packet in card_packets
                  if packet.transfer_type == TRANSFER_BULK and packet.endpoint & USB_DIR_IN
                  and packet.urb_type == URB_COMPLETE and packet.data]
    rx_units = [layout.split_rx(packet.data) for packet in rx_packets]
    tx_packets = [packet for packet in card_packets
                  if packet.transfer_type == TRANSFER_BULK and not packet.endpoint & USB_DIR_IN
                  and packet.urb_type == URB_SUBMIT and packet.data]
    tx_spans = [layout.tx_frame_span(packet.data) for packet in tx_packets]

    received_frames = [bytes(unit.data[unit.frame_start:unit.frame_end])
                       for units in rx_units for unit in units or []
                       if unit.kind is RxUnitKind.FRAME and not unit.hardware_error
                       and unit.frame_end - unit.frame_start >= MIN_FRAME_SIZE]
    sent_frames = [packet.data[span[0]:span[1]] for packet, span in zip(tx_packets, tx_spans) if span is not None]
    seen_macs: set[bytes] = set()
    seen_ssids: set[bytes] = set()
    for frame in received_frames + sent_frames:
        frame_end = frame_end_without_fcs(frame)
        for address in frame_address_offsets(frame, frame_end):
            seen_macs.add(frame[address:address + 6])
        for start, end in ssid_element_spans(frame, frame_end):
            seen_ssids.add(frame[start:end])
    mac_replacements = {mac: fake_mac(key, mac) for mac in seen_macs
                        if is_unicast(mac) and any(mac) and mac not in adapter_macs}
    cleaner = FrameCleaner(mac_replacements, {ssid: fake_ssid(key, ssid) for ssid in seen_ssids})

    counts: Counter[str] = Counter()
    kept_destinations: list[bytes] = []
    for packet, units in zip(rx_packets, rx_units):
        if units is None:
            packet.replace_data(b"")
            counts["rx transfers unparsed (emptied)"] += 1
            continue
        kept_units = []
        for unit in units:
            frame = unit.data[unit.frame_start:unit.frame_end]
            if unit.kind is RxUnitKind.NOT_A_FRAME:
                kept_units.append(unit)
                counts["rx non-frame packets kept"] += 1
                continue
            if unit.kind is RxUnitKind.SPLIT_FRAME:
                counts["rx frames dropped: split across transfers"] += 1
                continue
            if unit.hardware_error:
                counts["rx frames dropped: hardware error"] += 1
                continue
            if len(frame) < MIN_FRAME_SIZE or (frame[0] >> 2) & 0x3 == 0x3:
                counts["rx frames dropped: other"] += 1
                continue
            destination = bytes(frame[4:10])
            if (frame[0] >> 2) & 0x3 == FRAME_TYPE_MANAGEMENT and frame[0] >> 4 == SUBTYPE_BEACON:
                counts["rx beacons kept"] += 1
            elif (is_unicast(destination) and destination not in adapter_macs
                  and destination not in kept_destinations
                  and len(kept_destinations) < MAX_KEPT_UNICAST_DESTINATIONS):
                kept_destinations.append(destination)
                counts["rx unicast frames kept"] += 1
            else:
                counts["rx frames dropped: other"] += 1
                continue
            cleaner.clean(frame)
            unit.data[unit.frame_start:unit.frame_end] = frame
            kept_units.append(unit)
        cleaned = layout.join_rx(kept_units)
        if cleaned != packet.data:
            packet.replace_data(cleaned)
            counts["rx transfers rewritten"] += 1
        if not cleaned:
            counts["rx transfers emptied"] += 1
    counts["rx transfers"] = len(rx_packets)

    for packet, span in zip(tx_packets, tx_spans):
        if span is None:
            counts["tx transfers without an 802.11 frame (untouched)"] += 1
            continue
        frame = bytearray(packet.data[span[0]:span[1]])
        cleaner.clean(frame)
        packet.replace_data(packet.data[:span[0]] + bytes(frame) + packet.data[span[1]:])
        counts["tx frames cleaned"] += 1

    for packet in packets:
        if packet.device != card and packet.transfer_type != TRANSFER_CONTROL and packet.data:
            packet.replace_data(b"")
            counts["other-device payloads emptied"] += 1

    for packet, block_index in zip(packets, packet_block_index):
        if not packet.changed:
            continue
        struct.pack_into("<II", packet.header, 32, len(packet.data), len(packet.data))
        record = bytes(packet.header) + packet.data
        options_start = EPB_FIXED_SIZE + (struct.unpack_from("<I", packet.block, 20)[0] + 3) // 4 * 4
        options = packet.block[options_start:-4]
        body = (packet.block[8:20] + struct.pack("<II", len(record), len(record))
                + record + bytes(-len(record) % 4) + options)
        block_length = 8 + len(body) + 4
        blocks[block_index] = (struct.pack("<II", PCAPNG_ENHANCED_PACKET_BLOCK, block_length)
                               + body + struct.pack("<I", block_length))

    print(f"card: device {card}, layout {type(layout).__name__}")
    print(f"adapter MAC: {len(adapter_macs)} value(s) from {layout.ADAPTER_MAC_SOURCE}; "
          f"{len(adapter_macs & seen_macs)} of them appear in 802.11 frames")
    print(f"MACs replaced: {len(mac_replacements)}, ESSIDs replaced: {len(seen_ssids)}")
    for name, count in sorted(counts.items()):
        print(f"  {name}: {count}")
    return b"".join(blocks)


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove the owner's airspace from a driver usbmon capture.")
    parser.add_argument("capture", type=Path, help="usbmon pcapng from scripts/capture.py")
    parser.add_argument("output", type=Path, help="where to write the cleaned pcapng")
    args = parser.parse_args()
    args.output.write_bytes(clean_pcapng(args.capture.read_bytes(), os.urandom(32)))


if __name__ == "__main__":
    main()
