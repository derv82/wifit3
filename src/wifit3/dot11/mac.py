"""MAC address between its wire form (6 bytes) and its readable form (colon hex)."""


def str_to_mac(mac) -> bytes:
    if isinstance(mac, (bytes, bytearray)):
        return bytes(mac)
    return bytes(int(octet, 16) for octet in mac.split(":"))


def mac_to_str(mac: bytes) -> str:
    if len(mac) != 6:
        return "00:00:00:00:00:00"
    return ":".join(f"{b:02x}" for b in mac)
