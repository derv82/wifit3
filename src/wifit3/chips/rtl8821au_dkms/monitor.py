"""RTL8821AU (DKMS) always-monitor entry — vendor monitor opmode only.

wifit3 is always-monitor; it never runs airmon's STA->monitor dance. This emits
just the vendor monitor opmode entry, hw_var_set_opmode(_HW_STATE_MONITOR_)
[SRC] rtl8812a_hal_init.c:3710 -> Set_MSR(NOLINK) + hw_var_set_monitor (:3663).
Verified out-of-line against the cold-boot wire (anchored on the RCR write), like
the 8814au sibling's verify_monitor_block — the ~120 airmon STA-mode ops the
capture shows before it are intentionally not replayed.

8821a divergence vs 8814au: the monitor RCR is 0x9000382F — it CLEARS RCR_ACRC32 |
RCR_AICV ([SRC] :3689, "CRC/ICV packets dropped in recvbuf2recvframe"), so unlike
8814's 0x90003B2F it does not let CRC/ICV-error frames into the FIFO.
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
REG_MAC_ADDR = 0x0610           # REG_MACID — the card's own 6-byte address


def _set_msr(t, net_type: int) -> None:
    # Set_MSR(HW_PORT0): keep port1 net-type [3:2], rewrite port0 [1:0] to NOLINK.
    t.write8(MSR, (t.read8(MSR) & MSR_NETTYPE_MASK) | net_type)


def _hw_var_set_monitor(t) -> None:
    # Backup reads kept only to match the wire (wifit3 never leaves monitor), then
    # program the monitor RCR + open all three RX filter maps. Wire order: RCR
    # read+write, then RXFLTMAP0/1/2 read x3 then write x3.
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


def _write_mac_addr(t, mac6) -> None:
    """[SRC] SetHwReg(HW_VAR_MAC_ADDR) — REG_MACID 0x610-0x615. The cold monitor path never
    sets it; active-monitor re-points it so the hardware HW-ACKs frames to ``mac6``."""
    for i, b in enumerate(mac6):
        t.write8(REG_MAC_ADDR + i, b)
