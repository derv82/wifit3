"""Hardware-free regression for the EFUSE read + chip-param decode.

The full byte-for-byte check vs the cold-boot capture is
`scripts/rtl8814au_dkms/verify_efuse_pcap.py`; this pins the header-unpacking and
the parsers on synthetic input.
"""
from wifit3.chips.rtl8814au_dkms import constants as C
from wifit3.chips.rtl8814au_dkms import efuse


class FakeEfuse:
    """Minimal EFUSE_CTRL device: serves a canned physical-byte stream."""

    def __init__(self, phys):
        self.phys = phys
        self.addr_lo = 0
        self.addr_hi = 0

    def read16(self, a):
        return 0

    def write16(self, a, v):
        pass

    def read8(self, a):
        if a == C.REG_EFUSE_CTRL + 2:
            return 0x20
        if a == C.REG_EFUSE_CTRL + 3:
            return 0x80                       # read-done flag always set
        if a == C.REG_EFUSE_CTRL:
            addr = (self.addr_hi << 8) | self.addr_lo
            return self.phys[addr] if addr < len(self.phys) else 0xFF
        return 0

    def write8(self, a, v):
        if a == C.REG_EFUSE_CTRL + 1:
            self.addr_lo = v & 0xFF
        elif a == C.REG_EFUSE_CTRL + 2:
            self.addr_hi = v & 0x03


def test_unpack_single_full_section():
    # One PG block: header 0x00 -> section 0, all 4 words enabled, then 8 bytes.
    payload = [0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88]
    phys = [0x00] + payload + [0xFF] * 8
    m = efuse._read_logical_map(FakeEfuse(phys))
    assert len(m) == C.EFUSE_MAP_LEN
    assert list(m[0:8]) == payload
    assert m[8] == 0xFF                        # untouched section reads 0xFF


def test_unpack_word_enable_skips_disabled_words():
    # header 0x0e -> section 0, wden 0x0e: only word0 enabled (2 bytes).
    phys = [0x0E, 0xAB, 0xCD, 0xFF]
    m = efuse._read_logical_map(FakeEfuse(phys))
    assert m[0] == 0xAB and m[1] == 0xCD
    assert m[2] == 0xFF and m[3] == 0xFF       # words 1..3 disabled


def test_parse_rfe_type():
    m = bytearray(b"\xFF" * C.EFUSE_MAP_LEN)
    m[C.EEPROM_RFE_OPTION] = 0x05
    assert efuse._parse_rfe_type(bytes(m)) == 0x05
    m[C.EEPROM_RFE_OPTION] = 0x85              # bit7 set -> fallback
    assert efuse._parse_rfe_type(bytes(m)) == C.RFE_TYPE_8814AU_FALLBACK
    m[C.EEPROM_RFE_OPTION] = 0xFF              # blank -> fallback
    assert efuse._parse_rfe_type(bytes(m)) == C.RFE_TYPE_8814AU_FALLBACK


def test_rf_path_decision():
    # [SRC] rtl8814a_rfpath_decision — antenna option (0xC9) + link speed -> rf_path.
    # Over USB 2 (not super-speed) every antenna option collapses to RF_2T4R.
    for opt in (0xFF, 0xEE, 0x66, 0x6F, 0x00):
        assert efuse._rf_path_decision(opt, is_super_speed=False, autoload_fail=False) == efuse.RF_2T4R
    # SuperSpeed promotes a 4T4R/3T3R part to RF_3T3R; a 2T part stays RF_2T4R.
    assert efuse._rf_path_decision(0xFF, True, False) == efuse.RF_3T3R   # 4T4R part
    assert efuse._rf_path_decision(0xEE, True, False) == efuse.RF_3T3R   # 3T3R part
    assert efuse._rf_path_decision(0x66, True, False) == efuse.RF_2T4R   # 2T2R part, not promoted
    assert efuse._rf_path_decision(0x6F, True, False) == efuse.RF_2T4R   # 2T4R part
    # Autoload failure ignores the byte and defaults to RF_2T4R even on a super-speed link.
    assert efuse._rf_path_decision(0xFF, True, autoload_fail=True) == efuse.RF_2T4R


def test_max_tx_cnt():
    # [SRC] hal_intf.c:293 min(3, rf_type_to_rf_tx_cnt): RF_2T4R->2, RF_3T3R->3, RF_4T4R->3.
    assert efuse._max_tx_cnt(efuse.RF_2T4R) == 2
    assert efuse._max_tx_cnt(efuse.RF_3T3R) == 3
    assert efuse._max_tx_cnt(efuse.RF_4T4R) == 3


def test_parse_crystal_cap():
    m = bytearray(b"\xFF" * C.EFUSE_MAP_LEN)
    m[C.EEPROM_XTAL] = 0x23
    assert efuse._parse_crystal_cap(bytes(m)) == 0x23
    m[C.EEPROM_XTAL] = 0xFF                    # blank -> default
    assert efuse._parse_crystal_cap(bytes(m)) == C.EEPROM_DEFAULT_CRYSTAL_CAP


def test_parse_bb_swing_2g():
    m = bytearray(b"\xFF" * C.EFUSE_MAP_LEN)
    # Unburned byte (0xFF) -> 0 dB (0x200) on every path.
    assert efuse._parse_bb_swing_2g(bytes(m)) == (0x200, 0x200, 0x200, 0x200)
    # Burned 0xE4 = 11_10_01_00 -> D=3(-9), C=2(-6), B=1(-3), A=0(0dB).
    m[C.EEPROM_TX_BBSWING_2G] = 0xE4
    assert efuse._parse_bb_swing_2g(bytes(m)) == (0x200, 0x16A, 0x101, 0x0B6)
    # 0x00 -> all 0 dB (the value this card's cold-boot wire writes).
    m[C.EEPROM_TX_BBSWING_2G] = 0x00
    assert efuse._parse_bb_swing_2g(bytes(m)) == (0x200, 0x200, 0x200, 0x200)


def test_parse_bb_swing_5g():
    # Same 2-bit-per-path decode as 2.4 GHz, but from efuse byte 0xC7 (M5e).
    m = bytearray(b"\xFF" * C.EFUSE_MAP_LEN)
    assert efuse._parse_bb_swing_5g(bytes(m)) == (0x200, 0x200, 0x200, 0x200)
    m[C.EEPROM_TX_BBSWING_5G] = 0xE4           # 11_10_01_00 -> D=3,C=2,B=1,A=0
    assert efuse._parse_bb_swing_5g(bytes(m)) == (0x200, 0x16A, 0x101, 0x0B6)
    # This card's 5G fuse: index 1 on every path -> -3 dB (0x16A), per the cold-boot wire.
    m[C.EEPROM_TX_BBSWING_5G] = 0x55           # 01_01_01_01
    assert efuse._parse_bb_swing_5g(bytes(m)) == (0x16A, 0x16A, 0x16A, 0x16A)


def test_parse_mac_address():
    m = bytearray(b"\xFF" * C.EFUSE_MAP_LEN)
    assert efuse._parse_mac_address(bytes(m)) is None   # all-FF blank
    m[C.EEPROM_MAC_ADDR:C.EEPROM_MAC_ADDR + 6] = bytes([0x00] * 6)
    assert efuse._parse_mac_address(bytes(m)) is None   # all-zero invalid
    m[C.EEPROM_MAC_ADDR:C.EEPROM_MAC_ADDR + 6] = bytes(
        [0x00, 0xC0, 0xCA, 0x11, 0x22, 0x33])
    assert efuse._parse_mac_address(bytes(m)) == "00:c0:ca:11:22:33"
