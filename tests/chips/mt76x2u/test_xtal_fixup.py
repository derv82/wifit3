"""`mt76x2u_mac_fixup_xtal` — EEPROM-derived XO_CTRL writes.

Kernel reference: mt76x2/usb_mac.c:9-60. Covers:
  - The signed-magnitude offset math on TRIM_2 low byte
  - The TRIM_2 high → TRIM_1 → 0x14 fallback chain for c2_val
  - The CFG-bus XO_CTRL5 (C2_VAL) RMW + XO_CTRL6 (C2_CTRL) SET
  - The conditional XO_CTRL7 write based on NIC_CONF_2.XTAL_OPTION
"""

from wifit3.chips.mt76x2u import constants as C
from wifit3.chips.mt76x2u import mac


# ---------------------------------------------------------------------------
# Pure offset/c2_val math (no transport needed)
# ---------------------------------------------------------------------------

def test_compute_xtal_trim_normal_positive_offset():
    """TRIM_2 = 0x14_05 → offset = +5 (bit 7 clear), c2_val = 0x14."""
    c2, off = mac._compute_xtal_trim(trim_2=0x1405, trim_1_byte=0x00)
    assert c2 == 0x14
    assert off == 5


def test_compute_xtal_trim_negative_offset_via_sign_bit():
    """TRIM_2 = 0x14_85 → bit 7 set on low byte → offset = -5."""
    c2, off = mac._compute_xtal_trim(trim_2=0x1485, trim_1_byte=0x00)
    assert c2 == 0x14
    assert off == -5


def test_compute_xtal_trim_low_ff_means_zero_offset():
    """Low byte 0xff → uninitialized → offset = 0."""
    c2, off = mac._compute_xtal_trim(trim_2=0x14FF, trim_1_byte=0x00)
    assert off == 0


def test_compute_xtal_trim_falls_back_to_trim1_when_high_is_zero():
    """TRIM_2 high == 0x00 → use TRIM_1 low byte (0x20 here)."""
    c2, _ = mac._compute_xtal_trim(trim_2=0x0005, trim_1_byte=0x20)
    assert c2 == 0x20


def test_compute_xtal_trim_falls_back_to_trim1_when_high_is_ff():
    """TRIM_2 high == 0xff → use TRIM_1."""
    c2, _ = mac._compute_xtal_trim(trim_2=0xFF05, trim_1_byte=0x20)
    assert c2 == 0x20


def test_compute_xtal_trim_falls_back_to_default_0x14():
    """Both TRIM_2 high AND TRIM_1 are 0xff → kernel default 0x14."""
    c2, _ = mac._compute_xtal_trim(trim_2=0xFF05, trim_1_byte=0xFF)
    assert c2 == 0x14


def test_compute_xtal_trim_clamps_c2_to_7_bits():
    """Kernel does `eep_val &= 0x7f` — top bit dropped."""
    c2, _ = mac._compute_xtal_trim(trim_2=0xFF05, trim_1_byte=0xF0)
    # TRIM_1 = 0xF0, clamped to 7 bits = 0x70
    assert c2 == 0x70


# ---------------------------------------------------------------------------
# Full _mac_fixup_xtal — wire-level register writes
# ---------------------------------------------------------------------------

class FakeTransport:
    def __init__(self, eeprom: dict[int, int] | None = None,
                 reg_reads: dict[int, int] | None = None):
        self.eeprom = dict(eeprom or {})
        self.regs = dict(reg_reads or {})
        self.writes: list[tuple[int, int]] = []
        self.rmws: list[tuple[int, int, int]] = []

    def read32(self, addr: int) -> int:
        # EEPROM reads carry MT_VEND_TYPE_EEPROM marker (BIT 31). The
        # production read_u16 already does the byte-shift to extract the
        # u16 — our fake just returns the full word stored at the aligned
        # address.
        if addr & C.MT_VEND_TYPE_EEPROM:
            offset = addr & 0xFFFF
            return self.eeprom.get(offset, 0)
        return self.regs.get(addr, 0)

    def write32(self, addr: int, value: int) -> None:
        self.writes.append((addr, value & 0xFFFFFFFF))

    def rmw32(self, addr: int, mask: int, value: int) -> None:
        cur = self.regs.get(addr, 0)
        new = ((cur & ~mask) | (value & mask)) & 0xFFFFFFFF
        self.regs[addr] = new
        self.rmws.append((addr, mask, value))


