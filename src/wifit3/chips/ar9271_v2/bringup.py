"""Cold bring-up + channel-change orchestration — the driver-owned init sequence.

This is the sequencing layer: it calls the leaf register routines (``hw``, ``phy``, ``calib``,
``rx``, ``mac_queue``, ``phy_board``, ``phy_power``, ``gpio``, ``wmi``) in the kernel's order to
take the chip from a freshly firmware-booted state to a monitor-mode receiver on ch1, and to
retune it (full ath9k_hw_reset or fast channel change) on a hop. All register I/O funnels through
the WMI channel's transport, so the same code drives live silicon and the pcap-replay gate — the
gate builds the driver over a ReplayDevice transport and calls ``driver.connect`` / ``set_channel``
(``scripts/ar9271_v2/verify_pcap.py``); nothing here knows which it is.

Ported op-for-op from ath9k_htc start + ath9k_htc_set_channel [SRC] htc_drv_main.c:225/916 +
ath9k_hw_reset / ath9k_hw_do_fastcc [SRC] hw.c:1859/1788. The byte-exact wire order is the
contract the gate enforces.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from . import ani, calib, chan as chanmod, constants as C, eeprom, gpio, htc, hw as hwmod
from . import key, mac_queue, phy, phy_board, phy_power, reg as R, rx
from .transport import AR9271Transport
from .wmi import (
    WMI, HTC_M_MONITOR, WMI_ATH_INIT_CMDID, WMI_DISABLE_INTR_CMDID, WMI_DRAIN_TXQ_ALL_CMDID,
    WMI_ENABLE_INTR_CMDID, WMI_FLUSH_RECV_CMDID, WMI_SET_MODE_CMDID, WMI_START_RECV_CMDID,
    WMI_STOP_RECV_CMDID)

_MODE_11NG = struct.pack(">H", 1)             # WMI_SET_MODE body: HTC_MODE_11NG


@dataclass
class BringupResult:
    """The persistent driver state the cold bring-up produces: the WMI channel and the AthHw it
    drives (both keyed on the live transport), plus the HTC service->endpoint map TX routing
    needs."""
    wmi: WMI
    hw: hwmod.AthHw
    endpoints: dict


def hw_reset_body(wmi: WMI, hw: hwmod.AthHw, chan: chanmod.Channel) -> None:
    """The body of ath9k_hw_reset shared by the cold bring-up and every channel-change reset:
    process_ini through the calibration and the reset tail [SRC] hw.c:1944-2066. The caller runs
    reset_begin first (its preamble differs between the cold and warm paths)."""
    phy.process_ini(hw, chan)
    phy.set_rfmode(hw, chan)
    hw.init_mfp()
    phy.set_delta_slope(hw, chan)
    phy.spur_mitigate(hw, chan)
    phy_board.set_board_values(hw, chan)
    hw.reset_opmode(hw.macStaId1, hw.saveDefAntenna)
    phy.rf_set_freq(hw, chan)
    if not hw.txq:
        mac_queue.init_tx_queues(hw)                # driver-side alloc (no wire ops)
    mac_queue.init_queues(hw)
    hw.init_interrupt_masks()
    hw.ani_cache_ini_regs()
    hw.init_qos()
    hw.init_global_settings(chan)
    hw.reset_dma_and_intr()
    phy.init_bb(hw, chan)
    calib.init_cal(hw, chan)
    hw.reset_tail()


def cold_bringup(t: AR9271Transport) -> BringupResult:
    """Take a freshly firmware-booted AR9271 to a monitor-mode receiver on ch1. The firmware is
    already downloaded + the chip re-enumerated; this runs the HTC/WMI handshake, ath9k_hw init,
    ath9k_htc_start, the RX path, the monitor vif, and the initial set_channel — the exact wire of
    ``_walk_init`` in the verify gate. [SRC] htc_drv_init.c + hw.c + htc_drv_main.c."""
    st = htc.handshake(t)
    wmi = WMI(t, ctrl_epid=st.endpoints[C.WMI_CONTROL_SVC])
    hw = hwmod.init_reset(wmi)
    phy.rf_claim(hw)
    eeprom.init(hw)
    ani.ani_init(hw)
    key.init_crypto(hw)
    wmi.get_fw_version()

    # ath9k_htc_start wake path: a COLD chip reset (chip was FULL_SLEEPed after probe), then
    # init_pll for the initial channel (mac80211 default = ch1, 2412 MHz).
    chan = chanmod.channel_2ghz(1)
    hw.set_reset_reg(R.ATH9K_RESET_COLD)
    hw.init_pll(chan)
    gpio.led_init(hw)
    wmi.cmd(WMI_FLUSH_RECV_CMDID, b"")

    # ath9k_hw_reset (cold): preamble saves + chip_reset(WARM) + init_pll + MAC-gate + TSF restore
    # (the low word is wall-clock-dependent — the gate value-excepts AR_TSF_L32) + JTAG disable.
    hw.reset_begin(chan)
    hw_reset_body(wmi, hw, chan)

    # ath9k_htc_start tail [SRC] htc_drv_main.c:941: re-apply tx power (priv->txpowlimit=0 at the
    # first start clamps every per-rate target to 0), then SET_MODE(11ng) / ATH_INIT / START_RECV.
    phy_power.update_txpow(hw, chan, 0)
    wmi.cmd(WMI_SET_MODE_CMDID, _MODE_11NG)
    wmi.cmd(WMI_ATH_INIT_CMDID, b"")
    wmi.cmd(WMI_START_RECV_CMDID, b"")
    rx.host_rx_init(hw)
    wmi.update_cap_target(hw.txchainmask)

    # mac80211 promiscuous-monitor configure_filter (FIF_CONTROL|PSPOLL|BCN_PRBRESP_PROMISC|
    # OTHER_BSS -> 0xc01f) with the LED driven on in between.
    flags = rx.FilterFlags(control=True, pspoll=True, bcn_prbresp_promisc=True, other_bss=True)
    hw.rxfilter_flags = flags
    rfilt = rx.calcrxfilter(hw)
    gpio.set_gpio(hw, R.ATH_LED_PIN_9271, 0)
    rx.setrxfilter(hw, rfilt)

    # ath9k_htc_add_monitor_interface: create the monitor vif + its self-station.
    wmi.vap_create(0, HTC_M_MONITOR, hw.macaddr)
    wmi.node_create(hw.macaddr, b"\x00" * 6, 0, 0, 1, 0xFFFF)
    hw.is_monitoring = True
    hw.opmode = R.IFTYPE_MONITOR                    # no other vifs -> hw opmode = monitor

    # ath9k_htc_set_channel for the initial tune [SRC] htc_drv_main.c:225 (warm: curchan set ->
    # getnf first, htc_reset_init already cleared -> no RF-reset pulse). Same channel, so
    # process_ini re-runs identically.
    wmi.cmd(WMI_DISABLE_INTR_CMDID, b"")
    wmi.cmd(WMI_DRAIN_TXQ_ALL_CMDID, b"")
    wmi.cmd(WMI_STOP_RECV_CMDID, b"")
    hw.curchan = chan
    hw.getnf(chan)
    hw.reset_begin(chan)
    hw_reset_body(wmi, hw, chan)
    wmi.cmd(WMI_START_RECV_CMDID, b"")
    rx.host_rx_init(hw)                             # now monitoring -> 0xc03f
    wmi.cmd(WMI_SET_MODE_CMDID, _MODE_11NG)
    wmi.cmd(WMI_ENABLE_INTR_CMDID, b"")

    # config CONF_CHANGE_POWER: power_level 20 -> txpowlimit 40 raises the limit back from 0,
    # then mac80211 re-applies the (unchanged) monitor filter -> 0xc03f (now with PROM).
    phy_power.update_txpow(hw, chan, 40)
    rx.configure_filter(hw, hw.rxfilter_flags)
    return BringupResult(wmi=wmi, hw=hw, endpoints=st.endpoints)


def full_channel_change(wmi: WMI, hw: hwmod.AthHw, chan: chanmod.Channel) -> None:
    """One ath9k_htc_set_channel hop via a full ath9k_hw_reset [SRC] htc_drv_main.c:225: stop the
    target, reset to ``chan``, restart RX. This is the always-correct retune path (the cold tune
    uses the same body); the fast path below is the kernel's within-band optimisation."""
    wmi.cmd(WMI_DISABLE_INTR_CMDID, b"")
    wmi.cmd(WMI_DRAIN_TXQ_ALL_CMDID, b"")
    wmi.cmd(WMI_STOP_RECV_CMDID, b"")
    hw.getnf(chan)
    hw.reset_begin(chan)
    hw_reset_body(wmi, hw, chan)
    wmi.cmd(WMI_START_RECV_CMDID, b"")
    rx.host_rx_init(hw)
    wmi.cmd(WMI_SET_MODE_CMDID, _MODE_11NG)
    wmi.cmd(WMI_ENABLE_INTR_CMDID, b"")
    hw.curchan = chan


