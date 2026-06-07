"""RTL8188EUS monitor-mode entry — vendor opmode entry + the always-monitor deviation.

The cold-boot capture was taken under airmon-ng, which drives the STA-initialised vendor
driver into monitor through a chain of cfg80211 ioctls. wifit3 is *always* monitor, so it
runs only the vendor monitor opmode entry [SRC] rtl8188e_hal_init.c:3516
``hw_var_set_opmode(_HW_STATE_MONITOR_)``:

    Set_MSR(_HW_STATE_NOLINK_)          net-type -> NOLINK
    hw_var_set_monitor()                [SRC] rtl8188e_hal_init.c:3476

On the wire [WIRE] cap1 ops 1957-1961 that is exactly: MSR read/write, RCR read(backup)/write
(0x9000382f), RXFLTMAP2 write (0xffff).

DEVIATION (RX breadth): the vendor's ``hw_var_set_monitor`` opens only RXFLTMAP2 (data
frames) and leaves the mgmt/ctrl filters at their state from airmon's STA-mode dance — which
wifit3 never runs (hal_init even leaves RXFLTMAP0 unwritten, [SRC] usb_halinit.c:628). So
beacons (mgmt) and control frames would be filtered out. To capture them, wifit3 additionally
opens RXFLTMAP0 and RXFLTMAP1 to accept-all. Those two writes are NOT on the cold-boot wire;
the byte-for-byte monitor diff covers only the 5 vendor ops. [[monitor_mode_deviation]]
"""
from __future__ import annotations

from . import constants as C


def _set_msr(t, net_type: int) -> None:
    """``rtw_hal_set_msr(HW_PORT0)`` [SRC] hal_com.c:3080 — MSR[1:0] net-type, keeping
    port1's [3:2]."""
    v = t.read8(C.MSR)
    t.write8(C.MSR, (v & C.MSR_NETTYPE_MASK) | net_type)


def _hw_var_set_monitor(t) -> None:
    """``hw_var_set_monitor`` [SRC] rtl8188e_hal_init.c:3476 — back up + set the monitor
    RCR (accept-all, append FCS) and open RXFLTMAP2; then (wifit3 deviation) open
    RXFLTMAP0/1 so monitor RX also gets mgmt (beacons) + control frames."""
    t.read32(C.REG_RCR)                                       # rcr_backup
    t.write32(C.REG_RCR, C.RCR_MONITOR_VALUE)
    t.write16(C.REG_RXFLTMAP2, C.RXFLTMAP_ACCEPT_ALL)        # vendor (data)
    t.write16(C.REG_RXFLTMAP0, C.RXFLTMAP_ACCEPT_ALL)        # wifit3 (mgmt/beacons)
    t.write16(C.REG_RXFLTMAP1, C.RXFLTMAP_ACCEPT_ALL)        # wifit3 (control)


def enter_monitor(t) -> None:
    """``hw_var_set_opmode(_HW_STATE_MONITOR_)`` — the always-monitor entry."""
    _set_msr(t, C.MSR_NOLINK)
    _hw_var_set_monitor(t)
