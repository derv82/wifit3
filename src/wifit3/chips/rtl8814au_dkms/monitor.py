"""RTL8814AU monitor-mode entry (M3b-2) — vendor faithful, with a deliberate
deviation from the cold-boot pcap.

DIVERGENCE FROM THE PCAP / AIRMON (read this before comparing to the capture):
The cold-boot capture was taken under airmon-ng, which drives the *STA-initialised*
vendor driver into monitor mode through a sequence of cfg80211 ioctls. On the wire
[WIRE] cap1 ops 4451-4764 that looks like: enable beacon RX (RXFLTMAP1 0x420->0x520),
re-tune the channel that hal_init already tuned, re-program the MAC that was already
written, tear down the STA/AP beacon function (StopTxBeacon + BCN_CTRL), set opmode
NOLINK, and finally enter monitor. wifit3 is *always* monitor — it never enters STA
mode — so it does NOT replay airmon's STA-mode dance. It runs only the vendor's
actual monitor opmode entry, which is the last 10 ops of that wire block
(4755-4764) and maps to exactly one vendor function:

    hw_var_set_opmode(_HW_STATE_MONITOR_)   [SRC rtl8814a_hal_init.c:3222]
        Set_MSR(_HW_STATE_NOLINK_)          net-type -> NOLINK
        hw_var_set_monitor()                [SRC rtl8814a_hal_init.c:3155]

Because of this deviation the contiguous byte-for-byte differ stops at M3b-1; this
block is instead verified as a *targeted* 10-op diff against wire 4755-4764 (the
~300 skipped ops in between are airmon's STA-mode artifacts). The monitor RCR /
RXFLTMAP *values* are taken straight from that wire — they are what airmon's working
monitor session programmed.
"""
from __future__ import annotations

from . import constants as C


def _set_msr(t, net_type: int) -> None:
    """[SRC] Set_MSR / rtw_hal_set_msr(HW_PORT0) — REG_CR[17:16] net-type.

    Keeps port1's net-type [3:2], rewrites port0's [1:0]. hal_init left this at
    NT_LINK_AP; monitor needs NOLINK. [WIRE] op 4755-4756 (0x02 -> 0x00).
    """
    v = t.read8(C.MSR)
    t.write8(C.MSR, (v & C.MSR_NETTYPE_MASK) | net_type)


def _hw_var_set_monitor(t) -> None:
    """[SRC] hw_var_set_monitor — RCR + RXFLTMAP0/1/2 accept-all.

    Backs up the current RCR + the three RX filter maps (the vendor restores them
    when leaving monitor; wifit3 never leaves, so the reads are kept only to match
    the wire), then programs the monitor RCR (accept-all incl. CRC/ICV errors,
    append FCS) and opens all three RX filter maps. [WIRE] op 4757-4764.
    """
    t.read32(C.REG_RCR)            # rcr_backup
    t.read16(C.REG_RXFLTMAP0)      # rxfltmap0_backup
    t.read16(C.REG_RXFLTMAP1)      # rxfltmap1_backup
    t.read16(C.REG_RXFLTMAP2)      # rxfltmap2_backup
    t.write32(C.REG_RCR, C.RCR_MONITOR_VALUE)
    t.write16(C.REG_RXFLTMAP0, C.RXFLTMAP_ACCEPT_ALL)
    t.write16(C.REG_RXFLTMAP1, C.RXFLTMAP_ACCEPT_ALL)
    t.write16(C.REG_RXFLTMAP2, C.RXFLTMAP_ACCEPT_ALL)


def enter_monitor(t) -> None:
    """[SRC] hw_var_set_opmode(_HW_STATE_MONITOR_) — the always-monitor entry.

    See the module docstring for why this is the vendor monitor opmode entry only,
    not airmon's full STA->monitor transition.
    """
    _set_msr(t, C.MSR_NOLINK)
    _hw_var_set_monitor(t)
