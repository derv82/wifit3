"""M2b-3: ar9002 rf_claim emits the right AR_PHY probe, and the EEPROM fill walks the right
8-word REG_READ_MULTI address batches."""
import struct

from wifit3.chips.ar9271_v2 import eeprom, hw, phy, reg as R
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import WMI


class FakeDev:
    """Records writes; answers REG_READ with 0 and REG_READ_MULTI with N zero words."""

    def __init__(self):
        self.cmds = []

    def write(self, ep, data, timeout=None):
        cmd_id, seq = struct.unpack_from(">HH", data, 8)
        body = data[12:]
        self.cmds.append((cmd_id, body))
        nwords = max(1, len(body) // 4) if cmd_id == 0x14 else 1
        val = b"\x00\x00\x00\x00" * nwords
        self._resp = struct.pack(">BBH", 1, 0, 4 + len(val)) + b"\x00\x00\x00\x00" + \
            struct.pack(">HH", cmd_id, seq) + val
        return len(data)

    def read(self, ep, length, timeout=None):
        return bytearray(self._resp)


def test_reverse_bits():
    assert phy.reverse_bits(0x01, 8) == 0x80
    assert phy.reverse_bits(0x80, 8) == 0x01
    assert phy.reverse_bits(0x0F, 8) == 0xF0
    assert phy.reverse_bits(0xFF, 8) == 0xFF


def test_rf_claim_phy_writes():
    dev = FakeDev()
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    phy.rf_claim(h)
    WRITE = 0x0015
    # AR_PHY(0)=0x9800 single write of 7.
    assert dev.cmds[0] == (WRITE, bytes.fromhex("0000980000000007"))
    # get_radiorev buffered batch: AR_PHY(0x36)=0x98d8=0x7058 + 8x AR_PHY(0x20)=0x9880=0x10000.
    batch = "000098d800007058" + "0000988000010000" * 8
    assert dev.cmds[1] == (WRITE, bytes.fromhex(batch))
    # then a read of AR_PHY(256)=0x9c00.
    assert dev.cmds[2] == (0x0014, bytes.fromhex("00009c00"))


def test_eeprom_gen_fill_addresses():
    dev = FakeDev()
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    eeprom.fill(h)

    reads = [b for c, b in dev.cmds if c == 0x14]
    # 188 words from word 64 in 8-word batches => 23 full + 1 of 4 = 24 multi-reads.
    assert len(reads) == 24
    assert [len(b) // 4 for b in reads] == [8] * 23 + [4]

    # First batch addresses: words 64..71 -> 0x2100, 0x2104, ... 0x211c.
    first = [struct.unpack_from(">I", reads[0], k)[0] for k in range(0, 32, 4)]
    assert first == [R.AR5416_EEPROM_OFFSET + ((64 + i) << R.AR5416_EEPROM_S) for i in range(8)]
    # Last word read is 251.
    last = struct.unpack_from(">I", reads[-1], len(reads[-1]) - 4)[0]
    assert (last - R.AR5416_EEPROM_OFFSET) >> R.AR5416_EEPROM_S == 251
