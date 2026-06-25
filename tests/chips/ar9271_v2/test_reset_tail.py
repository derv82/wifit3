"""M2e-1: the ath9k_hw_reset tail after process_ini — rfmode, MFP, delta-slope, spur.

Expected register ops are the bytes the ath9k_htc driver puts on the wire on channel 1
(2412 MHz, 20 MHz) during the cold-boot reset.
"""
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
            struct.pack(">HH", cmd_id, seq) + struct.pack(">I", 0)
        return len(data)

    def read(self, ep, length, timeout=None):
        return bytearray(self._resp)


def _mk(spur0=R.AR_NO_SPUR):
    dev = FakeDev()
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    h.macVersion = R.AR_SREV_VERSION_9271
    h.eeprom = bytearray(376)
    struct.pack_into("<H", h.eeprom, 52 + 48, spur0)      # modalHeader.spurChans[0].spurChan
    return dev, h


def _writes(dev):
    out = []
    for c, b in dev.cmds:
        if c == 0x15:
            out += [struct.unpack_from(">II", b, k) for k in range(0, len(b) - 4, 8)]
    return out


def _rmws(dev):
    out = []
    for c, b in dev.cmds:
        if c == 0x20:
            out += [struct.unpack_from(">III", b, k) for k in range(0, len(b) - 4, 12)]
    return out


def test_set_rfmode_2ghz():
    dev, h = _mk()
    phy.set_rfmode(h, chanmod.channel_2ghz(1))
    assert (R.AR_PHY_MODE, R.AR_PHY_MODE_DYNAMIC) in _writes(dev)


def test_init_mfp():
    dev, h = _mk()
    h.init_mfp()
    assert (R.AR_AES_MUTE_MASK1, 0xC7FF0000, 0xFFFF0000) in _rmws(dev)
    assert h.sw_mgmt_crypto_tx is True and h.sw_mgmt_crypto_rx is False


def test_set_delta_slope():
    dev, h = _mk()
    phy.set_delta_slope(h, chanmod.channel_2ghz(1))     # synth 2412 MHz
    rmw = _rmws(dev)
    assert (R.AR_PHY_TIMING3, 0xA9D20000, R.AR_PHY_TIMING3_DSC_MAN) in rmw
    assert (R.AR_PHY_TIMING3, 0x00006000, R.AR_PHY_TIMING3_DSC_EXP) in rmw
    assert (R.AR_PHY_HALFGI, 0x0004C6B0, R.AR_PHY_HALFGI_DSC_MAN) in rmw
    assert (R.AR_PHY_HALFGI, 0x00000003, R.AR_PHY_HALFGI_DSC_EXP) in rmw


def test_spur_mitigate_no_spur():
    dev, h = _mk(spur0=R.AR_NO_SPUR)
    phy.spur_mitigate(h, chanmod.channel_2ghz(1))
    # No in-band spur -> a single REG_CLR_BIT on AR_PHY_FORCE_CLKEN_CCK.
    assert _rmws(dev) == [(R.AR_PHY_FORCE_CLKEN_CCK, 0, R.AR_PHY_FORCE_CLKEN_CCK_MRC_MUX)]
