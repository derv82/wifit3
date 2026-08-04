"""RTL8814AU airmon STA->monitor entry — full dance reproduced.

The cold-boot capture was taken under airmon-ng, which brings the interface up as a
STA, sets the channel, then switches it to monitor. On the wire that is four vendor
steps, each a real driver function, reproduced here so the chip reaches monitor along
the *same path* the kernel takes (a shortcut to the endpoint can carry different chip
state and hide an RX bug):

  1. enable_rx_bar           init_hw_mlme_ext -> HW_VAR_ENABLE_RX_BAR [SRC hal_com.c:12384]
  2. (channel tune)          init_hw_mlme_ext -> set_channel_bwmode   (chan.set_channel_bw)
  3. set_sta_opmode          hw_var_set_opmode(_HW_STATE_STATION_)    [SRC rtl8814a_hal_init.c:3204]
  4. enter_monitor           hw_var_set_opmode(_HW_STATE_MONITOR_)    [SRC rtl8814a_hal_init.c:3222]

Step 2 is the bare channel tune (chan.set_channel_bw), sequenced by the caller; this
module owns steps 1, 3 and 4. [WIRE] cap1: RX-BAR op 4451, STA opmode 4740-4754,
monitor opmode 4755-4764.
"""
from __future__ import annotations

from . import constants as C


def enable_rx_bar(t) -> None:
    """[SRC] HW_VAR_ENABLE_RX_BAR — RXFLTMAP1 |= BIT8 (accept block-ack requests).

    init_hw_mlme_ext sets this when the interface comes up. [WIRE] op 4451 (0x420->0x520).
    """
    v = t.read16(C.REG_RXFLTMAP1)
    t.write16(C.REG_RXFLTMAP1, v | (1 << 8))


def _set_msr(t, net_type: int) -> None:
    """[SRC] Set_MSR / rtw_hal_set_msr(HW_PORT0) — REG_CR[17:16] net-type.

    Keeps port1's net-type [3:2], rewrites port0's [1:0].
    """
    v = t.read8(C.MSR)
    t.write8(C.MSR, (v & C.MSR_NETTYPE_MASK) | net_type)


def _set_macaddr(t, mac_address: str) -> None:
    """[SRC] HW_VAR_MAC_ADDR -> set_macaddr_port — write the 6-byte MAC to REG_MACID.

    hw_var_set_opmode rewrites the efuse MAC before setting the net-type. [WIRE] op
    4740-4745 (6 writes, no readback). The hal_init turn-on tail already wrote it once.
    """
    if not mac_address:
        raise ValueError("set_sta_opmode: no MAC address (efuse read failed?)")
    mac = bytes.fromhex(mac_address.replace(":", ""))
    for i in range(C.ETH_ALEN):
        t.write8(C.REG_MACID + i, mac[i])


def _disable_tsf_update(t) -> None:
    """[SRC] rtw_iface_disable_tsf_update -> rtw_hal_set_tsf_update(0).

    Reads REG_BCN_CTRL and sets DIS_TSF_UDT only if it is clear. The hal_init beacon
    setup already left DIS_TSF_UDT set, so this is a bare read (no write). [WIRE] op 4746.
    """
    v = t.read8(C.REG_BCN_CTRL)
    if not (v & C.DIS_TSF_UDT):
        t.write8(C.REG_BCN_CTRL, v | C.DIS_TSF_UDT)


def _stop_tx_beacon(t) -> None:
    """[SRC] StopTxBeacon [hal_com.c:14821] — clear the beacon-function bit, set hold time.

    [WIRE] op 4749-4753: 0x422 &= ~BIT6, 0x541 = STOP_BCN hold low byte, 0x542 high nibble.
    """
    v = t.read8(C.REG_FWHW_TXQ_CTRL + 2)
    t.write8(C.REG_FWHW_TXQ_CTRL + 2, v & ~(1 << 6))
    t.write8(C.REG_TBTT_PROHIBIT + 1, C.TBTT_PROHIBIT_HOLD_TIME_STOP_BCN & 0xFF)
    v = t.read8(C.REG_TBTT_PROHIBIT + 2)
    t.write8(C.REG_TBTT_PROHIBIT + 2,
             (v & 0xF0) | (C.TBTT_PROHIBIT_HOLD_TIME_STOP_BCN >> 8))


def set_sta_opmode(t, mac_address: str) -> None:
    """[SRC] hw_var_set_opmode(_HW_STATE_STATION_) — port0, non-concurrent path.

    Airmon brings the interface up as a STA before switching to monitor. Reproduces the
    driver's port0 opmode block: set MAC addr, disable TSF update, Set_MSR(STATION),
    StopTxBeacon, then REG_BCN_CTRL = DIS_TSF_UDT | EN_BCN_FUNCTION | DIS_ATIM. [WIRE] op
    4740-4754.
    """
    _set_macaddr(t, mac_address)                        # HW_VAR_MAC_ADDR
    _disable_tsf_update(t)                               # rtw_iface_disable_tsf_update
    _set_msr(t, C.MSR_STATION)                           # Set_MSR(_HW_STATE_STATION_)
    _stop_tx_beacon(t)                                   # StopTxBeacon
    t.write8(C.REG_BCN_CTRL, C.DIS_TSF_UDT | C.EN_BCN_FUNCTION | C.DIS_ATIM)


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
    """[SRC] hw_var_set_opmode(_HW_STATE_MONITOR_) — Set_MSR(NOLINK) + hw_var_set_monitor.

    The final airmon step: net-type to NOLINK then the monitor RCR/RXFLTMAP. [WIRE] op
    4755-4764.
    """
    _set_msr(t, C.MSR_NOLINK)
    _hw_var_set_monitor(t)
