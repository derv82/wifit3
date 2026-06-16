"""Unit tests for the RTL8822BU TX-descriptor build (monitor injection).

No pcap to diff against (the capture's only TX is aireplay-ng's), so these assert the HALMAC field
offsets + the XOR-16 checksum invariant directly — the strongest available offline guarantee.
"""
from wifit3.chips.rtl8822bu_dkms.constants import DESC_RATE6M, RATEID_IDX_B
from wifit3.chips.rtl8822bu_dkms.tx import build_inject_txdesc

# A 26-byte deauth (FC=c0:00, dur, addr1=broadcast, addr2, addr3, seq) — addr1 multicast bit set.
_DEAUTH_BCAST = bytes.fromhex(
    "c0000000" "ffffffffffff" "001122334455" "001122334455" "0000")
# Same but a unicast addr1 (low bit of first octet clear).
_DEAUTH_UNICAST = bytes.fromhex(
    "c0000000" "020000000001" "001122334455" "001122334455" "0000")


def test_size_and_frame_appended_verbatim():
    out = build_inject_txdesc(_DEAUTH_UNICAST)
    assert len(out) == 48 + len(_DEAUTH_UNICAST)
    assert out[48:] == _DEAUTH_UNICAST          # frame appended unmodified


def test_word0_fields():
    w0 = int.from_bytes(build_inject_txdesc(_DEAUTH_UNICAST)[0:4], "little")
    assert w0 & 0xFFFF == len(_DEAUTH_UNICAST)   # TXPKTSIZE
    assert (w0 >> 16) & 0xFF == 48               # OFFSET == desc size
    assert (w0 >> 26) & 1 == 1                   # LS
    assert (w0 >> 31) & 1 == 1                   # DISQSELSEQ


def test_word1_qsel_macid_rateid():
    w1 = int.from_bytes(build_inject_txdesc(_DEAUTH_UNICAST, rate_id=RATEID_IDX_B)[4:8], "little")
    assert w1 & 0x7F == 1                         # MACID == RTW_DEFAULT_MGMT_MACID (bcast mgmt)
    assert (w1 >> 8) & 0x1F == 0x12               # QSEL == QSLT_MGNT
    assert (w1 >> 16) & 0x1F == 8                 # RATE_ID == B


def test_matches_captured_aireplay_descriptor():
    # The exact 48-byte descriptor the capture's aireplay-ng injector emitted for a 42-byte broadcast
    # mgmt frame (FC=0x40) — PORTING.md step-4 byte-diff. Only the frame's own seqctl varies per send.
    frame = bytes([0x40, 0x00, 0x00, 0x00]) + b"\xff" * 6 + bytes(42 - 10)
    expected = bytes.fromhex(
        "2a003085011208000000003f0001000000003200000000000100000020a9000000800000"
        "000000000000000000000000")
    assert build_inject_txdesc(frame)[:48] == expected


def test_use_rate_and_datarate():
    d = build_inject_txdesc(_DEAUTH_UNICAST, hw_rate=DESC_RATE6M)
    assert (int.from_bytes(d[0x0C:0x10], "little") >> 8) & 1 == 1    # USE_RATE
    assert int.from_bytes(d[0x10:0x14], "little") & 0x7F == 0x04     # DATARATE 6M


def test_en_hwseq():
    d = build_inject_txdesc(_DEAUTH_UNICAST)
    assert (int.from_bytes(d[0x20:0x24], "little") >> 15) & 1 == 1   # EN_HWSEQ


def test_checksum_total_xor_is_zero():
    # The checksum field is inside the first 32 bytes (word7), so a correct XOR-16 makes the XOR of
    # all 16 leading words == 0 — exactly how the USB HW validates the descriptor.
    d = build_inject_txdesc(_DEAUTH_UNICAST)[:48]
    acc = 0
    for i in range(16):
        acc ^= int.from_bytes(d[2 * i:2 * i + 2], "little")
    assert acc == 0


def test_bmc_bit_tracks_addr1_group_bit():
    assert (int.from_bytes(build_inject_txdesc(_DEAUTH_BCAST)[0:4], "little") >> 24) & 1 == 1
    assert (int.from_bytes(build_inject_txdesc(_DEAUTH_UNICAST)[0:4], "little") >> 24) & 1 == 0
