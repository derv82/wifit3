"""RTL8812AU always-monitor entry: the rtw_hal_init tail, then the vendor monitor opmode.

hw_var_set_opmode(_HW_STATE_MONITOR_) (rtl8812a_hal_init.c:3720-3727): Set_MSR(NOLINK) +
hw_var_set_monitor, the radiotap monitor RCR and the three RX filter maps opened.
"""
from __future__ import annotations

MSR = 0x0102                    # REG_CR[17:16] net-type (port0 low 2 bits)
MSR_NETTYPE_MASK = 0x0C         # preserve port1 [3:2]
MSR_NOLINK = 0x00
REG_RCR = 0x0608
RCR_AAP = 1 << 0                # hal_com_reg.h:1145
RCR_APP_PHYST_RXFF = 1 << 28    # hal_com_reg.h:1117
RCR_APPFCS = 1 << 31            # hal_com_reg.h:1114
REG_RXFLTMAP0 = 0x06A0
REG_RXFLTMAP1 = 0x06A2
REG_RXFLTMAP2 = 0x06A4
RXFLTMAP_ACCEPT_ALL = 0xFFFF
REG_USB_HRPWM = 0xFE58          # hal_com_reg.h:545
REG_FWHW_TXQ_CTRL = 0x0420
REG_MAC_ADDR = 0x0610           # REG_MACID — the card's own 6-byte address


def _set_msr(t, net_type: int) -> None:
    # Set_MSR(HW_PORT0): keep port1 net-type [3:2], rewrite port0 [1:0].
    t.write8(MSR, (t.read8(MSR) & MSR_NETTYPE_MASK) | net_type)


def _hw_var_set_monitor(t) -> None:
    # rtl8812a_hal_init.c:3663-3692: RCR backup + set (ARPHRD_IEEE80211_RADIOTAP appends FCS),
    # then RXFLTMAP0-2 backup reads + 0xFFFF writes.
    t.read32(REG_RCR)
    t.write32(REG_RCR, RCR_AAP | RCR_APP_PHYST_RXFF | RCR_APPFCS)
    t.read16(REG_RXFLTMAP0)
    t.read16(REG_RXFLTMAP1)
    t.read16(REG_RXFLTMAP2)
    t.write16(REG_RXFLTMAP0, RXFLTMAP_ACCEPT_ALL)
    t.write16(REG_RXFLTMAP1, RXFLTMAP_ACCEPT_ALL)
    t.write16(REG_RXFLTMAP2, RXFLTMAP_ACCEPT_ALL)


def enter_monitor(t) -> None:
    _set_msr(t, MSR_NOLINK)
    _hw_var_set_monitor(t)


# RXFLTMAP1 bit13 admits ACK control frames so RX can see the AP's ACKs to us (off by default).
# Scoped to bit13 — accept-all here cost RX on rtl8188eus.
RXFLTMAP1_ACK = 1 << 13


def admit_ack_frames(t) -> None:
    t.write16(REG_RXFLTMAP1, t.read16(REG_RXFLTMAP1) | RXFLTMAP1_ACK)


def drop_ack_frames(t) -> None:
    t.write16(REG_RXFLTMAP1, t.read16(REG_RXFLTMAP1) & ~RXFLTMAP1_ACK)


def _write_mac_addr(t, mac6) -> None:
    """[SRC] SetHwReg(HW_VAR_MAC_ADDR) — REG_MACID 0x610-0x615."""
    for i, b in enumerate(mac6):
        t.write8(REG_MAC_ADDR + i, b)


def set_monitor_mode(t, channel, params) -> None:
    """rtl8812au_hal_init's MISC31 tail, rtw_hal_init's tail (hal_intf.c:608-620), then enter_monitor.

    A monitor iface makes no station opmode run: rtw_hal_init_opmode returns early (hal_intf.c:494-515).
    """
    from . import chan, txpower
    mac = [int(x, 16) for x in (params.mac_address or "00:00:00:00:00:00").split(":")]
    t.write8(REG_USB_HRPWM, 0x00)                                            # usb_halinit.c:1710
    t.write32(REG_FWHW_TXQ_CTRL, t.read32(REG_FWHW_TXQ_CTRL) | (1 << 12))    # CONFIG_XMIT_ACK, :1714
    _write_mac_addr(t, mac)                                                  # rtw_mi_set_mac_addr
    # init_hw_mlme_ext (rtw_mlme_ext.c:1299): HW_VAR_ENABLE_RX_BAR, then set_channel_bwmode.
    t.write16(REG_RXFLTMAP1, t.read16(REG_RXFLTMAP1) | (1 << 8))
    chan.set_channel_bw(t, channel, bb_swing_2g_a=params.bb_swing_2g[0],
                        bb_swing_2g_b=params.bb_swing_2g[1], rfe_type=params.rfe_type,
                        is_c_cut=params.is_c_cut)
    txpower.set_tx_power(t, channel, params.tx_power_2g)
    enter_monitor(t)
