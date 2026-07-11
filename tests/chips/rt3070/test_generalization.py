"""EEPROM-variant generalization: the RT3070 driver must run on ANY 148f:3070 regardless of
EEPROM contents, keeping the RF3020 reference byte-identical. Covers the RF-companion resolver
(NIC_CONF0.RF_TYPE, not the EEPROM_CHIP_ID landmine), the config_channel give-it-a-shot for an
unported/unburned RF, and the SW antenna-diversity port. No hardware — a recording fake dev
captures the control writes.
"""
from __future__ import annotations

from wifit3.chips.rt3070 import chan, constants as C
from wifit3.chips.rt3070.eeprom import parse_eeprom, resolve_rf_chip
from wifit3.chips.rt3070.state import DrvData
from wifit3.chips.rt3070.transport import RT3070Transport


class RecordingDev:
    """Fake usb.core.Device: reads return zeros (regbusy 'not busy'), writes are recorded as
    (register-address, 32-bit LE value)."""

    def __init__(self):
        self.writes: list[tuple[int, int]] = []

    def ctrl_transfer(self, rt, req, val, idx, data_or_len, timeout=None):
        if rt & 0x80:                                        # IN / read
            n = data_or_len if isinstance(data_or_len, int) else len(data_or_len)
            return b"\x00" * n
        data = bytes(data_or_len)
        if data:                                             # skip DEVICE_MODE (no data phase)
            self.writes.append((idx, int.from_bytes(data, "little")))
        return len(data)

    def value_at(self, addr: int) -> int | None:
        for a, v in self.writes:
            if a == addr:
                return v
        return None


def _eeprom(*, nic0: int = 0x0511, nic1: int = 0x0000, chip_id: int = 0x0000) -> bytes:
    """512-byte EEPROM image with the words the resolver / config path read. Default nic0 =
    RF3020 (0x05), 1T1R — the reference layout."""
    buf = bytearray(512)
    buf[0:2] = chip_id.to_bytes(2, "little")                              # EEPROM_CHIP_ID (word0)
    buf[C.EEPROM_NIC_CONF0 * 2:C.EEPROM_NIC_CONF0 * 2 + 2] = nic0.to_bytes(2, "little")
    buf[C.EEPROM_NIC_CONF1 * 2:C.EEPROM_NIC_CONF1 * 2 + 2] = nic1.to_bytes(2, "little")
    return bytes(buf)


# --- resolve_rf_chip ---------------------------------------------------------

def test_resolve_reference_rf3020_is_ported():
    rf = resolve_rf_chip(parse_eeprom(_eeprom(nic0=0x0511)))
    assert rf.rf_id == C.RF3020 and rf.name == "RF3020" and rf.ported


def test_resolve_uses_rf_type_not_chip_id_landmine():
    """The reference card's EEPROM_CHIP_ID (word0) reads 0x3070; resolving from it would
    wrongly pick RF3070 (config_channel_rf53xx). RT3070 silicon must use NIC_CONF0.RF_TYPE."""
    rf = resolve_rf_chip(parse_eeprom(_eeprom(nic0=0x0511, chip_id=0x3070)))
    assert rf.rf_id == C.RF3020 and rf.ported
    assert rf.rf_id != C.RF3070


def test_resolve_unported_rf_is_flagged_not_fatal():
    """RF3052 (0x09) is a foreign radio this driver does not port — resolved + named, ported
    False, so the driver warns + runs the silicon default rather than crashing."""
    rf = resolve_rf_chip(parse_eeprom(_eeprom(nic0=0x0911)))   # RF_TYPE=0x09
    assert rf.rf_id == C.RF3052 and rf.name == "RF3052" and not rf.ported


def test_resolve_blank_eeprom_default_rf2820_not_ported():
    """A blank NIC_CONF0 (0xffff) is fixed up to RF2820 by validate_eeprom; RF2820 is not in
    the rf3xxx set, so it's flagged untested but still runs on the silicon default."""
    from wifit3.chips.rt3070.eeprom import validate_eeprom
    buf = bytearray(_eeprom())
    buf[C.EEPROM_NIC_CONF0 * 2:C.EEPROM_NIC_CONF0 * 2 + 2] = (0xFFFF).to_bytes(2, "little")
    rf = resolve_rf_chip(parse_eeprom(validate_eeprom(bytes(buf))))
    assert rf.rf_id == C.RF2820 and not rf.ported


