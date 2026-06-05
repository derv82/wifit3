"""RTL8812AU always-monitor entry — vendor monitor opmode (jaguar-standard).

wifit3 is always-monitor; it never runs airmon's STA->monitor dance. This emits the
vendor monitor opmode entry, hw_var_set_opmode(_HW_STATE_MONITOR_) -> Set_MSR(NOLINK) +
hw_var_set_monitor: program the accept-all RCR + open the three RX filter maps. The RCR
value is the jaguar monitor RCR (same register layout as the 8821au sibling): accept-all
+ APP_PHYST + APPFCS, with ACRC32 | AICV cleared so CRC/ICV-error frames are dropped in
recvbuf2recvframe (the base RX walk also skips them defensively). The STA-mode RCR the
MAC init wrote is replaced here.
"""
from __future__ import annotations

MSR = 0x0102                    # REG_CR[17:16] net-type (port0 low 2 bits)
MSR_NETTYPE_MASK = 0x0C         # preserve port1 [3:2]
MSR_NOLINK = 0x00
REG_RCR = 0x0608
RCR_MONITOR_VALUE = 0x9000382F  # accept-all + APP_PHYST + APPFCS; ACRC32/AICV cleared
REG_RXFLTMAP0 = 0x06A0
REG_RXFLTMAP1 = 0x06A2
REG_RXFLTMAP2 = 0x06A4
RXFLTMAP_ACCEPT_ALL = 0xFFFF


def _set_msr(t, net_type: int) -> None:
    # Set_MSR(HW_PORT0): keep port1 net-type [3:2], rewrite port0 [1:0] to NOLINK.
    t.write8(MSR, (t.read8(MSR) & MSR_NETTYPE_MASK) | net_type)


def _hw_var_set_monitor(t) -> None:
    # Program the monitor RCR + open all three RX filter maps (accept all frame types).
    t.read32(REG_RCR)
    t.write32(REG_RCR, RCR_MONITOR_VALUE)
    t.read16(REG_RXFLTMAP0)
    t.read16(REG_RXFLTMAP1)
    t.read16(REG_RXFLTMAP2)
    t.write16(REG_RXFLTMAP0, RXFLTMAP_ACCEPT_ALL)
    t.write16(REG_RXFLTMAP1, RXFLTMAP_ACCEPT_ALL)
    t.write16(REG_RXFLTMAP2, RXFLTMAP_ACCEPT_ALL)


def enter_monitor(t) -> None:
    _set_msr(t, MSR_NOLINK)
    _hw_var_set_monitor(t)