def _setup_eeprom(trim_2: int, trim_1_low: int, nic_conf_2: int) -> FakeTransport:
    """Pack EEPROM values into 4-byte words at our trim offsets.

    EEPROM offsets:
      0x09E = TRIM_2 (u16) — sits at word 0x09C (low 16 bits if 0x09C aligned)
      0x03A = TRIM_1 (u16)
      0x042 = NIC_CONF_2 (u16)
    """
    eep = {}
    # 0x09E is aligned at 0x09C; word = (whatever) | (trim_2 << 16)
    eep[0x09C] = (trim_2 & 0xFFFF) << 16
    # 0x03A is aligned at 0x038; word = (low half) | (trim_1 << 16)
    eep[0x038] = (trim_1_low & 0xFFFF) << 16
    # 0x042 is aligned at 0x040; word = (low half) | (nic_conf_2 << 16)
    eep[0x040] = (nic_conf_2 & 0xFFFF) << 16
    return FakeTransport(eeprom=eep)


def test_mac_fixup_xtal_writes_xo_ctrl5_rmw_with_computed_c2_val():
    """TRIM_2 = 0x14_05 → c2 + offset = 0x14 + 5 = 0x19 → bits 14:8."""
    t = _setup_eeprom(trim_2=0x1405, trim_1_low=0x00, nic_conf_2=0)
    mac._mac_fixup_xtal(t)
    expected_value = (0x19 << 8) & C.MT_XO_CTRL5_C2_VAL_MASK
    matched = [
        rmw for rmw in t.rmws
        if rmw == (C.MT_VEND_TYPE_CFG | C.MT_XO_CTRL5,
                   C.MT_XO_CTRL5_C2_VAL_MASK, expected_value)
    ]
    assert matched, f"XO_CTRL5 RMW not found in {t.rmws}"


def test_mac_fixup_xtal_sets_xo_ctrl6_c2_ctrl():
    """SET on XO_CTRL6.C2_CTRL → RMW with mask = value."""
    t = _setup_eeprom(trim_2=0x1405, trim_1_low=0x00, nic_conf_2=0)
    mac._mac_fixup_xtal(t)
    matched = [
        rmw for rmw in t.rmws
        if rmw == (C.MT_VEND_TYPE_CFG | C.MT_XO_CTRL6,
                   C.MT_XO_CTRL6_C2_CTRL_MASK, C.MT_XO_CTRL6_C2_CTRL_MASK)
    ]
    assert matched, f"XO_CTRL6 SET not found in {t.rmws}"


def test_mac_fixup_xtal_writes_xo_ctrl7_when_xtal_option_0():
    """NIC_CONF_2 with XTAL_OPTION=0 → XO_CTRL7 = 0x5C1FEE80 (default bus)."""
    t = _setup_eeprom(trim_2=0x1405, trim_1_low=0x00, nic_conf_2=0)
    mac._mac_fixup_xtal(t)
    assert any(
        addr == C.MT_XO_CTRL7 and val == 0x5C1FEE80
        for addr, val in t.writes
    )


def test_mac_fixup_xtal_writes_xo_ctrl7_when_xtal_option_1():
    """XTAL_OPTION=1 → XO_CTRL7 = 0x5C1FEED0 (default bus)."""
    nic_conf_2 = 1 << C.MT_EE_NIC_CONF_2_XTAL_OPTION_SHIFT
    t = _setup_eeprom(trim_2=0x1405, trim_1_low=0x00, nic_conf_2=nic_conf_2)
    mac._mac_fixup_xtal(t)
    assert any(
        addr == C.MT_XO_CTRL7 and val == 0x5C1FEED0
        for addr, val in t.writes
    )


def test_mac_fixup_xtal_skips_xo_ctrl7_when_xtal_option_2():
    """XTAL_OPTION=2 falls into the default branch → no XO_CTRL7 write."""
    nic_conf_2 = 2 << C.MT_EE_NIC_CONF_2_XTAL_OPTION_SHIFT
    t = _setup_eeprom(trim_2=0x1405, trim_1_low=0x00, nic_conf_2=nic_conf_2)
    mac._mac_fixup_xtal(t)
    assert not any(
        addr == C.MT_XO_CTRL7
        for addr, _ in t.writes
    )


def test_mac_fixup_xtal_does_the_504_50c_housekeeping():
    """The four MAC-engine writes around 0x504/0x50c are still there."""
    t = _setup_eeprom(trim_2=0x1405, trim_1_low=0x00, nic_conf_2=0)
    mac._mac_fixup_xtal(t)
    addrs = [addr for addr, _ in t.writes]
    assert 0x504 in addrs
    assert 0x50c in addrs
    # 0x504 gets written twice (set + clear)
    assert sum(1 for a, _ in t.writes if a == 0x504) == 2