# --- config_channel give-it-a-shot ------------------------------------------

def _run_config_channel(nic0: int) -> RecordingDev:
    dev = RecordingDev()
    t = RT3070Transport(dev, timeout_ms=50)
    ev = parse_eeprom(_eeprom(nic0=nic0))
    chip = C.ChipInfo(rt=C.RT3070, rev=C.REV_RT3070F)
    drv = DrvData(calibration_bw20=8, calibration_bw40=139, bbp25=128, bbp26=0)
    chan.config_channel(t, chip, ev, drv, channel=1, lna_gain=0)
    return dev


def _rfcsr_regnums_written(dev: RecordingDev) -> set[int]:
    nums = set()
    for addr, val in dev.writes:
        if addr == C.RF_CSR_CFG and (val & C.RF_CSR_CFG_WRITE):
            nums.add(C.get_field(val, C.RF_CSR_CFG_REGNUM))
    return nums


def test_config_channel_reference_runs_rf3xxx():
    """RF3020 reference: config_channel programs the rf3xxx RFCSRs (2,3,6,12,13,...)."""
    nums = _rfcsr_regnums_written(_run_config_channel(0x0511))
    assert {2, 3, 6, 12, 13}.issubset(nums)


def test_config_channel_unported_rf_gives_it_a_shot():
    """RF3052 (0x09) no longer raises — it runs the same rf3xxx silicon-default tune."""
    nums = _rfcsr_regnums_written(_run_config_channel(0x0911))   # would have raised before
    assert {2, 3, 6, 12, 13}.issubset(nums)


# --- SW antenna diversity (config_ant) --------------------------------------

def _run_config_ant(ant_div: int, rx_chain: int = 1) -> RecordingDev:
    dev = RecordingDev()
    t = RT3070Transport(dev, timeout_ms=50)
    nic0 = 0x0500 | (rx_chain & 0xF) | ((rx_chain & 0xF) << 4)   # RF3020 + tx/rx chains
    nic1 = (ant_div << 11) & C.EEPROM_NIC_CONF1_ANT_DIVERSITY
    ev = parse_eeprom(_eeprom(nic0=nic0, nic1=nic1))
    chip = C.ChipInfo(rt=C.RT3070, rev=C.REV_RT3070F)
    chan.config_ant(t, chip, ev)
    return dev


def _ant_select_ran(dev: RecordingDev) -> bool:
    host = dev.value_at(C.HOST_CMD_CSR)
    return host is not None and C.get_field(host, C.HOST_CMD_CSR_HOST_COMMAND) == C.MCU_ANT_SELECT


def test_ant_diversity_zero_skips_set_ant_diversity():
    """The reference (ANT_DIVERSITY=0) never fires MCU_ANT_SELECT or the GPIO3 write."""
    dev = _run_config_ant(ant_div=0)
    assert not _ant_select_ran(dev)
    assert dev.value_at(C.GPIO_CTRL) is None


def test_ant_diversity_antenna_a_sets_eesk1_gpio0():
    """ANT_DIVERSITY 1/2 → default_ant.rx = ANTENNA_A → eesk pin 1, GPIO3 val 0."""
    dev = _run_config_ant(ant_div=1)
    assert _ant_select_ran(dev)
    mbox = dev.value_at(C.H2M_MAILBOX_CSR)
    assert C.get_field(mbox, C.H2M_MAILBOX_CSR_ARG0) == 1        # eesk_pin
    gpio = dev.value_at(C.GPIO_CTRL)
    assert C.get_field(gpio, C.GPIO_CTRL_VAL3) == 0 and C.get_field(gpio, C.GPIO_CTRL_DIR3) == 0


def test_ant_diversity_antenna_b_sets_eesk0_gpio1():
    """ANT_DIVERSITY 3 → default_ant.rx = ANTENNA_B → eesk pin 0, GPIO3 val 1."""
    dev = _run_config_ant(ant_div=3)
    assert _ant_select_ran(dev)
    mbox = dev.value_at(C.H2M_MAILBOX_CSR)
    assert C.get_field(mbox, C.H2M_MAILBOX_CSR_ARG0) == 0        # eesk_pin
    gpio = dev.value_at(C.GPIO_CTRL)
    assert C.get_field(gpio, C.GPIO_CTRL_VAL3) == 1
