"""Monitor-mode entry + per-hop tune for the rt2500usb (RT2570).

Reproduces, in wire order, the rt2x00 framework calls the kernel runs when a
monitor interface comes up and when airodump/iw hop channels — the same code
the driver runs at runtime and the pcap gate replays [[passive_by_default]]:

  enable_monitor (airmon-ng start):
    led_enable + start_queue_rx     rt2x00lib_enable_radio tail (dev.c:69-77;
                                    the link tuner that follows is monitor-skipped)
    config_filter(monitoring=False) configure_filter, interface up
    initial_config                  the first rt2x00mac_config (POWER|PS, no channel)
    config_filter(monitoring=True)  configure_filter, monitor on (ToDS opens)

  tune_hop (airodump/iw channel set) = one rt2x00mac_config(CHANGE_CHANNEL):
    stop_queue_rx                   rt2x00mac_config brackets the config with
    config_channel + reset_tuner    rt2x00lib_config (CONF_CHANGE_CHANNEL)
    config_ant + reset_tuner        rt2x00lib_config_antenna (always runs)
    start_queue_rx

config_intf is absent: a monitor vif programs no MAC/BSSID/beacon-sync (the
cold-boot capture issues zero MAC_CSR2/CSR5/TXRX_CSR18-20 writes).

Source: rt2500usb.c + rt2x00{dev,mac,config}.c (rt2x00-source-v6.18).
"""
from __future__ import annotations

from . import bbp, chan, mac
from .constants import DEFAULT_TXPOWER
from .transport import RT2500USBTransport


def initial_config(t: RT2500USBTransport, rf_type: int, eeprom: bytes,
                   ant_tx: int, ant_rx: int, txpower: int = DEFAULT_TXPOWER) -> None:
    """The first rt2x00mac_config after radio-on — flags POWER|PS, no channel
    (rt2x00mac.c:307-352). Same stop/antenna/start frame as tune_hop, but the
    body runs config_txpower + config_ps instead of a channel tune."""
    mac.stop_queue_rx(t)
    chan.config_txpower(t, txpower, rf3=0)        # CONF_CHANGE_POWER (RF cache = 0)
    mac.config_ps(t)                              # CONF_CHANGE_PS → STATE_AWAKE
    chan.config_ant(t, rf_type, ant_tx, ant_rx)   # rt2x00lib_config_antenna
    bbp.reset_tuner(t, eeprom)                    # config_antenna's reset_tuner
    mac.start_queue_rx(t)


def enable_monitor(t: RT2500USBTransport, rf_type: int, eeprom: bytes,
                   ant_tx: int, ant_rx: int, txpower: int = DEFAULT_TXPOWER) -> None:
    """Bring up monitor mode: LEDs + RX queue → interface-up filter → initial
    config → monitor filter. Mirrors the airmon-ng start callback order."""
    mac.led_enable(t)                             # rt2x00lib_enable_radio tail
    mac.start_queue_rx(t)
    mac.config_filter(t, monitoring=False)        # configure_filter (interface up)
    initial_config(t, rf_type, eeprom, ant_tx, ant_rx, txpower)
    mac.config_filter(t, monitoring=True)         # configure_filter (monitor on)


def tune_hop(t: RT2500USBTransport, rf_type: int, channel: int, eeprom: bytes,
             ant_tx: int, ant_rx: int, txpower: int = DEFAULT_TXPOWER) -> bool:
    """One rt2x00mac_config(CHANGE_CHANNEL) — the airodump/iw hop sequence.

    RX is bracketed off (stop_queue_rx … start_queue_rx) because the chip
    ignores antenna/channel changes while RX is live. Returns config_channel's
    RF-busy status. reset_tuner re-seeds the AGC twice (once from
    rt2x00lib_config's CHANGE_CHANNEL, once from config_antenna) — kernel-exact.
    """
    mac.stop_queue_rx(t)
    ok = chan.config_channel(t, rf_type, channel, txpower)   # rt2x00lib_config
    bbp.reset_tuner(t, eeprom)                               # lib_config CHANGE_CHANNEL
    chan.config_ant(t, rf_type, ant_tx, ant_rx)             # rt2x00lib_config_antenna
    bbp.reset_tuner(t, eeprom)                               # config_antenna
    mac.start_queue_rx(t)
    return ok
