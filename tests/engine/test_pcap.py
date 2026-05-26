"""libpcap writer — frames are saved with a clean (FCS-less) tail so readers
that treat LINKTYPE_IEEE802_11 as FCS-less don't flag a malformed trailing IE."""

import zlib

from wifit3.engine.pcap import write_pcap


def _first_packet(path) -> bytes:
    """Extract the first packet's payload from a libpcap file (24-byte global
    header + 16-byte record header, all little-endian)."""
    raw = path.read_bytes()
    caplen = int.from_bytes(raw[24 + 8: 24 + 12], "little")
    return raw[40: 40 + caplen]


def _beacon_body() -> bytes:
    # Minimal beacon-ish MPDU: 24-byte hdr + 12 fixed params + SSID IE.
    return (
        b"\x80\x00" + b"\x00" * 22          # FC + dur + addr1/2/3 + seq (24 B)
        + b"\x00" * 12                       # timestamp/interval/caps
        + b"\x00\x04test"                    # SSID IE: tag 0, len 4
    )


def test_write_pcap_strips_valid_fcs(tmp_path):
    body = _beacon_body()
    fcs = (zlib.crc32(body) & 0xFFFFFFFF).to_bytes(4, "little")
    path = tmp_path / "a.pcap"
    assert write_pcap(path, [body + fcs]) == 1
    # The saved packet is the body WITHOUT the FCS.
    assert _first_packet(path) == body


def test_write_pcap_leaves_frame_without_fcs(tmp_path):
    body = _beacon_body() + b"\xde\xad\xbe\xef"   # tail is NOT a valid CRC32
    path = tmp_path / "b.pcap"
    write_pcap(path, [body])
    assert _first_packet(path) == body            # untouched


def test_write_pcap_strips_only_real_fcs_not_double(tmp_path):
    """A frame already FCS-less (its tail isn't a CRC) keeps all its bytes —
    no spurious second strip."""
    body = _beacon_body()
    # body's own last 4 bytes ("test") are not a CRC of the rest → no strip.
    path = tmp_path / "c.pcap"
    write_pcap(path, [body])
    assert _first_packet(path) == body
