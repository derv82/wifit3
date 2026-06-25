"""M2c-4: the channel model + compute_pll_control + init_pll wire output."""
import struct

from wifit3.chips.ar9271_v2 import chan as chanmod, hw, phy, reg as R
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import WMI


class FakeDev:
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


def test_channel_2ghz():
    c = chanmod.channel_2ghz(1)
    assert c.center_freq == 2412 and c.is_2ghz() and not c.is_5ghz()
    assert chanmod.channel_2ghz(11).center_freq == 2462


def test_compute_pll_control_2ghz():
    # AR9271 2.4 GHz: ref_div=5 (<<10) | pll_div=0x2c -> 0x142c.
    assert phy.compute_pll_control(None, chanmod.channel_2ghz(1)) == 0x142C
    assert phy.compute_pll_control(None, chanmod.channel_2ghz(11)) == 0x142C


def test_init_pll_writes():
    dev = FakeDev()
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    h.init_pll(chanmod.channel_2ghz(1))
    WRITE = 0x0015
    assert [(c, b.hex()) for c, b in dev.cmds] == [
        (WRITE, "0000701400001 42c".replace(" ", "")),   # AR_RTC_PLL_CONTROL = 0x142c
        (WRITE, "0005004000000304"),                      # core clock 117 MHz
        (WRITE, "0000704800000002"),                      # AR_RTC_SLEEP_CLK = FORCE_DERIVED
    ]
    assert R.AR_RTC_PLL_CONTROL == 0x7014
