"""Generalization tests for the rt5370 (RT5390) driver: prove it decodes + tunes for
ANY 148f:5370 EEPROM burn (RF chip, chain count, BT-coex), not just the captured
reference (RF5370, 1T1R, non-BT, rev-F). The cold-boot wire byte-identity is the job of
scripts/chips/rt5370/verify_pcap.py; these cover the runtime-EEPROM branches it can't (its
capture only ever shows the one card)."""
from __future__ import annotations

from wifit3.chips.rt5370 import chan
from wifit3.chips.rt5370 import constants as C
from wifit3.chips.rt5370.constants import ChipInfo, get_field
from wifit3.chips.rt5370.eeprom import (
    RfChip, parse_eeprom, resolve_rf_chip,
)

# rev of the reference card + a pre-F stand-in for the pre-rev arms.
REV_F = ChipInfo(rt=C.RT5390, rev=C.REV_RT5390F)
REV_PRE_F = ChipInfo(rt=C.RT5390, rev=0x0500)


def _eeprom(*, chip_id=0x5370, nic_conf0=0xFF11, nic_conf1=0x0000):
    """A minimal burned EEPROM image with the words the tune/resolve read.
    Defaults reproduce the reference: RF5370, 1T1R (NIC_CONF0 low byte 0x11),
    non-BT (NIC_CONF1=0)."""
    buf = bytearray(C.EEPROM_SIZE)

    def w(word_idx, val):
        buf[word_idx * 2] = val & 0xFF
        buf[word_idx * 2 + 1] = (val >> 8) & 0xFF

    w(C.EEPROM_CHIP_ID, chip_id)
    w(C.EEPROM_NIC_CONF0, nic_conf0)
    w(C.EEPROM_NIC_CONF1, nic_conf1)
    return parse_eeprom(bytes(buf))


class RecordingTransport:
    """Records rfcsr/bbp/register writes; serves reads from a dict (default 0)."""

    def __init__(self, initial=None):
        self.regs = dict(initial or {})
        self.rfcsr = {}
        self.bbp = {}
        self.rfcsr_writes: list[tuple[int, int]] = []
        self.bbp_writes: list[tuple[int, int]] = []
        self.reg_writes: list[tuple[int, int]] = []

    def rfcsr_read(self, n):
        return self.rfcsr.get(n, 0)

    def rfcsr_write(self, n, v):
        self.rfcsr[n] = v & 0xFF
        self.rfcsr_writes.append((n, v & 0xFF))

    def bbp_read(self, n):
        return self.bbp.get(n, 0)

    def bbp_write(self, n, v):
        self.bbp[n] = v & 0xFF
        self.bbp_writes.append((n, v & 0xFF))

    def register_read(self, addr):
        return self.regs.get(addr, 0)

    def register_write(self, addr, v):
        self.regs[addr] = v & 0xFFFFFFFF
        self.reg_writes.append((addr, v & 0xFFFFFFFF))

    def mcu_request(self, *a, **k):
        pass


# ---------------------------------------------------------------------------
# resolve_rf_chip — the EEPROM RF discriminator [SRC rt2800lib.c:11187-11201]
# ---------------------------------------------------------------------------

def test_resolve_rf_chip_reference_is_rf5370_ported():
    rf = resolve_rf_chip(C.RT5390, _eeprom(chip_id=0x5370))
    assert rf == RfChip(rf_id=C.RF5370, name="RF5370", ported=True)


def test_resolve_rf_chip_rf5372_2t2r_sibling_is_ported():
    """Same RT5390 silicon, EEPROM_CHIP_ID=RF5372 → the 2T2R sibling. Ported (the
    RT5392-silicon chain-2 writes are silicon-gated, but the rf53xx tune covers it)."""
    rf = resolve_rf_chip(C.RT5390, _eeprom(chip_id=0x5372, nic_conf0=0xFF22))
    assert rf.rf_id == C.RF5372 and rf.name == "RF5372" and rf.ported is True


def test_resolve_rf_chip_rf5390_and_rf5392_ported():
    assert resolve_rf_chip(C.RT5390, _eeprom(chip_id=0x5390)).ported is True
    assert resolve_rf_chip(C.RT5392, _eeprom(chip_id=0x5392)).ported is True


def test_resolve_rf_chip_rf5592_named_but_unported():
    """RF5592 → the kernel dispatches to config_channel_rf55xx; this driver has no
    rf55xx path, so it's flagged unported (caller logs 'untested variant' + runs rf53xx)."""
    rf = resolve_rf_chip(C.RT5390, _eeprom(chip_id=0x000F))
    assert rf.rf_id == C.RF5592 and rf.name == "RF5592" and rf.ported is False


def test_resolve_rf_chip_unburned_is_zero_not_fail():
    """Erased EEPROM (all zero) → EEPROM_CHIP_ID=0, rf_id=0, unported. The kernel
    -ENODEVs; we return rf_id=0 so the caller still brings the card up."""
    rf = resolve_rf_chip(C.RT5390, _eeprom(chip_id=0x0000, nic_conf0=0x0000))
    assert rf.rf_id == 0 and rf.ported is False


