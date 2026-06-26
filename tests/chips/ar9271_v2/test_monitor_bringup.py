"""M7: monitor-mode bring-up — promiscuous configure_filter + WMI VAP/NODE create."""
import struct

from wifit3.chips.ar9271_v2 import hw, rx
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import WMI, HTC_M_MONITOR

MAC = bytes.fromhex("c01c304f78b0")


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


def _hw(dev):
    return hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))


def test_monitor_filter_value():
    dev = FakeDev()
    h = _hw(dev)
    h.is_monitoring = False                  # PROM not yet set at this point on the wire
    flags = rx.FilterFlags(control=True, pspoll=True, bcn_prbresp_promisc=True, other_bss=True)
    assert rx.calcrxfilter(h, flags) == 0xC01F


def test_station_filter_default():
    h = _hw(FakeDev())
    assert rx.calcrxfilter(h) == 0x207       # ucast|bcast|mcast|mybeacon


def test_vap_create_payload():
    dev = FakeDev()
    _hw(dev).wmi.vap_create(0, HTC_M_MONITOR, MAC)
    cmd, body = dev.cmds[-1]
    assert cmd == 0x13
    assert body == bytes.fromhex("0008") + MAC + bytes.fromhex("00000000")


def test_node_create_payload():
    dev = FakeDev()
    _hw(dev).wmi.node_create(MAC, b"\x00" * 6, 0, 0, 1, 0xFFFF)
    cmd, body = dev.cmds[-1]
    assert cmd == 0x10
    assert body == MAC + b"\x00" * 6 + bytes.fromhex("000001") + bytes.fromhex("00000000ffff00")
