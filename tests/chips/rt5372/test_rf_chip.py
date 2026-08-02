"""RF companion-chip identification + EEPROM-gated tune branches for the standalone
rt5372 (RT5392) driver.

The generalization goal: run on ANY 148f:5372 card regardless of EEPROM contents.
The RT5392 silicon is fixed (drives init_bbp/init_rfcsr), but the RF chip and the
antenna chain count come from the runtime EEPROM — RF5372 (2T2R, the captured
reference) vs RF5370 (1T1R) vs RF5390/RF5392, distinguished only by EEPROM_CHIP_ID.
[SRC] driver_sources/rt2x00-source-v6.18/rt2800lib.c:11182-11235 (RF resolve),
3387-3474 (config_channel_rf53xx RFCSR59 tables).
"""
from __future__ import annotations

from wifit3.chips.rt5372 import chan
from wifit3.chips.rt5372 import constants as C
from wifit3.chips.rt5372.constants import RF5370, RF5372, RF5390, RF5392, RT5390, RT5392, ChipInfo
from wifit3.chips.rt5372.eeprom import parse_eeprom, resolve_rf_chip


def _eeprom(chip_id: int = 0, nic_conf0: int = 0, nic_conf1: int = 0) -> bytes:
    """Craft a minimal EEPROM image with the words resolve_rf_chip / the tune read."""
    buf = bytearray(C.EEPROM_SIZE)
    buf[C.EEPROM_CHIP_ID * 2:C.EEPROM_CHIP_ID * 2 + 2] = chip_id.to_bytes(2, "little")
    buf[C.EEPROM_NIC_CONF0 * 2:C.EEPROM_NIC_CONF0 * 2 + 2] = nic_conf0.to_bytes(2, "little")
    buf[C.EEPROM_NIC_CONF1 * 2:C.EEPROM_NIC_CONF1 * 2 + 2] = nic_conf1.to_bytes(2, "little")
    return bytes(buf)


# ---- EepromValues field decode --------------------------------------------
def test_chip_id_word_is_eeprom_word0():
    assert parse_eeprom(_eeprom(chip_id=0x5372)).chip_id_word == 0x5372


def test_rf_type_nibble_decodes_nic_conf0_bits_11_8():
    """NIC_CONF0.RF_TYPE = FIELD16(0x0f00) — independent of the antenna low byte."""
    ev = parse_eeprom(_eeprom(nic_conf0=0x0922))   # RF nibble 0x9, txpath=rxpath=2
    assert ev.rf_type_nibble == 0x9
    assert ev.tx_chain_num == 2 and ev.rx_chain_num == 2


def test_looks_unburned_on_blank_and_erased_chip_id():
    assert parse_eeprom(_eeprom(chip_id=0x0000)).looks_unburned is True
    assert parse_eeprom(_eeprom(chip_id=0xFFFF)).looks_unburned is True
    assert parse_eeprom(_eeprom(chip_id=0x5372)).looks_unburned is False


# ---- resolve_rf_chip (kernel rt2800_init_eeprom) --------------------------
def test_resolve_reference_rf5372_2t2r_ported():
    """Captured PAU05/PAU06: RT5392 silicon, EEPROM_CHIP_ID=0x5372 → RF5372, ported."""
    rf = resolve_rf_chip(RT5392, parse_eeprom(_eeprom(chip_id=RF5372, nic_conf0=0x0022)))
    assert (rf.rf_id, rf.name, rf.ported) == (RF5372, "RF5372", True)


def test_resolve_rf5370_1t1r_ported():
    """The 1T1R sibling — same RT5392 silicon, EEPROM_CHIP_ID=RF5370, txpath=rxpath=1.
    RF id + chain count both come from the EEPROM, not the silicon."""
    ev = parse_eeprom(_eeprom(chip_id=RF5370, nic_conf0=0x0011))
    rf = resolve_rf_chip(RT5392, ev)
    assert (rf.rf_id, rf.name, rf.ported) == (RF5370, "RF5370", True)
    assert ev.tx_chain_num == 1 and ev.rx_chain_num == 1


