"""M5: htc-start tx-power update — priv->txpowlimit=0 clamps every per-rate power to 0x0a.

The gain config (AR_PHY_TPCRG1 fields) and the 32-word PDADC table are limit-independent, so
they match the reset-time apply_txpower; only the six per-rate registers change.
"""
import struct

from wifit3.chips.ar9271_v2 import chan as chanmod, hw, phy_power, reg as R
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import WMI

# A minimal but valid 4k EEPROM image: real bring-up fills hw.eeprom from the device. The
# per-rate clamp to 0 holds for any image because new_pwr is 0, so an all-zero map suffices
# for asserting the rate registers (the PDADC/gain values are exercised by test_txpower).
EEPROM = bytes(0x440)


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
        if c == 0x15:
            for k in range(0, len(b), 8):
                reg, val = struct.unpack_from(">II", b, k)
                out[reg] = val
    return out


def _run(new_txpow):
    dev = FakeDev()
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    h.eeprom = bytearray(EEPROM)
    phy_power.update_txpow(h, chanmod.channel_2ghz(1), new_txpow)
    return _writes(dev)


def test_txpowlimit_zero_clamps_all_rates():
    w = _run(0)
    # +10 table offset on a clamped-to-0 target -> 0x0a in every byte of the six rate regs.
    for reg in (R.AR_PHY_POWER_TX_RATE1, R.AR_PHY_POWER_TX_RATE2, R.AR_PHY_POWER_TX_RATE3,
                R.AR_PHY_POWER_TX_RATE4, R.AR_PHY_POWER_TX_RATE5, R.AR_PHY_POWER_TX_RATE6):
        assert w[reg] == 0x0A0A0A0A
    # TPC disabled -> the per-rate max register carries MAX_RATE_POWER.
    assert w[R.AR_PHY_POWER_TX_RATE_MAX] == R.MAX_RATE_POWER
