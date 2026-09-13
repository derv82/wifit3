"""MAC address between its wire form (6 bytes) and its readable form (colon hex)."""
import os


def str_to_mac(mac) -> bytes:
    if isinstance(mac, (bytes, bytearray)):
        return bytes(mac)
    return bytes(int(octet, 16) for octet in mac.split(":"))


def mac_to_str(mac: bytes) -> str:
    if len(mac) != 6:
        return "00:00:00:00:00:00"
    return ":".join(f"{b:02x}" for b in mac)


def mac_header(fc: bytes, addr1: bytes, addr2: bytes, addr3: bytes, duration: bytes = b"\x00\x00") -> bytes:
    """Build a 24-byte 802.11 MAC header with a zero sequence-control field."""
    return fc + duration + addr1 + addr2 + addr3 + b"\x00\x00"


def header_len(fc0: int, fc1: int) -> int:
    """802.11 MAC header length in bytes from the two frame-control octets."""
    length = 24
    if (fc1 & 0x01) and (fc1 & 0x02):
        length += 6
    if ((fc0 & 0xF0) >> 4) & 0x08:
        length += 2
    if fc1 & 0x80:
        length += 4
    return length


def random_client_mac() -> bytes:
    """Random locally-administered unicast MAC (0x02 prefix + 5 random bytes)."""
    return bytes([0x02]) + os.urandom(5)
