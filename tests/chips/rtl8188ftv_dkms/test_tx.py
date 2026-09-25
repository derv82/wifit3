"""rtl8188ftv_dkms M8: TX descriptor builder + bulk-OUT send.

Vectors below are inline recorded descriptor/payload bytes (kept in-file
so the suite runs anywhere with no capture files); the builder must
reproduce them byte-exact from decoded fields.
"""


class FakeT:
    def __init__(self, reads=()):
        self._reads = list(reads)
        self.writes = []
        self.bulk = []

    def read32(self, addr):
        return self._reads.pop(0)

    def write32(self, addr, value):
        self.writes.append((addr, value & 0xFFFFFFFF))

    def bulk_out(self, data):
        self.bulk.append(bytes(data))


PROBE_DESC = bytes.fromhex(
    "4000280101120800000000000001000000001a00"
    "00000000000000007b1200000080000000000000")
AUTH_DESC = bytes.fromhex(
    "1e00280001120800000000000001000000001a00"
    "0000000000000000251300000080000000c00200")
DEAUTH_DESC = bytes.fromhex(
    "1a00280001120800000008000001000000003200"
    "0000000000000000011300000080000000f00200")
DEAUTH_FRAME = bytes.fromhex(
    "c00000001c634979c10444efbf1f9dfb"
    "1c634979c104f0020300")
DATA_DESC = bytes.fromhex(
    "6c002800000006000000010000000000001f0000"
    "0000000000000000431e00010080000000100000")
SP_DATA_DESC = bytes.fromhex(
    "6801280000000600000001000001000000000000"
    "1000000000000000570100010080000000400000")
CARD = bytes.fromhex("44efbf1f9dfb")
AP = bytes.fromhex("1c634979c104")


def test_checksum_vectors():
    from wifit3.chips.rtl8188ftv_dkms import tx
    for desc in (PROBE_DESC, AUTH_DESC, DATA_DESC, SP_DATA_DESC,
                 DEAUTH_DESC):
        assert len(desc) == 40
        blanked = desc[:28] + b"\x00\x00" + desc[30:]
        import struct
        assert tx.txdesc_checksum(blanked) == \
            struct.unpack("<H", desc[28:30])[0]


def test_build_probe_matches_recorded():
    from wifit3.chips.rtl8188ftv_dkms import tx
    assert tx.build_mgnt_desc(size=64, seq=0, bmc=True,
                              retry_limit=6) == PROBE_DESC


def test_build_auth_matches_recorded():
    from wifit3.chips.rtl8188ftv_dkms import tx
    assert tx.build_mgnt_desc(size=30, seq=44, bmc=False,
                              retry_limit=6) == AUTH_DESC


def test_build_deauth_matches_recorded():
    from wifit3.chips.rtl8188ftv_dkms import tx
    assert tx.build_mgnt_desc(size=26, seq=47, bmc=False,
                              retry_limit=12,
                              spe_rpt=True) == DEAUTH_DESC
    assert tx.build_deauth_frame(AP, CARD, AP, 47,
                                 reason=3) == DEAUTH_FRAME


def test_build_data_vectors_match_recorded():
    from wifit3.chips.rtl8188ftv_dkms import tx
    assert tx.build_tx_desc(size=108, seq=1, macid=0, qsel=0, rateid=6,
                            use_rate=False, tx_rate=0, retry_en=False,
                            retry_limit=0, bmc=False, agg_num=1,
                            agg_break=True, fb_limit=0x1F) == DATA_DESC
    assert tx.build_tx_desc(size=360, seq=4, macid=0, qsel=0, rateid=6,
                            use_rate=True, tx_rate=0, retry_en=False,
                            retry_limit=0, bmc=False, agg_num=1,
                            agg_break=True, data_short=True,
                            fb_limit=0) == SP_DATA_DESC


def test_zero_pad_rule():
    from wifit3.chips.rtl8188ftv_dkms import tx
    assert tx.needs_zero_pad(472) is True
    assert tx.needs_zero_pad(64) is False
    desc = tx.build_mgnt_desc(size=472, seq=0, bmc=False)
    frame = b"\xc0" + bytes(471)
    urb = tx.build_tx_urb(frame, desc)
    assert len(urb) == 520
    assert urb[:8] == bytes(8)
    assert urb[8:48] == desc
    assert urb[48:] == frame
    urb2 = tx.build_tx_urb(b"\xc0" + bytes(63), desc)
    assert urb2 == desc + b"\xc0" + bytes(63)


def test_seq_helpers_round_trip():
    from wifit3.chips.rtl8188ftv_dkms import tx
    frame = tx.build_deauth_frame(AP, CARD, AP, 0)
    assert tx.frame_seqnum(frame) == 0
    stamped = tx.stamp_seqnum(frame, 47)
    assert tx.frame_seqnum(stamped) == 47
    assert stamped[:22] == frame[:22] and stamped[24:] == frame[24:]


def test_inject_frame_sends_urb_and_advances_seq():
    from wifit3.chips.rtl8188ftv_dkms import tx
    t = FakeT()
    st = {"mgnt_seq": 0}
    frame = tx.stamp_seqnum(tx.build_deauth_frame(AP, CARD, AP, 0), 5)
    assert tx.inject_frame(t, st, frame) is True
    assert st["mgnt_seq"] == 6
    assert len(t.bulk) == 1
    desc, sent = t.bulk[0][:40], t.bulk[0][40:]
    assert sent == frame
    assert desc == tx.build_mgnt_desc(size=len(frame), seq=5, bmc=False)


def test_inject_frame_sends_qos_data_with_monitor_template():
    from wifit3.chips.rtl8188ftv_dkms import tx
    t = FakeT()
    st = {"mgnt_seq": 7}
    qos = (bytes((0x88, 0x01, 0x00, 0x00)) + AP + CARD + AP
           + bytes((0x70, 0x00, 0x00, 0x00)) + bytes(36))
    assert tx.inject_frame(t, st, qos) is True
    assert st["mgnt_seq"] == 8
    desc, sent = t.bulk[0][:40], t.bulk[0][40:]
    assert sent == qos
    assert desc == tx.build_mgnt_desc(size=len(qos), seq=7, bmc=False)


def test_inject_frame_rejects_control_and_short():
    from wifit3.chips.rtl8188ftv_dkms import tx
    t = FakeT()
    try:
        tx.inject_frame(t, {}, b"\xD4" + bytes(30))
    except ValueError:
        pass
    else:
        raise AssertionError("CTRL accepted")
    try:
        tx.inject_frame(t, {}, b"\xc0" * 10)
    except ValueError:
        pass
    else:
        raise AssertionError("short frame accepted")
    assert t.bulk == [] and t.writes == []
