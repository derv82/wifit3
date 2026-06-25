"""M2e-2: eep set_board_values — switch/gain/analog-bias/settling modal config."""
import struct

from wifit3.chips.ar9271_v2 import chan as chanmod, hw, phy_board, reg as R
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import WMI
from wifit3.chips.ar9271_v2.eeprom_4k import Map4k

from .test_txpower import EEPROM


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


def _run():
    dev = FakeDev()
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    h.macVersion = R.AR_SREV_VERSION_9271
    h.eeprom = bytearray(EEPROM)
    phy_board.set_board_values(h, chanmod.channel_2ghz(1))
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


def test_modal_field_decode():
    eep = Map4k(EEPROM)
    assert eep.antCtrlCommon == 0x11441411
    assert eep.txRxAttenCh0 == 32
    assert eep.ob[:3] == [5, 4, 4]
    assert eep.db1[0] == 4 and eep.db2[0] == 4
    assert eep.switchSettling == 44 and eep.thresh62 == 28


def test_switch_com_and_gain():
    dev = _run()
    w, rmw = _writes(dev), _rmws(dev)
    assert w[R.AR_PHY_SWITCH_COM] == 0x11441411
    # txRxAttenCh0=32 programmed into RXGAIN TXRX_ATTEN for both chain blocks.
    assert (R.AR_PHY_RXGAIN, R.SM(32, R.AR9280_PHY_RXGAIN_TXRX_ATTEN),
            R.AR9280_PHY_RXGAIN_TXRX_ATTEN) in rmw
    assert (R.AR_PHY_RXGAIN + 0x1000, R.SM(32, R.AR9280_PHY_RXGAIN_TXRX_ATTEN),
            R.AR9280_PHY_RXGAIN_TXRX_ATTEN) in rmw


def test_analog_bias():
    rmw = _rmws(_run())
    assert (R.AR9285_AN_RF2G3, R.SM(5, R.AR9271_AN_RF2G3_OB_cck), R.AR9271_AN_RF2G3_OB_cck) in rmw
    assert (R.AR9285_AN_RF2G3, R.SM(4, R.AR9271_AN_RF2G3_OB_psk), R.AR9271_AN_RF2G3_OB_psk) in rmw
    assert (R.AR9285_AN_RF2G4, R.SM(4, R.AR9271_AN_RF2G4_DB_2), R.AR9271_AN_RF2G4_DB_2) in rmw


def test_settling_batch():
    rmw = _rmws(_run())
    assert (R.AR_PHY_SETTLING, R.SM(44, R.AR_PHY_SETTLING_SWITCH), R.AR_PHY_SETTLING_SWITCH) in rmw
    assert (R.AR_PHY_CCA, R.SM(28, R.AR9280_PHY_CCA_THRESH62), R.AR9280_PHY_CCA_THRESH62) in rmw
    assert (R.AR_PHY_EXT_CCA0, R.SM(28, R.AR_PHY_EXT_CCA0_THRESH62),
            R.AR_PHY_EXT_CCA0_THRESH62) in rmw
