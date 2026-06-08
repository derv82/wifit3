"""Hardware-free regression for the RTL8188EUS (DKMS) monitor-mode entry.

Locks the RX-BAR enable (init_hw_mlme_ext), the MSR NOLINK RMW, the monitor RCR value, and
the single RXFLTMAP2 open. These vendor ops are byte-diffed against the wire by verify_pcap.
"""
from wifit3.chips.rtl8188eus_dkms import monitor


class Tx:
    def __init__(self, reads):
        self._reads = dict(reads)
        self.w8, self.w16, self.w32 = [], [], []

    def read8(self, a):
        return self._reads.get(a, 0) & 0xFF

    def read16(self, a):
        return self._reads.get(a, 0) & 0xFFFF

    def read32(self, a):
        return self._reads.get(a, 0)

    def write8(self, a, v):
        self.w8.append((a, v & 0xFF))

    def write16(self, a, v):
        self.w16.append((a, v & 0xFFFF))

    def write32(self, a, v):
        self.w32.append((a, v & 0xFFFFFFFF))


def test_enable_rx_bar():
    # HW_VAR_ENABLE_RX_BAR: RXFLTMAP1 |= BIT(8). Wire pre-state RXFLTMAP1=0x0000.
    t = Tx({0x06A2: 0x0000})
    monitor.enable_rx_bar(t)
    assert t.w16 == [(0x06A2, 0x0100)]               # BIT(8) set, nothing else
    assert t.w8 == [] and t.w32 == []


def test_enter_monitor_writes():
    # Wire pre-state: MSR=0x02 (NT_LINK_AP), RCR=0x700060ce (STA init).
    t = Tx({0x0102: 0x02, 0x0608: 0x700060CE})
    monitor.enter_monitor(t)
    assert t.w8 == [(0x0102, 0x00)]                  # MSR NOLINK (0x02 & 0x0C = 0)
    assert t.w32 == [(0x0608, 0x9000382F)]           # monitor RCR
    # Only RXFLTMAP2 (data subtypes) — RXFLTMAP0 stays at reset, RXFLTMAP1 set by RX-BAR.
    assert t.w16 == [(0x06A4, 0xFFFF)]


def test_msr_keeps_port1_nettype():
    # Port1 net-type [3:2] is preserved; only port0 [1:0] is rewritten to NOLINK.
    t = Tx({0x0102: 0x0E, 0x0608: 0x700060CE})       # 0x0E = port1=0b11, port0=0b10
    monitor.enter_monitor(t)
    assert t.w8 == [(0x0102, 0x0C)]                  # keep [3:2], clear [1:0]
