"""M2d-6: apply_txpower — TPCRG1 gain config, PDADC cal table, per-rate power registers.

The expected register values are the bytes the ath9k_htc driver puts on the wire during the
cold-boot reset (channel 1, 20 MHz) for this card's 4k EEPROM. The EEPROM image below is the
4k map read off the capture; the computation is deterministic from it.
"""
import struct

from wifit3.chips.ar9271_v2 import chan as chanmod, hw, phy_power, reg as R
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import WMI

# 376-byte map4k image (capture-1), little-endian struct ar5416_eeprom_4k.
EEPROM = bytes.fromhex(
    "7801a09d0fe0020060000000c01c304f78b00101000000000000007f08001800000000000000"
    "00000000000000000000000000000000000011144411002c2000e2000d00020e1cba0c010000"
    "064504010e0e0000002c00000404044000900000000100000080000000000000000000000000"
    "000000000000708eac1f212a40564c56616c740c0f132b6525355079ac1c1e2a3d5549555f6a"
    "730a0b12255a1f3044679c1d1f283e544a545f6a730c0b1126571f2d4467a47024242424b824"
    "242424ff00000000701e1e1e1e891e1e1e1eac1e1e1e1e701c1c1c1c1c1c1c1c891c1c1c1c1c"
    "1c1c1cac1c1c1c1c1c1c1c1c701a1a1a1a1a1a1a1a891a1a1a1a1a1a1a1aac1a1a1a1a1a1a1a"
    "1a111215174142454731323537703c757ca23c0000703c757ca23c0000703c757ca23c00007a"
    "3c7f7c937c983c703c757cac3cb83c703c757cac3c0000703c757cac3c00007a3c7f7c937ca2"
    "3c703c757cac3c0000703c757cac3c0000703c757cac3c00007a3c7f7c937ca23c00")


class FakeDev:
    def __init__(self):
        self.cmds = []

    def write(self, ep, data, timeout=None):
        cmd_id, seq = struct.unpack_from(">HH", data, 8)
        self.cmds.append((cmd_id, data[12:]))
        self._resp = struct.pack(">BBH", 1, 0, 8) + b"\x00\x00\x00\x00" + \
            struct.pack(">HH", cmd_id, seq) + struct.pack(">I", 0)
        return len(data)

    def read(self, ep, length, timeout=None):
        return bytearray(self._resp)


def _writes(dev):
    out = {}
    for c, b in dev.cmds:
        if c == 0x15:                       # REG_WRITE (single or multi)
            for k in range(0, len(b) - 4, 8):
                reg, val = struct.unpack_from(">II", b, k)
                out[reg] = val
    return out


def _rmws(dev):
    out = []
    for c, b in dev.cmds:
        if c == 0x20:                       # REG_RMW
            for k in range(0, len(b) - 4, 12):
                out.append(struct.unpack_from(">III", b, k))
    return out


def _run(ch=1):
    dev = FakeDev()
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    h.eeprom = bytearray(EEPROM)
    phy_power.apply_txpower(h, chanmod.channel_2ghz(ch))
    return dev


def test_gain_boundaries_interpolated_channel():
    # Channel 10 (2457 MHz) falls between piers -> the interpolated PDADC path. The gain-boundary
    # default fill must not clobber boundary[numXpdGains-1] (the C-loop-index off-by-one).
    w = _writes(_run(ch=10))
    assert w[R.AR_PHY_TPCRG5] == 0x0EBAE676      # gainBoundaries[1] = 57, not 58


def test_tpcrg1_gain_config():
    # numXpdGain=2 -> NUM_PD_GAIN=(2-1)&3=1; xpdGainValues=[3,2]; PD_GAIN_3=0.
    rmws = _rmws(_run())
    assert (R.AR_PHY_TPCRG1, R.SM(1, R.AR_PHY_TPCRG1_NUM_PD_GAIN), R.AR_PHY_TPCRG1_NUM_PD_GAIN) in rmws
    assert (R.AR_PHY_TPCRG1, R.SM(3, R.AR_PHY_TPCRG1_PD_GAIN_1), R.AR_PHY_TPCRG1_PD_GAIN_1) in rmws
    assert (R.AR_PHY_TPCRG1, R.SM(2, R.AR_PHY_TPCRG1_PD_GAIN_2), R.AR_PHY_TPCRG1_PD_GAIN_2) in rmws
    assert (R.AR_PHY_TPCRG1, 0, R.AR_PHY_TPCRG1_PD_GAIN_3) in rmws


def test_pdadc_and_boundaries():
    w = _writes(_run())
    assert w[R.AR_PHY_TPCRG5] == 0x0EBAEA86
    # 32 PDADC words at AR_PHY_BASE + (672<<2) = 0xa280.
    base = R.AR_PHY_BASE + (672 << 2)
    assert w[base] == 0x00000000             # word 0
    assert w[base + 0x0C] == 0x0C090603      # word 3
    assert w[base + 0x40] == 0x8579716A      # word 16
    assert w[base + 0x7C] == 0xFAFAFAFA      # word 31 (tail padding)


def test_per_rate_power_registers():
    w = _writes(_run())
    assert w[R.AR_PHY_POWER_TX_RATE1] == 0x28282828   # OFDM 6/9/12/18
    assert w[R.AR_PHY_POWER_TX_RATE2] == 0x28282828   # OFDM 24/36/48/54
    assert w[R.AR_PHY_POWER_TX_RATE3] == 0x2E2E282E   # CCK + XR
    assert w[R.AR_PHY_POWER_TX_RATE4] == 0x2E2E2E2E   # CCK
    assert w[R.AR_PHY_POWER_TX_RATE5] == 0x26262626   # HT20 0-3
    assert w[R.AR_PHY_POWER_TX_RATE6] == 0x26262626   # HT20 4-7
    assert w[R.AR_PHY_POWER_TX_RATE_MAX] == R.MAX_RATE_POWER   # TPC disabled
