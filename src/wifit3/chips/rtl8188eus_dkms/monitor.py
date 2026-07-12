"""RTL8188EUS monitor-mode entry — the vendor register writes that put the chip in
always-monitor RX.

We don't run airmon-ng; the chip only ever sees register writes, so this reproduces the
vendor-driver writes airmon *triggers* when it sets the interface to monitor. Two vendor
functions, in wire order [WIRE] cap1 ops 2452-2507:

  init_hw_mlme_ext()                  [SRC] rtw_mlme_ext.c:1554
    HW_VAR_ENABLE_RX_BAR            -> RXFLTMAP1 |= BIT(8)  (enable_rx_bar, below)
    set_channel_bwmode()           -> the channel tune (driver owns this separately)
  hw_var_set_opmode(_HW_STATE_MONITOR_)   [SRC] rtl8188e_hal_init.c:3516
    Set_MSR(_HW_STATE_NOLINK_)       net-type -> NOLINK
    hw_var_set_monitor()           [SRC] rtl8188e_hal_init.c:3476

On the wire that is: RXFLTMAP1 read/|=BIT8, [channel tune], MSR read/write, RCR
read(backup)/write(0x9000382f), RXFLTMAP2 write(0xffff).

The vendor opens exactly three RX-filter words: RXFLTMAP1 only for BlockAckReq (BIT8, via
HW_VAR_ENABLE_RX_BAR), RXFLTMAP2 accept-all (data) in hw_var_set_monitor, and RXFLTMAP0 is
left at its reset state (hal_init leaves it unwritten [SRC] usb_halinit.c:624). Beacons
(mgmt) still reach RX because RCR is accept-all-physical (0x9000382f); the per-subtype
RXFLTMAP gate is only consulted for the subtypes the chip defaults to filtering. We write
neither RXFLTMAP0 nor an accept-all RXFLTMAP1 — a write the vendor never made is as much a
divergence as one missed.
"""
from __future__ import annotations

from . import constants as C


def enable_rx_bar(t) -> None:
    """``init_hw_mlme_ext`` -> ``HW_VAR_ENABLE_RX_BAR`` (enable) [SRC] hal_com.c:10257 —
    RXFLTMAP1 |= BIT(8): accept BlockAckReq control frames. Runs before the channel tune."""
    val16 = t.read16(C.REG_RXFLTMAP1)
    t.write16(C.REG_RXFLTMAP1, val16 | C.RXFLTMAP1_RX_BAR)


def _set_msr(t, net_type: int) -> None:
    """``rtw_hal_set_msr(HW_PORT0)`` [SRC] hal_com.c:3080 — MSR[1:0] net-type, keeping
    port1's [3:2]."""
    v = t.read8(C.MSR)
    t.write8(C.MSR, (v & C.MSR_NETTYPE_MASK) | net_type)


def _hw_var_set_monitor(t) -> None:
    """``hw_var_set_monitor`` [SRC] rtl8188e_hal_init.c:3476 — back up + set the monitor
    RCR (accept-all-physical, append FCS) and open RXFLTMAP2 (data subtypes). RXFLTMAP0/1
    are not touched here (RX-BAR already set RXFLTMAP1's one bit; RXFLTMAP0 stays at reset)."""
    t.read32(C.REG_RCR)                                       # rcr_backup
    t.write32(C.REG_RCR, C.RCR_MONITOR_VALUE)
    t.write16(C.REG_RXFLTMAP2, C.RXFLTMAP_ACCEPT_ALL)        # data subtypes


def enter_monitor(t) -> None:
    """``hw_var_set_opmode(_HW_STATE_MONITOR_)`` — net-type NOLINK + the monitor RCR/RXFLTMAP2."""
    _set_msr(t, C.MSR_NOLINK)
    _hw_var_set_monitor(t)


def admit_ack_frames(t) -> None:
    """RXFLTMAP1 |= BIT(13): let RX see the AP's ACKs to our injects. Off by default."""
    t.write16(C.REG_RXFLTMAP1, t.read16(C.REG_RXFLTMAP1) | C.RXFLTMAP1_ACK)


def drop_ack_frames(t) -> None:
    """Clear RXFLTMAP1 BIT(13) — restore the default monitor ctrl filter."""
    t.write16(C.REG_RXFLTMAP1, t.read16(C.REG_RXFLTMAP1) & ~C.RXFLTMAP1_ACK)
