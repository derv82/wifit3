"""M2d-1: ath9k_hw_reset opening — preamble saves, chip_reset (WARM, no pending TX/RX),
init_pll, and the AR9271 RF-reset / MAC-gate writes, in order."""
import struct

from wifit3.chips.ar9271_v2 import chan as chanmod, hw, reg as R
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import WMI


class FakeDev:
    """Reads return 0 (no pending TX/RX, idle TSF/LED); records every command."""

    def __init__(self):
        self.cmds = []

    def write(self, ep, data, timeout=None):
        cmd_id, seq = struct.unpack_from(">HH", data, 8)
        self.cmds.append((cmd_id, data[12:]))
        nwords = max(1, len(data[12:]) // 4) if cmd_id == 0x14 else 1
        self._resp = struct.pack(">BBH", 1, 0, 4 + 4 * nwords) + b"\x00\x00\x00\x00" + \
            struct.pack(">HH", cmd_id, seq) + b"\x00\x00\x00\x00" * nwords
        return len(data)

    def read(self, ep, length, timeout=None):
        return bytearray(self._resp)


def _reads(dev):
    return [struct.unpack(">I", b)[0] for c, b in dev.cmds if c == 0x14]


def _writes(dev):
    return [(struct.unpack_from(">I", b, 0)[0], struct.unpack_from(">I", b, 4)[0])
            for c, b in dev.cmds if c == 0x15 and len(b) == 8]


def _hw(dev):
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    h.reset_power_on = True          # set during init_reset in the real flow
    h.chip_fullsleep = False
    return h


def test_reset_begin_preamble_reads():
    dev = FakeDev()
    h = _hw(dev)
    h.reset_begin(chanmod.channel_2ghz(1))
    reads = _reads(dev)
    # DEF_ANTENNA, STA_ID1, TSF(U32,L32,U32), CFG_LED, then chip_reset's Q_TXE + CR.
    assert reads[:5] == [R.AR_DEF_ANTENNA, R.AR_STA_ID1, R.AR_TSF_U32, R.AR_TSF_L32, R.AR_TSF_U32]
    assert reads[5] == R.AR_CFG_LED
    assert R.AR_Q_TXE in reads and R.AR_CR in reads


def _all_pairs(dev):
    pairs = []
    for c, b in dev.cmds:
        if c == 0x15:
            pairs += [struct.unpack_from(">II", b, k) for k in range(0, len(b) - 4, 8)]
    return pairs


def test_reset_begin_key_writes():
    dev = FakeDev()
    h = _hw(dev)
    h.reset_begin(chanmod.channel_2ghz(1))
    pairs = _all_pairs(dev)
    assert (R.AR_PHY_ACTIVE, 0) in pairs                                  # mark_phy_inactive
    assert (R.AR9271_RESET_POWER_DOWN_CONTROL, R.AR9271_RADIO_RF_RST) in pairs
    assert (R.AR9271_RESET_POWER_DOWN_CONTROL, R.AR9271_GATE_MAC_CTL) in pairs
    assert (R.AR_RTC_PLL_CONTROL, 0x142C) in pairs                        # init_pll ran
    # WARM reset: AR_RTC_RC flush carries MAC_WARM only (no MAC_COLD bit).
    assert (R.AR_RTC_RC, R.AR_RTC_RC_MAC_WARM) in pairs
    assert h.chip_fullsleep is False
