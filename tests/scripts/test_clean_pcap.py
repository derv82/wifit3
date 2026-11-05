import struct
import zlib

from wifit3.scripts.clean_pcap import (
    AthHtcLayout,
    FrameCleaner,
    RealtekRxdesc24Layout,
    Rtl8187Layout,
    RxUnitKind,
    UsbPacket,
    clean_pcapng,
    fake_mac,
    fake_ssid,
)

KEY = bytes(range(32))
BROADCAST = b"\xff" * 6
ADAPTER = bytes.fromhex("aabbccddeeff")
BSSID = bytes.fromhex("aabbccdd0001")
CLIENT = bytes.fromhex("aabbccdd0002")
SSID = b"TestNetwork"


def with_fcs(frame: bytes) -> bytes:
    return frame + struct.pack("<I", zlib.crc32(frame))


def has_valid_fcs(frame: bytes) -> bool:
    return zlib.crc32(frame[:-4]) == struct.unpack("<I", frame[-4:])[0]


def beacon(bssid: bytes, ssid: bytes, extra_elements: bytes = b"") -> bytes:
    header = b"\x80\x00\x00\x00" + BROADCAST + bssid + bssid + b"\x00\x00"
    fixed = bytes(8) + b"\x64\x00\x01\x04"
    return with_fcs(header + fixed + bytes([0, len(ssid)]) + ssid + b"\x01\x01\x82" + extra_elements)


def data_frame(destination: bytes, source: bytes) -> bytes:
    return with_fcs(b"\x88\x02\x00\x00" + destination + source + source + b"\x00\x00" + b"\x00\x00" + b"secret body")


def probe_request(source: bytes, ssid: bytes) -> bytes:
    return b"\x40\x00\x00\x00" + BROADCAST + source + BROADCAST + b"\x00\x00" + bytes([0, len(ssid)]) + ssid


def test_fake_mac_is_locally_administered_unicast_and_keyed():
    fake = fake_mac(KEY, BSSID)
    assert fake[0] & 0x02 and not fake[0] & 0x01
    assert fake == fake_mac(KEY, BSSID)
    assert fake != fake_mac(bytes(32), BSSID)


def test_fake_ssid_keeps_length_and_is_keyed():
    fake = fake_ssid(KEY, SSID)
    assert len(fake) == len(SSID) and fake != SSID
    assert fake == fake_ssid(KEY, SSID)


def test_cleaner_rewrites_beacon_addresses_ssid_and_fcs():
    frame = bytearray(beacon(BSSID, SSID, b"\xdd\x0a\x00\x00\x00\x01" + BSSID))
    original = bytes(frame)
    cleaner = FrameCleaner({BSSID: fake_mac(KEY, BSSID)}, {SSID: fake_ssid(KEY, SSID)})
    cleaner.clean(frame)
    assert len(frame) == len(original)
    assert frame[4:10] == BROADCAST
    assert frame[10:16] == frame[16:22] == fake_mac(KEY, BSSID)
    assert frame[38:38 + len(SSID)] == fake_ssid(KEY, SSID)
    assert SSID not in frame and BSSID not in frame
    assert has_valid_fcs(bytes(frame))


def test_cleaner_replaces_ssid_found_elsewhere_in_the_body():
    frame = bytearray(beacon(BSSID, SSID, b"\xdd" + bytes([4 + len(SSID)]) + b"\x00\x50\xf2\x04" + SSID))
    FrameCleaner({}, {SSID: fake_ssid(KEY, SSID)}).clean(frame)
    assert SSID not in frame
    assert frame.count(fake_ssid(KEY, SSID)) == 2


def test_cleaner_leaves_hidden_ssid_and_unknown_addresses():
    frame = bytearray(beacon(BSSID, bytes(len(SSID))))
    original = bytes(frame)
    FrameCleaner({CLIENT: fake_mac(KEY, CLIENT)}, {SSID: fake_ssid(KEY, SSID)}).clean(frame)
    assert bytes(frame) == original


def test_cleaner_zeroes_data_body_past_qos_header():
    frame = bytearray(data_frame(CLIENT, BSSID))
    FrameCleaner({CLIENT: fake_mac(KEY, CLIENT), BSSID: fake_mac(KEY, BSSID)}, {}).clean(frame)
    assert frame[4:10] == fake_mac(KEY, CLIENT)
    assert frame[26:-4] == bytes(len(frame) - 30)
    assert has_valid_fcs(bytes(frame))