def test_resolve_rf5390_and_rf5392_ported():
    assert resolve_rf_chip(RT5392, parse_eeprom(_eeprom(chip_id=RF5390))).rf_id == RF5390
    assert resolve_rf_chip(RT5392, parse_eeprom(_eeprom(chip_id=RF5392))).ported is True


def test_resolve_rt5390_silicon_also_reads_chip_id_word():
    """Both RT5390 and RT5392 silicon take the RF id from EEPROM_CHIP_ID, not the
    NIC_CONF0 nibble. [SRC] rt2800lib.c:11187-11191."""
    rf = resolve_rf_chip(RT5390, parse_eeprom(_eeprom(chip_id=RF5370, nic_conf0=0x0922)))
    assert rf.rf_id == RF5370   # CHIP_ID wins over the 0x9 RF_TYPE nibble


def test_resolve_unburned_chip_id_not_fatal():
    """Erased EFUSE reads EEPROM_CHIP_ID=0 → kernel -ENODEVs; we return rf_id=0
    (ported=False, name '0x0000') and the caller runs the silicon default."""
    rf = resolve_rf_chip(RT5392, parse_eeprom(_eeprom(chip_id=0x0000)))
    assert (rf.rf_id, rf.name, rf.ported) == (0x0000, "0x0000", False)


def test_resolve_unknown_burned_rf_marked_unported():
    """A burned EEPROM claiming an RF with no ported tune path is flagged unported,
    not crashed — the driver still runs the silicon-default rf53xx tune + warns."""
    rf = resolve_rf_chip(RT5392, parse_eeprom(_eeprom(chip_id=0x1234)))
    assert (rf.rf_id, rf.name, rf.ported) == (0x1234, "0x1234", False)


def test_resolve_non_rt539x_silicon_falls_back_to_nic_conf0_nibble():
    """Defensive fallback for a mislabeled card whose MAC_CSR0 is not RT5390/RT5392:
    the RF id comes from NIC_CONF0.RF_TYPE, faithful to the kernel else-branch."""
    rf = resolve_rf_chip(0x3572, parse_eeprom(_eeprom(chip_id=RF5372, nic_conf0=0x0922)))
    assert rf.rf_id == 0x9   # RF_TYPE nibble, NOT the 0x5372 CHIP_ID word


# ---- config_channel_rf53xx RFCSR59 table (bt_coexist gate) ----------------
class _RecordingTransport:
    """Records rfcsr_write; every read returns 0 so freq_cal_mode1 is a no-op."""

    def __init__(self):
        self.rfcsr_writes: dict[int, int] = {}

    def rfcsr_read(self, reg):
        return 0

    def rfcsr_write(self, reg, val):
        self.rfcsr_writes[reg] = val

    def register_read(self, addr):
        return 0

    def register_write(self, addr, val):
        pass

    def mcu_request(self, *a, **k):
        pass


def _run_rf53xx(nic_conf1: int, channel: int) -> _RecordingTransport:
    t = _RecordingTransport()
    ev = parse_eeprom(_eeprom(chip_id=RF5372, nic_conf0=0x0022, nic_conf1=nic_conf1))
    chan.config_channel_rf53xx(t, ChipInfo(rt=RT5392, rev=0x0223), ev,
                               rf=(0, 0, 0), default_power1=0, default_power2=0,
                               channel=channel)
    return t


def test_rf53xx_non_bt_reference_writes_rf59_non_bt():
    """Reference bt_coexist=0 → RFCSR59 from RF59_NON_BT (byte-identical to before)."""
    for ch in (1, 7, 14):
        t = _run_rf53xx(nic_conf1=0x0000, channel=ch)
        assert t.rfcsr_writes[59] == C.RF59_NON_BT[ch - 1]


def test_rf53xx_bt_coexist_writes_rf59_bt_no_raise():
    """A BT-combo RT5392 card (NIC_CONF1 BT_COEXIST set) writes RFCSR59 from RF59_BT
    instead of raising — the fail-loud branch is now a ported, runtime-gated table."""
    for ch in (1, 8, 14):
        t = _run_rf53xx(nic_conf1=C.EEPROM_NIC_CONF1_BT_COEXIST, channel=ch)
        assert t.rfcsr_writes[59] == C.RF59_BT[ch - 1]