def test_resolve_rf_chip_unknown_id_hex_named():
    rf = resolve_rf_chip(C.RT5390, _eeprom(chip_id=0xABCD))
    assert rf.rf_id == 0xABCD and rf.name == "0xabcd" and rf.ported is False


def test_looks_unburned_flag():
    assert _eeprom(nic_conf0=0x0000).looks_unburned is True
    assert _eeprom(nic_conf0=0xFFFF).looks_unburned is True
    assert _eeprom(nic_conf0=0xFF11).looks_unburned is False   # reference


# ---------------------------------------------------------------------------
# chain count comes from NIC_CONF0, not silicon [SRC rt2800lib.c:11240-11243]
# ---------------------------------------------------------------------------

def test_chain_count_decodes_1t1r_and_2t2r():
    ref = _eeprom(nic_conf0=0xFF11)
    assert (ref.tx_chain_num, ref.rx_chain_num) == (1, 1)
    two = _eeprom(nic_conf0=0xFF22)
    assert (two.tx_chain_num, two.rx_chain_num) == (2, 2)


def test_config_ant_2t2r_selects_second_chain():
    """config_ant is chain-gated off the EEPROM: 2T2R → BBP1 TX_ANTENNA=2, BBP3
    RX_ANTENNA=1 (vs the reference 1T1R's 0/0) [SRC rt2800lib.c:2322-2398]."""
    t = RecordingTransport()
    chan.config_ant(t, REV_F, _eeprom(nic_conf0=0xFF22))
    assert get_field(t.bbp[1], C.BBP1_TX_ANTENNA) == 2
    assert get_field(t.bbp[3], C.BBP3_RX_ANTENNA) == 1

    t2 = RecordingTransport()
    chan.config_ant(t2, REV_F, _eeprom(nic_conf0=0xFF11))   # reference 1T1R
    assert get_field(t2.bbp[1], C.BBP1_TX_ANTENNA) == 0
    assert get_field(t2.bbp[3], C.BBP3_RX_ANTENNA) == 0


# ---------------------------------------------------------------------------
# config_channel_rf53xx BT-coex arm — ported, not fail-loud [SRC rt2800lib.c:3431-3482]
# ---------------------------------------------------------------------------

def _tune_rf53xx(t, chip, ev, channel):
    chan.config_channel_rf53xx(t, chip, ev, C.RF_VALS_3X_2G[channel], 0, 0, channel)


def _r55_r59(writes):
    """Last RFCSR55 / RFCSR59 values written (None if absent)."""
    r55 = next((v for n, v in reversed(writes) if n == 55), None)
    r59 = next((v for n, v in reversed(writes) if n == 59), None)
    return r55, r59


def test_config_channel_rf53xx_non_bt_rev_f_reference_arm():
    """Reference: non-BT + rev-F → RFCSR55 + RFCSR59 from the _rev tables."""
    t = RecordingTransport()
    _tune_rf53xx(t, REV_F, _eeprom(nic_conf1=0x0000), 6)
    assert _r55_r59(t.rfcsr_writes) == (C.RF55_NON_BT_REV[5], C.RF59_NON_BT_REV[5])


def test_config_channel_rf53xx_bt_coex_rev_f_writes_bt_tables_not_raise():
    """A bt_coexist EEPROM used to raise NotImplementedError; now it tunes with the
    BT _rev tables (rev-F → RFCSR55 + RFCSR59)."""
    t = RecordingTransport()
    ev = _eeprom(nic_conf1=C.EEPROM_NIC_CONF1_BT_COEXIST)
    assert ev.bt_coexist is True
    for ch in range(1, 15):
        t.rfcsr_writes.clear()
        _tune_rf53xx(t, REV_F, ev, ch)
        assert _r55_r59(t.rfcsr_writes) == (C.RF55_BT_REV[ch - 1], C.RF59_BT_REV[ch - 1])


def test_config_channel_rf53xx_bt_coex_pre_f_writes_only_rfcsr59():
    """pre-REV_RT5390F BT arm writes only RFCSR59 (no RFCSR55)."""
    t = RecordingTransport()
    ev = _eeprom(nic_conf1=C.EEPROM_NIC_CONF1_BT_COEXIST)
    _tune_rf53xx(t, REV_PRE_F, ev, 3)
    r55, r59 = _r55_r59(t.rfcsr_writes)
    assert r55 is None and r59 == C.RF59_BT[2]


def test_config_channel_rf53xx_bt_coex_forces_pa_pe_g0():
    """A BT-combo card forces TX_PIN_CFG PA_PE_G0_EN on; on 2.4 GHz this equals the
    non-BT value (is_g=1) so full config_channel stays byte-identical for the reference."""
    bt = RecordingTransport()
    chan.config_channel(bt, REV_F, _eeprom(nic_conf1=C.EEPROM_NIC_CONF1_BT_COEXIST), 6, 8)
    non_bt = RecordingTransport()
    chan.config_channel(non_bt, REV_F, _eeprom(nic_conf1=0x0000), 6, 8)
    bt_pin = dict(bt.reg_writes)[C.TX_PIN_CFG]
    non_bt_pin = dict(non_bt.reg_writes)[C.TX_PIN_CFG]
    assert get_field(bt_pin, C.TX_PIN_CFG_PA_PE_G0_EN) == 1
    assert bt_pin == non_bt_pin        # identical on 2.4 GHz

