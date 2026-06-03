"""Hardware-free regression for the M3b-2 monitor opmode entry.

The targeted byte-for-byte check vs the cold-boot capture lives in
`scripts/rtl8814au_dkms/verify_pcap.py` (verify_monitor_block); this pins the
monitor RCR value and the accept-all RX filter maps.
"""
from wifit3.chips.rtl8814au_dkms import monitor


class Rec:
    def __init__(self, reads=None):
        self.ops = []
        self.reads = reads or {}

    def _r(self, a):
        self.ops.append(("R", a))
        return self.reads.get(a, 0)

    def read8(self, a):
        return self._r(a)

    def read16(self, a):
        return self._r(a)

    def read32(self, a):
        return self._r(a)

    def write8(self, a, v):
        self.ops.append(("W", a, v))

    def write16(self, a, v):
        self.ops.append(("W", a, v))

    def write32(self, a, v):
        self.ops.append(("W", a, v))


def test_enter_monitor_sets_msr_nolink_and_accept_all():
    # hal_init left MSR at NT_LINK_AP (0x02); STA RCR + beacon-filtered RXFLTMAP.
    rec = Rec(reads={0x102: 0x02, 0x608: 0xF40060CE,
                     0x6A0: 0x0000, 0x6A2: 0x0520, 0x6A4: 0x0000})
    monitor.enter_monitor(rec)
    w = {a: v for k, a, v in (o for o in rec.ops if o[0] == "W")}
    assert w[0x102] == 0x00              # Set_MSR(NOLINK): net-type [1:0] -> 0
    assert w[0x608] == 0x90003B2F        # monitor RCR (accept-all incl. CRC/ICV)
    assert w[0x6A0] == 0xFFFF            # RXFLTMAP0 (data)
    assert w[0x6A2] == 0xFFFF            # RXFLTMAP1 (mgmt)
    assert w[0x6A4] == 0xFFFF            # RXFLTMAP2 (ctrl)


def test_set_msr_preserves_port1_nettype():
    # Set_MSR keeps [3:2] (port1) and rewrites [1:0]; e.g. 0x0e & 0x0c = 0x0c.
    rec = Rec(reads={0x102: 0x0E})
    monitor._set_msr(rec, monitor.C.MSR_NOLINK)
    w = {a: v for k, a, v in (o for o in rec.ops if o[0] == "W")}
    assert w[0x102] == 0x0C
