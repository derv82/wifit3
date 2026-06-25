"""M2e-3: reset_opmode — STA id/defaults, BSSID mask, antenna, associd, operating mode."""
import struct

from wifit3.chips.ar9271_v2 import hw, reg as R
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import WMI

MAC = bytes.fromhex("c01c304f78b0")        # c0:1c:30:4f:78:b0


class FakeDev:
    """Returns the post-op415 STA_ID1 value (0x88800000) for the setbssidmask read."""
    def __init__(self):
        self.cmds = []

    def write(self, ep, data, timeout=None):
        cmd_id, seq = struct.unpack_from(">HH", data, 8)
        self.cmds.append((cmd_id, data[12:]))
        val = 0x88800000 if cmd_id == 0x14 else 0
        self._resp = struct.pack(">BBH", 1, 0, 8) + b"\x00\x00\x00\x00" + \
            struct.pack(">HH", cmd_id, seq) + struct.pack(">I", val)
        return len(data)

    def read(self, ep, length, timeout=None):
        return bytearray(self._resp)


def _run():
    dev = FakeDev()
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    h.macaddr = bytearray(MAC)
    h.reset_opmode(0, 1)                    # macStaId1=0, saveDefAntenna=1
    return dev


def _writes(dev):
    out = {}
    for c, b in dev.cmds:
        if c == 0x15:
            for k in range(0, len(b) - 4, 8):
                reg, val = struct.unpack_from(">II", b, k)
                out[reg] = val
    return out


def _rmws(dev):
    out = []
    for c, b in dev.cmds:
        if c == 0x20:
            out += [struct.unpack_from(">III", b, k) for k in range(0, len(b) - 4, 12)]
    return out


def test_sta_id1_rmw():
    rmw = _rmws(_run())
    # set = RTS_USE_DEF | CRPT_MIC | MCAST_KSRCH = 0x88800000; clr = ~SADH_MASK.
    assert (R.AR_STA_ID1, 0x88800000, 0xFFFF0000) in rmw


def test_macaddr_and_mask_writes():
    w = _writes(_run())
    assert w[R.AR_STA_ID0] == 0x4F301CC0
    assert w[R.AR_STA_ID1] == 0x8880B078       # (read & ~SADH) | le16(mac[4:6])
    assert w[R.AR_BSSMSKL] == 0xFFFFFFFF and w[R.AR_BSSMSKU] == 0xFFFF
    assert w[R.AR_DEF_ANTENNA] == 1
    assert w[R.AR_BSS_ID0] == 0 and w[R.AR_BSS_ID1] == 0
    assert w[R.AR_RSSI_THR] == R.INIT_RSSI_THR


def test_operating_mode_station():
    rmw = _rmws(_run())
    assert (R.AR_CFG, 0, R.AR_CFG_AP_ADHOC_INDICATION) in rmw
    assert (R.AR_STA_ID1, R.AR_STA_ID1_KSRCH_MODE,
            R.AR_STA_ID1_STA_AP | R.AR_STA_ID1_ADHOC) in rmw
