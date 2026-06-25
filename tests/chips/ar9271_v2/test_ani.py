"""M2c-1: ani_init emits the PHY-error/MIB register sequence (two batched flushes around two
5-counter MIB multi-reads)."""
import struct

from wifit3.chips.ar9271_v2 import ani, hw, reg as R
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import WMI


class FakeDev:
    def __init__(self):
        self.cmds = []

    def write(self, ep, data, timeout=None):
        cmd_id, seq = struct.unpack_from(">HH", data, 8)
        body = data[12:]
        self.cmds.append((cmd_id, body))
        nwords = max(1, len(body) // 4) if cmd_id == 0x14 else 1
        val = b"\x00\x00\x00\x00" * nwords
        self._resp = struct.pack(">BBH", 1, 0, 4 + len(val)) + b"\x00\x00\x00\x00" + \
            struct.pack(">HH", cmd_id, seq) + val
        return len(data)

    def read(self, ep, length, timeout=None):
        return bytearray(self._resp)


def test_ani_init_sequence():
    dev = FakeDev()
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    ani.ani_init(h)
    READ, WRITE = 0x0014, 0x0015

    mib = "".join("%08x" % a for a in
                  (R.AR_RTS_OK, R.AR_RTS_FAIL, R.AR_ACK_FAIL, R.AR_FCS_FAIL, R.AR_BEACON_CNT))
    ani_flush = "0000812c00000000" "0000813400000000" "0000813000020000" "0000813802000000"
    mib_flush = ("0000812400000000" "0000812800000000" "0000004000000000"
                 "0000813000020000" "0000813802000000")

    got = [(c, b.hex()) for c, b in dev.cmds]
    assert got == [
        (WRITE, ani_flush),     # ani_restart: PHY_ERR_1/2 + masks
        (READ, mib),            # ani_restart -> update_mibstats
        (READ, mib),            # enable_mib_counters -> update_mibstats
        (WRITE, mib_flush),     # enable_mib_counters: FILT_OFDM/CCK + MIBC=0 + masks
    ]
