"""M2e-4: rf_set_freq — single-chip 2.4 GHz synthesizer (CHANSEL_2G + AR_PHY_SYNTH_CONTROL)."""
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


def _writes(dev):
    out = {}
    for c, b in dev.cmds:
        if c == 0x15:
            for k in range(0, len(b) - 4, 8):
                reg, val = struct.unpack_from(">II", b, k)
                out[reg] = val
    return out


def test_synth_control_channel1():
    dev = FakeDev(read_val=0)
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    phy.rf_set_freq(h, chanmod.channel_2ghz(1))           # 2412 MHz
    w = _writes(dev)
    # bMode<<29 | fracMode<<28 | CHANSEL_2G(2412)
    assert w[R.AR_PHY_SYNTH_CONTROL] == (0x30000000 | R.CHANSEL_2G(2412)) == 0x30A0CCCC
    # channel != 14 -> JAPAN bit cleared in CCK_TX_CTRL.
    assert w[R.AR_PHY_CCK_TX_CTRL] == 0


def test_synth_preserves_top_bits():
    dev = FakeDev(read_val=0xC0000000)                    # top 2 bits set on read
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    phy.rf_set_freq(h, chanmod.channel_2ghz(6))           # 2437 MHz
    w = _writes(dev)
    assert w[R.AR_PHY_SYNTH_CONTROL] == (0xC0000000 | 0x30000000 | R.CHANSEL_2G(2437))
