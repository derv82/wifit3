"""M2c-2: the key-cache clear reads each entry's TYPE then zeroes its 8 words (TYPE=CLR),
across all 128 entries."""
import struct

from wifit3.chips.ar9271_v2 import hw, key, reg as R
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import WMI


class FakeDev:
    """Returns 0 for every read, so no entry looks TKIP-typed (no mic-entry clear)."""

    def __init__(self):
        self.cmds = []

    def write(self, ep, data, timeout=None):
        cmd_id, seq = struct.unpack_from(">HH", data, 8)
        self.cmds.append((cmd_id, data[12:]))
        self._resp = struct.pack(">BBH", 1, 0, 8) + b"\x00\x00\x00\x00" + \
            struct.pack(">HH", cmd_id, seq) + b"\x00\x00\x00\x00"
        return len(data)

    def read(self, ep, length, timeout=None):
        return bytearray(self._resp)


def test_keyreset_one_entry():
    dev = FakeDev()
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    key.keyreset(h, 0)
    READ, WRITE = 0x0014, 0x0015

    # Read AR_KEYTABLE_TYPE(0) = 0x8814.
    assert dev.cmds[0] == (READ, bytes.fromhex("00008814"))
    # One batched write: KEY0..KEY4=0, TYPE=CLR(7), MAC0=0, MAC1=0.
    batch = ("0000880000000000" "0000880400000000" "0000880800000000" "0000880c00000000"
             "0000881000000000" "0000881400000007" "0000881800000000" "0000881c00000000")
    assert dev.cmds[1] == (WRITE, bytes.fromhex(batch))
    assert len(dev.cmds) == 2


def test_init_crypto_clears_all_entries():
    dev = FakeDev()
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    key.init_crypto(h)
    reads = [c for c, _ in dev.cmds if c == 0x14]
    writes = [c for c, _ in dev.cmds if c == 0x15]
    assert len(reads) == R.AR_KEYTABLE_SIZE == 128
    assert len(writes) == 128
    # Last entry's TYPE read is AR_KEYTABLE(127)+20.
    last_read = [b for c, b in dev.cmds if c == 0x14][-1]
    assert struct.unpack(">I", last_read)[0] == R.AR_KEYTABLE(127) + 20
