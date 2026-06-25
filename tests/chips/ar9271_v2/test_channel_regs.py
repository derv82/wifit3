"""M2d-4: override_ini (RX block + PCU mode2) and set_channel_regs (20 MHz PHY config)."""
import struct

from wifit3.chips.ar9271_v2 import chan as chanmod, hw, phy, reg as R
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import WMI


class FakeDev:
    def __init__(self, read_val=0):
        self.cmds = []
        self._rv = read_val

    def write(self, ep, data, timeout=None):
        cmd_id, seq = struct.unpack_from(">HH", data, 8)
        self.cmds.append((cmd_id, data[12:]))
        val = self._rv if cmd_id == 0x14 else 0
        self._resp = struct.pack(">BBH", 1, 0, 8) + b"\x00\x00\x00\x00" + \
            struct.pack(">HH", cmd_id, seq) + struct.pack(">I", val)
        return len(data)

    def read(self, ep, length, timeout=None):
        return bytearray(self._resp)


def _pairs(dev):
    out = []
    for c, b in dev.cmds:
        if c == 0x15:
            out += [struct.unpack_from(">II", b, k) for k in range(0, len(b) - 4, 8)]
    return out


def test_override_ini():
    dev = FakeDev(read_val=0x00500003)        # PCU_MISC_MODE2 has KEYID bit + others
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    phy.override_ini(h, chanmod.channel_2ghz(1))
    rmw = [struct.unpack_from(">III", b, 0) for c, b in dev.cmds if c == 0x20]
    assert (R.AR_DIAG_SW, R.AR_DIAG_RX_DIS | R.AR_DIAG_RX_ABORT, 0) in rmw
    # write = (read & ~KEYID_ENABLE) | CFP_IGNORE  (HWWAR1 NOT cleared on 9271).
    expect = (0x00500003 & ~R.AR_ADHOC_MCAST_KEYID_ENABLE) | R.AR_PCU_MISC_MODE2_CFP_IGNORE
    assert (R.AR_PCU_MISC_MODE2, expect) in _pairs(dev)


def test_set_channel_regs_20mhz():
    dev = FakeDev(read_val=0)                 # DAC-FIFO bit clear
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    phy.set_channel_regs(h, chanmod.channel_2ghz(1))
    pairs = _pairs(dev)
    phymode = (R.AR_PHY_FC_HT_EN | R.AR_PHY_FC_SHORT_GI_40
               | R.AR_PHY_FC_SINGLE_HT_LTF1 | R.AR_PHY_FC_WALSH)
    assert phymode == 0x3C0
    assert (R.AR_PHY_TURBO, 0x3C0) in pairs
    assert (R.AR_2040_MODE, 0) in pairs
    assert (R.AR_GTXTO, 25 << 16) in pairs
    assert (R.AR_CST, 0xF << 16) in pairs