def rxdesc24_unit(frame: bytes, drvinfo_units: int = 0, crc_error: bool = False, c2h: bool = False) -> bytes:
    dw0 = len(frame) | (drvinfo_units << 16) | ((1 << 14) if crc_error else 0)
    dw2 = (1 << 28) if c2h else 0
    return struct.pack("<6I", dw0, 0, dw2, 0, 0, 0) + bytes(drvinfo_units * 8) + frame


def rxdesc24_aggregate(units: list[bytes]) -> bytes:
    buffer = b""
    for unit in units:
        buffer += bytes(-len(buffer) % 8) + unit
    return buffer


def test_realtek_split_and_join_keep_8_byte_alignment():
    first, second = beacon(BSSID, SSID), data_frame(CLIENT, BSSID)
    buffer = rxdesc24_aggregate([rxdesc24_unit(first, drvinfo_units=4), rxdesc24_unit(b"\x09\x00", c2h=True),
                                 rxdesc24_unit(second, crc_error=True)])
    layout = RealtekRxdesc24Layout()
    units = layout.split_rx(buffer)
    assert [unit.kind for unit in units] == [RxUnitKind.FRAME, RxUnitKind.NOT_A_FRAME, RxUnitKind.FRAME]
    assert bytes(units[0].data[units[0].frame_start:units[0].frame_end]) == first
    assert units[2].hardware_error and not units[0].hardware_error
    assert layout.join_rx(units) == buffer
    assert layout.split_rx(layout.join_rx([units[0], units[2]]))[1].data == units[2].data


def ath_htc_unit(frame: bytes) -> bytes:
    rx_status = bytearray(40)
    struct.pack_into(">H", rx_status, 8, len(frame))
    packet = bytes(8) + bytes(rx_status) + frame
    return struct.pack("<HH", len(packet), 0x4E00) + packet + bytes(-len(packet) % 4)


def test_ath_htc_marks_a_frame_split_across_two_transfers():
    whole, split = ath_htc_unit(beacon(BSSID, SSID)), ath_htc_unit(data_frame(CLIENT, BSSID))
    layout = AthHtcLayout()
    first = layout.split_rx(whole + split[:30])
    second = layout.split_rx(split[30:] + whole)
    assert [unit.kind for unit in first] == [RxUnitKind.FRAME, RxUnitKind.SPLIT_FRAME]
    assert [unit.kind for unit in second] == [RxUnitKind.SPLIT_FRAME, RxUnitKind.FRAME]
    assert layout.join_rx([second[1]]) == whole


def control_packets(urb_id: int, request_type: int, request: int, value: int, data: bytes) -> list[UsbPacket]:
    setup = struct.pack("<BBHHH", request_type, request, value, 0, len(data))
    submit = bytearray(64)
    struct.pack_into("<Q", submit, 0, urb_id)
    submit[8:12] = bytes([0x53, 2, 0x00, 5])
    submit[40:48] = setup
    complete = bytearray(submit)
    complete[8], complete[10] = 0x43, 0x80
    if request_type & 0x80:
        return [UsbPacket(b"", submit, b""), UsbPacket(b"", complete, data)]
    return [UsbPacket(b"", submit, data), UsbPacket(b"", complete, b"")]


def test_rtl8187_adapter_mac_from_eeprom_bit_banging():
    """Mirrors eeprom_93cx6_read on EEPROM_CMD 0xFF50: CS=bit3, CK=bit2, DI=bit1, DO=bit0."""
    eeprom_words = {7: int.from_bytes(ADAPTER[0:2], "little"), 8: int.from_bytes(ADAPTER[2:4], "little"),
                    9: int.from_bytes(ADAPTER[4:6], "little")}
    packets: list[UsbPacket] = []

    def write(register: int) -> None:
        packets.extend(control_packets(len(packets), 0x40, 0x05, 0xFF50, bytes([0x80 | register])))

    def read(register: int) -> None:
        packets.extend(control_packets(len(packets), 0xC0, 0x05, 0xFF50, bytes([register])))

    for word, value in eeprom_words.items():
        read(0)
        write(0x08)
        write(0x0C)
        write(0x08)
        read(0x08)
        for bit in [1, 1, 0] + [(word >> shift) & 1 for shift in range(5, -1, -1)]:
            write(0x08 | (bit << 1))
            write(0x0C | (bit << 1))
            write(0x08 | (bit << 1))
        write(0x08)
        read(0x08)
        for shift in range(15, -1, -1):
            write(0x0C)
            read(0x0C | ((value >> shift) & 1))
            write(0x08)
        read(0x08)
        write(0x00)
        write(0x04)
        write(0x00)
    assert Rtl8187Layout().adapter_macs(packets) == {ADAPTER}


