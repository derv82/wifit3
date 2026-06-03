"""Hardware-free regression for driver-level behaviour (M4b inject_frame).

A fake transport records the bulk-OUT bytes so we can check that inject_frame builds
the M4a management descriptor, prepends it to the frame, derives the BMC bit from
addr1, and rejects frames too short to address. asyncio_mode=auto runs the async
tests without a decorator.
"""
from wifit3.chips.rtl8814au_dkms import tx
from wifit3.chips.rtl8814au_dkms.driver import Rtl8814auDkmsDriver


class _FakeTransport:
    def __init__(self):
        self.sent = []

    def bulk_out(self, data):
        self.sent.append(bytes(data))


def _field(desc, byte_off, bit_start, bit_len):
    word = int.from_bytes(desc[byte_off:byte_off + 4], "little")
    return (word >> bit_start) & ((1 << bit_len) - 1)


def _deauth(addr1: bytes) -> bytes:
    # FC=deauth | dur | addr1(DA) | addr2(SA) | addr3(BSSID) | seq | reason = 26 B
    return (b"\xc0\x00\x00\x00" + addr1 + b"\x00\x11\x22\x33\x44\x55"
            + b"\x00\x11\x22\x33\x44\x55" + b"\x00\x00" + b"\x07\x00")


async def test_inject_frame_builds_desc_and_sends_broadcast():
    drv = Rtl8814auDkmsDriver(_FakeTransport())
    frame = _deauth(b"\xff" * 6)
    assert await drv.inject_frame(frame) is True
    sent = drv.transport.sent[0]
    desc, payload = sent[:40], sent[40:]
    assert payload == frame                          # frame rides behind the 40 B desc
    assert _field(desc, 0, 0, 16) == len(frame)      # PKT_SIZE
    assert _field(desc, 0, 24, 1) == 1               # BMC (broadcast addr1)
    assert _field(desc, 4, 8, 5) == tx.QSLT_MGNT     # MGMT queue


async def test_inject_frame_unicast_clears_bmc():
    drv = Rtl8814auDkmsDriver(_FakeTransport())
    frame = _deauth(b"\x00\x11\x22\x33\x44\x55")     # unicast (even first octet)
    assert await drv.inject_frame(frame) is True
    desc = drv.transport.sent[0][:40]
    assert _field(desc, 0, 24, 1) == 0               # BMC clear


async def test_inject_frame_rejects_too_short():
    drv = Rtl8814auDkmsDriver(_FakeTransport())
    assert await drv.inject_frame(b"\xc0\x00\x00\x00") is False
    assert drv.transport.sent == []                  # nothing transmitted
