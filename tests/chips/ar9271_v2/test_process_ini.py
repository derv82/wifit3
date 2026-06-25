"""M2d-3: the generated initvals tables and process_ini's table-write order."""
import struct

from wifit3.chips.ar9271_v2 import chan as chanmod, hw, initvals as I, phy, reg as R
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import WMI


class FakeDev:
    def __init__(self):
        self.writes = []                 # (reg, val) pairs across all REG_WRITE commands

    def write(self, ep, data, timeout=None):
        cmd_id, seq = struct.unpack_from(">HH", data, 8)
        body = data[12:]
        if cmd_id == 0x15:
            for k in range(0, len(body), 8):
                self.writes.append(struct.unpack_from(">II", body, k))
        nwords = max(1, len(body) // 4) if cmd_id == 0x14 else 1
        self._resp = struct.pack(">BBH", 1, 0, 4 + 4 * nwords) + b"\x00\x00\x00\x00" + \
            struct.pack(">HH", cmd_id, seq) + b"\x00\x00\x00\x00" * nwords
        return len(data)

    def read(self, ep, length, timeout=None):
        return bytearray(self._resp)


def test_table_shapes():
    assert (len(I.MODES_9271), len(I.MODES_9271[0])) == (303, 5)
    assert (len(I.COMMON_9271), len(I.COMMON_9271[0])) == (325, 2)
    assert len(I.MODES_NORMAL_POWER_TX_GAIN_9271) == len(I.MODES_HIGH_POWER_TX_GAIN_9271) == 33
    # 2.4 GHz mode column (index 4) of the first iniModes row.
    assert I.MODES_9271[0] == [0x1030, 0x230, 0x460, 0x2c0, 0x160]


def test_process_ini_write_order():
    dev = FakeDev()
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    h.eeprom = bytearray(64)
    h.eeprom[31] = 0                      # txGainType = normal
    phy.process_ini(h, chanmod.channel_2ghz(1))

    w = dev.writes
    # Analog prologue.
    assert w[0] == (R.AR_PHY(0), 0x07)
    assert w[1] == (R.AR_PHY_ADC_SERIAL_CTL, R.AR_PHY_SEL_EXTERNAL_RADIO)
    assert w[2] == (R.AR_PHY_ADC_SERIAL_CTL, R.AR_PHY_SEL_INTERNAL_ADDAC)
    # Then iniModes(col4) + normal txgain(col4) + iniCommon(col1), in that order.
    body = w[3:]
    expected = ([(r[0], r[4]) for r in I.MODES_9271]
                + [(r[0], r[4]) for r in I.MODES_NORMAL_POWER_TX_GAIN_9271]
                + [(r[0], r[1]) for r in I.COMMON_9271])
    assert body == expected


def test_process_ini_high_power_selects_high_table():
    dev = FakeDev()
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    h.eeprom = bytearray(64)
    h.eeprom[31] = R.AR5416_EEP_TXGAIN_HIGH_POWER
    phy.process_ini(h, chanmod.channel_2ghz(1))
    high = [(r[0], r[4]) for r in I.MODES_HIGH_POWER_TX_GAIN_9271]
    assert all(p in dev.writes for p in high[:3])