def usbmon_record(urb_id: int, urb_type: int, transfer_type: int, endpoint: int, device: int,
                  setup: bytes = bytes(8), data: bytes = b"") -> bytes:
    header = bytearray(64)
    struct.pack_into("<Q", header, 0, urb_id)
    header[8:12] = bytes([urb_type, transfer_type, endpoint, device])
    struct.pack_into("<II", header, 32, len(data), len(data))
    header[40:48] = setup
    return bytes(header) + data


def pcapng(records: list[bytes]) -> bytes:
    section_header = struct.pack("<IIIHHqI", 0x0A0D0D0A, 28, 0x1A2B3C4D, 1, 0, -1, 28)
    interface = struct.pack("<IIHHII", 1, 20, 220, 0, 0, 20)
    blocks = [section_header, interface]
    for record in records:
        body = struct.pack("<IIIII", 0, 0, 0, len(record), len(record)) + record + bytes(-len(record) % 4)
        blocks.append(struct.pack("<II", 6, 12 + len(body)) + body + struct.pack("<I", 12 + len(body)))
    return b"".join(blocks)


def records_of(capture: bytes) -> list[bytes]:
    records = []
    offset = 0
    while offset < len(capture):
        block_type, length = struct.unpack_from("<II", capture, offset)
        if block_type == 6:
            captured = struct.unpack_from("<I", capture, offset + 20)[0]
            records.append(capture[offset + 28:offset + 28 + captured])
        offset += length
    return records


def test_clean_pcapng_end_to_end_on_a_realtek_capture():
    card, other_device = 5, 2
    records = []
    for position, byte in enumerate(ADAPTER):
        setup = struct.pack("<BBHHH", 0x40, 0x05, 0x0610 + position, 0, 1)
        records.append(usbmon_record(position, 0x53, 2, 0x00, card, setup, bytes([byte])))
        records.append(usbmon_record(position, 0x43, 2, 0x00, card, setup))
    destinations = [bytes.fromhex(f"aabbccdd10{index:02x}") for index in range(7)]
    rx_frames = [beacon(BSSID, SSID), data_frame(ADAPTER, BSSID), with_fcs(probe_request(CLIENT, SSID))]
    rx_frames += [data_frame(destination, BSSID) for destination in destinations] + [data_frame(destinations[0], CLIENT)]
    rx_buffer = rxdesc24_aggregate([rxdesc24_unit(frame) for frame in rx_frames])
    records.append(usbmon_record(100, 0x43, 3, 0x84, card, data=rx_buffer))
    tx_frame = probe_request(ADAPTER, SSID)
    tx_buffer = struct.pack("<II", len(tx_frame) | (40 << 16), 0x12 << 8) + bytes(32) + tx_frame
    records.append(usbmon_record(101, 0x53, 3, 0x05, card, data=tx_buffer))
    records.append(usbmon_record(102, 0x53, 3, 0x02, other_device, data=b"file contents"))

    cleaned = records_of(clean_pcapng(pcapng(records), KEY))

    assert len(cleaned) == len(records)
    assert cleaned[:12] == records[:12]
    kept = RealtekRxdesc24Layout().split_rx(cleaned[12][64:])
    kept_frames = [bytes(unit.data[unit.frame_start:unit.frame_end]) for unit in kept]
    assert [frame[4:10] for frame in kept_frames] == [BROADCAST] + [fake_mac(KEY, d) for d in destinations[:5]]
    assert all(has_valid_fcs(frame) for frame in kept_frames)
    assert all(frame[26:-4] == bytes(len(frame) - 30) for frame in kept_frames[1:])
    assert SSID not in cleaned[12] and BSSID not in cleaned[12] and ADAPTER not in cleaned[12]
    cleaned_tx_frame = cleaned[13][64 + 40:]
    assert cleaned_tx_frame[10:16] == ADAPTER
    assert cleaned_tx_frame[26:26 + len(SSID)] == fake_ssid(KEY, SSID)
    assert cleaned[14][64:] == b""
