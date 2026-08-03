"""Operational-phase entry: monitor bring-up + the periodic filter re-push.

This is the operational counterpart to ``bring_up.py``. Where ``bring_up`` is the
cold register init that mac80211 runs before the interface exists, ``enable_monitor``
is the exact register sequence mac80211/airmon issues when the monitor interface
comes *up* — and, like ``bring_up``, it is ONE function shared by ``driver.connect()``
(the live path) and the acceptance gate (``scripts/chips/rt5572/verify_pcap.py``). The gate
drives this real function, so it tests exactly what connect() runs; there is no second
copy to drift.

Order mirrors the RF5592 cold-boot capture's monitor-enable block byte-for-byte
(capture ops 1781-1838):

    start_queue(RX) → configure_filter #1 (CONFIG_MONITORING off → RX_FILTER 0x97) →
    stop_queue(RX) → config_txpower(2.4G) → config_retry_limit → config_ps(AWAKE) →
    config_ant → reset_tuner(set_vgc) → start_queue(RX) →
    configure_filter #2 (CONFIG_MONITORING on → RX_FILTER 0x93)

which is what these mac80211 callbacks emit when airmon brings up a monitor vif:

    rt2x00lib_enable_radio tail       → start_queue(RX)              [SRC] rt2x00dev.c:76
    rt2x00mac_configure_filter (×2)   → config_filter 0x97 then 0x93 [SRC] rt2x00mac.c:355
    rt2x00mac_config (no CHANGE_CHANNEL):                            [SRC] rt2x00mac.c:302
        stop_queue(RX)
        rt2x00lib_config → rt2800_config: config_txpower (CHANGE_POWER),
            config_retry_limit (CHANGE_RETRY_LIMITS), config_ps (CHANGE_PS)
        rt2x00lib_config_antenna → config_ant + reset_tuner          [SRC] rt2x00config.c
        start_queue(RX)

The two configure_filter passes differ only in CONFIG_MONITORING: the first (before the
monitor vif's CONF_MONITOR is committed) still drops not-to-me (0x97), the second opens
it (0x93 — promiscuous, dropping only CRC/PHY/VER errors + duplicates). The final resting
filter is 0x93.
"""
from __future__ import annotations

from .chan import config_ant, config_txpower
from .constants import FIF_ALLMULTI, FIF_CONTROL, FIF_PSPOLL
from .eeprom import EepromValues
from .link_tuner import get_default_vgc, set_vgc
from .mac import config_filter, config_ps_awake, config_retry_limit, toggle_rx
from .transport import RT5572Transport

# mac80211 always forces FIF_ALLMULTI, and — since rt2x00 advertises no separate
# control-frame filters — FIF_CONTROL and FIF_PSPOLL imply each other. So the flags
# that reach config_filter for a monitor vif are exactly these three.
# [SRC] rt2x00mac.c:367-397 rt2x00mac_configure_filter.
MON_FILTER_FLAGS = FIF_ALLMULTI | FIF_CONTROL | FIF_PSPOLL


def enable_monitor(t: RT5572Transport, silicon_id: int, ev: EepromValues,
                   xtal_40mhz: bool = False) -> None:
    """Bring the monitor interface up: the mac80211 start → configure_filter →
    config → configure_filter sequence, in the kernel's exact wire order.

    Leaves RX_FILTER_CFG at 0x93 (promiscuous monitor) — the kernel-faithful
    replacement for wifit3's old ``RX_FILTER_CFG=0x11`` monitor-first shortcut.
    Pure register I/O (no threads / async), so both connect() (via an executor)
    and the offline gate (via a ReplayDevice) drive the identical sequence."""
    # rt2x00lib_enable_radio tail: start_queue(RX). [SRC] rt2x00dev.c:76
    toggle_rx(t, True)
    # configure_filter #1 — CONFIG_MONITORING not yet set → still drops not-to-me.
    config_filter(t, MON_FILTER_FLAGS, monitoring=False)              # → 0x97

    # rt2x00mac_config (no CHANGE_CHANNEL): stop RX, run the ->config callbacks,
    # then config_antenna, then start RX. [SRC] rt2x00mac.c:302-350
    toggle_rx(t, False)
    config_txpower(t, ev, is_2g=True)                                # CHANGE_POWER
    config_retry_limit(t)                                            # CHANGE_RETRY_LIMITS
    config_ps_awake(t)                                              # CHANGE_PS

    # config_antenna → config_ant + reset_tuner. reset_tuner writes the default VGC
    # for the resting band (2.4 GHz here — no channel tuned yet) with rssi=0.
    # [SRC] rt2x00config.c rt2x00lib_config_antenna → rt2800_reset_tuner.
    vgc = get_default_vgc(silicon_id, 1, ev.lna_gain_bg)
    config_ant(t, ev.txpath, ev.rxpath)
    set_vgc(t, silicon_id, vgc, rx_chain_num=ev.rxpath, rssi=0)
    # config_antenna's trailing start_queue(RX) is the one that actually flips the
    # RX-enable bit (rt2x00mac_config's own start_queue then no-ops). [SRC] rt2x00queue.c:949
    toggle_rx(t, True)

    # configure_filter #2 — CONFIG_MONITORING now set → open not-to-me (promiscuous).
    config_filter(t, MON_FILTER_FLAGS, monitoring=True)              # → 0x93


def reapply_filter(t: RT5572Transport) -> None:
    """One periodic mac80211 configure_filter re-push (monitoring on → 0x93→0x93).

    mac80211 re-issues configure_filter on unrelated interface changes; on a
    monitor vif the flags are unchanged so it re-writes the same 0x93. The gate
    drives this for the handful of re-pushes that interleave between channel hops.
    [SRC] rt2x00mac.c:355 (same callback as enable_monitor's passes)."""
    config_filter(t, MON_FILTER_FLAGS, monitoring=True)
