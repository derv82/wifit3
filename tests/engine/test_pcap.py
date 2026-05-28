"""libpcap writer — saves LINKTYPE_IEEE802_11 frames verbatim. Callers deliver
FCS-less MPDU bodies (every chip driver strips at RX ingress), so the writer
no longer second-guesses the frame tail."""

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


def test_write_pcap_writes_frame_verbatim(tmp_path):
    body = _beacon_body()
    path = tmp_path / "a.pcap"
    assert write_pcap(path, [body]) == 1
    assert _first_packet(path) == body
