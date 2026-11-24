"""SSID text for the terminal UI."""
from wifit3.dot11.ie import ssid_text


def displayable_ssid(ssid_bytes: bytes) -> str:
    """The SSID as the UI shows it: UTF-8; else GBK when every byte >= 0x80 is in a GB2312 pair (A1-FE A1-FE);
    else cp1252. Non-printable characters and bytes the charset has no character for as \\xNN."""
    try:
        ssid_bytes.decode("utf-8")
        return ssid_text(ssid_bytes)
    except UnicodeDecodeError:
        pass
    gb2312_pairs = True
    index = 0
    while index < len(ssid_bytes):
        if ssid_bytes[index] < 0x80:
            index += 1
        elif (index + 1 < len(ssid_bytes) and 0xA1 <= ssid_bytes[index] <= 0xFE
              and 0xA1 <= ssid_bytes[index + 1] <= 0xFE):
            index += 2
        else:
            gb2312_pairs = False
            break
    if gb2312_pairs:
        text = ssid_bytes.decode("gbk", errors="surrogateescape")
    else:
        text = ssid_bytes.decode("cp1252", errors="surrogateescape")
    characters = []
    for character in text:
        if 0xDC80 <= ord(character) <= 0xDCFF:          # surrogateescape: a byte with no character
            characters.append(f"\\x{ord(character) - 0xDC00:02x}")
        elif character.isprintable():
            characters.append(character)
        else:
            characters.append(f"\\x{ord(character):02x}")
    return "".join(characters)
