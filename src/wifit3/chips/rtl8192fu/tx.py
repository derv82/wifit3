import struct


TX_DESC_SIZE = 40
TXDESC_QUEUE_BEACON = 0x10
TXDESC_QUEUE_MGNT = 0x12
TXDESC40_AGG_BREAK = 1 << 16
TXDESC40_USE_DRIVER_RATE = 1 << 8
TXDESC40_DATA_RATE_FB_SHIFT = 8
TXDESC40_RETRY_LIMIT_ENABLE = 1 << 17
TXDESC40_RETRY_LIMIT_SHIFT = 18
TXDESC40_SEQ_SHIFT = 12


def _validate_frame(frame: bytes) -> None:
    if len(frame) < 24:
        raise ValueError("802.11 TX frame is shorter than its header")
    if len(frame) > 0xFFFF - TX_DESC_SIZE:
        raise ValueError("802.11 TX frame is too large")
    if frame[0] & 0x0C == 0x04:
        raise ValueError("RTL8192FU control-frame TX is not ported")


def build_tx_packet(frame: bytes) -> bytes:
    _validate_frame(frame)
    descriptor = bytearray(TX_DESC_SIZE)
    descriptor[0:2] = len(frame).to_bytes(2, "little")
    descriptor[2] = TX_DESC_SIZE
    descriptor[3] = 1 if frame[4] & 0x01 else 0

    frame_type = frame[0] & 0x0C
    if frame_type == 0:
        queue = TXDESC_QUEUE_BEACON if frame[0] & 0xFC == 0x80 else TXDESC_QUEUE_MGNT
        txdw3 = TXDESC40_USE_DRIVER_RATE
        txdw4 = TXDESC40_RETRY_LIMIT_ENABLE | (6 << TXDESC40_RETRY_LIMIT_SHIFT)
    else:
        queue = 0
        txdw3 = 0
        txdw4 = 0x1F << TXDESC40_DATA_RATE_FB_SHIFT
    txdw1 = queue << 8
    txdw2 = TXDESC40_AGG_BREAK
    sequence_number = int.from_bytes(frame[22:24], "little") >> 4
    txdw9 = sequence_number << TXDESC40_SEQ_SHIFT
    struct.pack_into("<IIII", descriptor, 4, txdw1, txdw2, txdw3, txdw4)
    struct.pack_into("<I", descriptor, 36, txdw9)

    checksum = 0
    for offset in range(0, 32, 2):
        checksum ^= int.from_bytes(descriptor[offset:offset + 2], "little")
    descriptor[28:30] = ((~checksum) & 0xFFFF).to_bytes(2, "little")
    return bytes(descriptor) + frame


def stamp_sequence(frame: bytes, sequence_number: int) -> bytes:
    _validate_frame(frame)
    stamped = bytearray(frame)
    fragment_number = stamped[22] & 0x0F
    stamped[22:24] = ((sequence_number & 0xFFF) << 4 | fragment_number).to_bytes(2, "little")
    return bytes(stamped)