def _channel_change_body(wmi: WMI, hw: hwmod.AthHw, chan: chanmod.Channel) -> None:
    """ath9k_hw_channel_change [SRC] hw.c:1543 — the fast retune the AR9271 runs without a full
    reset: confirm the QCUs are idle, pause the baseband (rfbus), reprogram the per-channel PHY +
    synth + TX power, then release. The FCC_BAND_SWITCH / band_switch reloads are guarded by caps
    the 9271 never sets on a within-band hop, so they are skipped."""
    [hw.numtxpending(q) for q in range(R.AR_NUM_QCU)]
    phy.rfbus_req(hw)
    phy.set_channel_regs(hw, chan)
    phy.rf_set_freq(hw, chan)
    hw.set_clockrate()                              # no wire ops
    phy_power.apply_txpower(hw, chan)
    phy.set_delta_slope(hw, chan)
    phy.spur_mitigate(hw, chan)
    phy.init_bb(hw, chan)
    phy.rfbus_done(hw)


def fast_channel_change(wmi: WMI, hw: hwmod.AthHw, chan: chanmod.Channel) -> None:
    """One ath9k_htc_set_channel hop on the fast path [SRC] htc_drv_main.c:240 (fastcc, caldata
    NULL): stop the target, then ath9k_hw_reset's fastcc branch -> ath9k_hw_do_fastcc [SRC]
    hw.c:1788 (check_alive + channel_change + loadnf + start_nfcal + the AR9271 ANI-reg reload),
    then restart RX."""
    wmi.cmd(WMI_DISABLE_INTR_CMDID, b"")
    wmi.cmd(WMI_DRAIN_TXQ_ALL_CMDID, b"")
    wmi.cmd(WMI_STOP_RECV_CMDID, b"")
    hw.getnf(chan)                                  # ath9k_hw_reset getnf(curchan)
    hw.check_alive()
    _channel_change_body(wmi, hw, chan)
    calib.loadnf(hw, chan)
    calib.start_nfcal(hw, update=True)
    phy.load_ani_reg(hw, chan)                      # AR9271-only
    wmi.cmd(WMI_START_RECV_CMDID, b"")
    rx.host_rx_init(hw)
    wmi.cmd(WMI_SET_MODE_CMDID, _MODE_11NG)
    wmi.cmd(WMI_ENABLE_INTR_CMDID, b"")
    hw.curchan = chan
