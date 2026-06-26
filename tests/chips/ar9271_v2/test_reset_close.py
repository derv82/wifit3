"""M4: ath9k_hw_reset close-out — saved LED + 32 kHz clock, AR9271 USB descriptor byte-swap.

restore_chainmask, gen_timer_start_tsf2, and apply_gpio_override are no-ops on the STA cold
bring-up (no ar9002 restore_chainmask op, no tsf2 timer, gpio_mask 0), so the tail is two
register writes.
"""
import struct

import pytest

from wifit3.chips.ar9271_v2 import hw, reg as R
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


def _run(save_led=0, gpio_mask=0, tsf2=False):
    dev = FakeDev()
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    h.saveLedState = save_led
    h.gpio_mask = gpio_mask
    h.tsf2_enabled = tsf2
    h.reset_tail()
    return dev


def _write_list(dev):
    out = []
    for c, b in dev.cmds:
        if c == 0x15:
            out += [struct.unpack_from(">II", b, k) for k in range(0, len(b), 8)]
    return out


def test_led_and_descriptor_writes():
    writes = _write_list(_run())
    # cold: saveLedState 0 -> just the 32 kHz sleep clock; then the AR9271 byte-swap.
    assert (R.AR_CFG_LED, R.AR_CFG_SCLK_32KHZ) in writes
    assert (R.AR_CFG, R.AR_CFG_SWRB | R.AR_CFG_SWTB) in writes


def test_led_preserves_saved_state():
    writes = _write_list(_run(save_led=0x80))
    assert (R.AR_CFG_LED, 0x80 | R.AR_CFG_SCLK_32KHZ) in writes


def test_tail_emits_no_rmws_on_sta_path():
    dev = _run()
    assert all(c != 0x20 for c, _ in dev.cmds)


def test_gpio_override_unported_guard():
    with pytest.raises(NotImplementedError):
        _run(gpio_mask=0x4)
