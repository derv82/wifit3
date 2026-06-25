"""M2c-5: the LED GPIO output config (OE_OUT driver, MUX3 mux, inverted set) for pin 15."""
import struct

from wifit3.chips.ar9271_v2 import gpio, hw
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import WMI


class FakeDev:
    def __init__(self):
        self.cmds = []

    def write(self, ep, data, timeout=None):
        cmd_id, seq = struct.unpack_from(">HH", data, 8)
        self.cmds.append((cmd_id, data[12:]))
        self._resp = struct.pack(">BBH", 1, 0, 8) + b"\x00\x00\x00\x00" + \
            struct.pack(">HH", cmd_id, seq) + b"\x00\x00\x00\x00"
        return len(data)

    def read(self, ep, length, timeout=None):
        return bytearray(self._resp)


def test_led_init_rmw_sequence():
    dev = FakeDev()
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    gpio.led_init(h)
    RMW = 0x0020

    def rmw(reg, s, c):
        return (RMW, struct.pack(">III", reg, s, c))

    # pin 15: OE_OUT driver-all at shift 30, MUX3 AS_OUTPUT at shift 15, set_gpio(1) inverted->0.
    assert [(c, b) for c, b in dev.cmds] == [
        rmw(0x404c, 0x3 << 30, 0x3 << 30),     # AR_GPIO_OE_OUT
        rmw(0x4068, 0 << 15, 0x1f << 15),      # AR_GPIO_OUTPUT_MUX3, AS_OUTPUT=0
        rmw(0x4048, 0 << 15, 1 << 15),         # AR_GPIO_IN_OUT, inverted value=0
    ]
