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


# --- morrownr/airmon RX-START tail (the monitor opmode + nl80211 set-channel) -------------
REG_USB_RPWM = 0xFE58           # USB RPWM (power-mode request); 0 = wake / no power-save
REG_FWHW_TXQ_CTRL = 0x0420
REG_MAC_ADDR = 0x0610           # REG_MACID — the card's own 6-byte address
REG_BCN_CTRL = 0x0550
REG_TBTT_PROHIBIT = 0x0540
REG_RCR = 0x0608
RCR_MONITOR_AIRMON = 0x90000001  # morrownr's monitor RCR: AAP|APP_PHYST|APPFCS (RXFLTMAP does the rest)
NT_LINK_AP = 0x02


def _write_mac_addr(t, mac6) -> None:
    """[SRC] SetHwReg(HW_VAR_MAC_ADDR) — REG_MACID 0x610-0x615."""
    for i, b in enumerate(mac6):
        t.write8(REG_MAC_ADDR + i, b)


def set_monitor_mode(t, channel, params) -> None:
    """Byte-for-byte reproduction of morrownr/airmon's RX-START tail: enter monitor opmode
    and (nl80211) set the channel. The channel set is just wifit3's own ``set_channel_bw`` +
    ``set_tx_power`` re-run (the 8812a tunes once during bring-up, so this is redundant
    config) bracketed by the opmode writes -- MAC-addr, RXFLTMAP, beacon-related regs,
    MSR->NOLINK, and the monitor RCR 0x90000001.

    wifit3 is always-monitor and could skip this airmon dance and keep its direct
    ``enter_monitor`` -- BUT that is unproven while RX is still garbage, so it is reproduced
    here to match morrownr's exact RX-time chip state. Revisit once RX is confirmed.
    """
    from . import chan, txpower
    mac = [int(x, 16) for x in (params.mac_address or "00:00:00:00:00:00").split(":")]

    # enter monitor (pre-channel): wake RPWM, FWHW_TXQ bit12, MAC addr, RXFLTMAP1 bit8
    t.write8(REG_USB_RPWM, 0x00)
    t.write32(REG_FWHW_TXQ_CTRL, t.read32(REG_FWHW_TXQ_CTRL) | (1 << 12))
    _write_mac_addr(t, mac)
    t.write16(REG_RXFLTMAP1, t.read16(REG_RXFLTMAP1) | (1 << 8))

    # set channel (airmon's nl80211 set-channel == our runtime tune, re-run)
    chan.set_channel_bw(t, channel, bb_swing_2g_a=params.bb_swing_2g[0],
                        bb_swing_2g_b=params.bb_swing_2g[1], rfe_type=params.rfe_type,
                        is_c_cut=params.is_c_cut)
    txpower.set_tx_power(t, channel, params.tx_power_2g)

    # opmode tail: MAC addr, beacon-related regs, MSR->NOLINK, monitor RCR
    _write_mac_addr(t, mac)
    t.read8(REG_BCN_CTRL)
    _set_msr(t, NT_LINK_AP)                                           # disconnect-path Set_MSR
    t.write8(REG_FWHW_TXQ_CTRL + 2, t.read8(REG_FWHW_TXQ_CTRL + 2) & ~0x40)
    t.write8(REG_TBTT_PROHIBIT + 1, 0x64)
    t.write8(REG_TBTT_PROHIBIT + 2, t.read8(REG_TBTT_PROHIBIT + 2) & 0xF0)
    t.write8(REG_BCN_CTRL, 0x19)
    _set_msr(t, MSR_NOLINK)
    t.read32(REG_RCR)
    t.write32(REG_RCR, RCR_MONITOR_AIRMON)
