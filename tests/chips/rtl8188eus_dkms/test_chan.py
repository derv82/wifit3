"""Hardware-free regression for the RTL8188EUS (DKMS) channel tune.

Locks the stateful RfRegChnlVal RMW (channel field then 20 MHz BW field) and the two
RF_CHNLBW writes. Full byte-for-byte replay of the initial tune lives in
``scripts/chips/rtl8188eus_dkms/verify_channels.py``.
"""
from wifit3.chips.rtl8188eus_dkms import chan
from wifit3.chips.rtl8188eus_dkms.efuse import TxPwr2G

WIRE_TXPWR = TxPwr2G(
    cck_base=(0x30, 0x30, 0x2F, 0x2E, 0x2E, 0x2E),
    bw40_base=(0x33, 0x33, 0x33, 0x32, 0x31),
    cck_diff=0, ofdm_diff=1, bw20_diff=0,
)


class RegTx:
    """Stateful register fake; all reads start at 0 (txagc RMWs harmless)."""
    def __init__(self):
        self.regs = {}
        self.w8, self.w16, self.w32 = [], [], []

    def read8(self, a):
        return self.regs.get(a, 0) & 0xFF

    def read16(self, a):
        return self.regs.get(a, 0) & 0xFFFF

    def read32(self, a):
        return self.regs.get(a, 0)

    def write8(self, a, v):
        self.regs[a] = v & 0xFF
        self.w8.append((a, v & 0xFF))

    def write16(self, a, v):
        self.regs[a] = v & 0xFFFF
        self.w16.append((a, v & 0xFFFF))

    def write32(self, a, v):
        v &= 0xFFFFFFFF
        self.regs[a] = v
        self.w32.append((a, v))


def _rf18_writes(t):
    """The RF_CHNLBW (RF reg 0x18) values written via the path-A LSSI register 0x840."""
    return [v & 0xFFFFF for a, v in t.w32 if a == 0x0840 and ((v >> 20) & 0xFF) == 0x18]


def test_set_channel_ch1():
    t = RegTx()
    new = chan.set_channel(t, WIRE_TXPWR, 0x07407, 1)
    assert new == 0x07C01                       # ch1 in [9:0], 20 MHz (BIT10|BIT11)
    assert _rf18_writes(t) == [0x07401, 0x07C01]   # channel write, then BW write
    assert (0x0603, 0x04) in t.w8               # BWOPMODE |= BW_OPMODE_20MHZ


def test_set_channel_preserves_upper_bits_and_steps_channel():
    # A second tune from the BW-set state: channel field replaced, BW field re-asserted.
    t = RegTx()
    new = chan.set_channel(t, WIRE_TXPWR, 0x07C01, 6)
    assert (new & 0x3FF) == 6 and (new >> 10) & 0x3 == 0x3   # ch6, 20 MHz
    assert _rf18_writes(t)[0] & 0x3FF == 6                   # channel write carries ch6


def test_channels_2g():
    assert chan.CHANNELS_2G == list(range(1, 15))
