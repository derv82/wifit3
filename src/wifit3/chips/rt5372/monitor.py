"""Monitor-mode entry for the RT5372 (RT5392) — the ``airmon-ng start`` register sequence.

Reproduces, in wire order, the mac80211 callbacks the kernel runs when a monitor
interface comes up [[passive_by_default]]:

  1. ``configure_filter`` with the interface-up flags (CONFIG_MONITORING not yet set)
     ⇒ RX_FILTER_CFG = 0x97.
  2. ``rt2x00mac_config`` with the post-radio flags (POWER|RETRY|PS, no channel) —
     ``config_txpower`` + ``config_retry_limit`` + ``config_ps`` wrapped by the RX
     stop/start and the antenna reconfigure.
  3. ``configure_filter`` again, now with CONFIG_MONITORING set ⇒ 0x93 (clears
     DROP_NOT_TO_ME so the card receives every frame).

The channel tunes that follow (airodump/iw) are ``chan.set_channel`` calls, not part of
entry. ``config_erp`` is ported for a future managed-mode port but is *not* called in
monitor mode (BSS-change driven; a monitor vif has no BSS).

Ported from ``rt2800lib.c`` (``config_ps`` 5649, ``config_retry_limit`` 5636,
``config_erp`` 2177) + ``rt2x00mac.c`` / ``rt2x00config.c`` (the config wrapper).
"""
from __future__ import annotations

from . import chan, mac
from . import constants as C
from .constants import ChipInfo, set_field
from .eeprom import EepromValues
from .link_tuner import reset_tuner
from .transport import RT5372Transport

# Monitor RX filter: mac80211 masks total_flags to this set and forces ALLMULTI
# [SRC rt2x00mac.c:366-401]. Monitor wants control + ps-poll frames too.
MONITOR_FILTER = C.FIF_ALLMULTI | C.FIF_CONTROL | C.FIF_PSPOLL

# mac80211 hw->conf retry defaults (IEEE80211 short=7 / long=4).
DEFAULT_SHORT_RETRY = 7
DEFAULT_LONG_RETRY = 4


def config_retry_limit(t: RT5372Transport, short_retry: int = DEFAULT_SHORT_RETRY,
                       long_retry: int = DEFAULT_LONG_RETRY) -> None:
    """TX retry limits [SRC rt2800lib.c:5636-5647 rt2800_config_retry_limit]."""
    reg = t.register_read(C.TX_RTY_CFG)
    reg = set_field(reg, C.TX_RTY_CFG_SHORT_RTY_LIMIT, short_retry)
    reg = set_field(reg, C.TX_RTY_CFG_LONG_RTY_LIMIT, long_retry)
    t.register_write(C.TX_RTY_CFG, reg)


def config_ps(t: RT5372Transport) -> None:
    """Power-save config, STATE_AWAKE path [SRC rt2800lib.c:5649-5677 rt2800_config_ps].
    Monitor never sleeps: clear AUTOWAKEUP_CFG, then wake the MCU."""
    reg = t.register_read(C.AUTOWAKEUP_CFG)
    reg = set_field(reg, C.AUTOWAKEUP_CFG_AUTO_LEAD_TIME, 0)
    reg = set_field(reg, C.AUTOWAKEUP_CFG_TBCN_BEFORE_WAKE, 0)
    reg = set_field(reg, C.AUTOWAKEUP_CFG_AUTOWAKE, 0)
    t.register_write(C.AUTOWAKEUP_CFG, reg)
    mac.wakeup(t)


def config_erp(t: RT5372Transport, *, short_preamble: bool, cts_protection: bool,
               basic_rates: int, slot_time: int, eifs: int, beacon_int: int) -> None:
    """ERP / BSS parameters [SRC rt2800lib.c:2177-2222 rt2800_config_erp].

    #TODO untestable: BSS-change driven (managed/AP mode); a monitor vif has no BSS so
    this is never called in the airmon capture. Ported for a future STA port."""
    reg = t.register_read(C.AUTO_RSP_CFG)
    reg = set_field(reg, C.AUTO_RSP_CFG_AR_PREAMBLE, int(bool(short_preamble)))
    t.register_write(C.AUTO_RSP_CFG, reg)

    reg = t.register_read(C.OFDM_PROT_CFG)
    reg = set_field(reg, C.PROT_CFG_PROTECT_CTRL, 2 if cts_protection else 0)
    t.register_write(C.OFDM_PROT_CFG, reg)

    t.register_write(C.LEGACY_BASIC_RATE, 0xFF0 | basic_rates)
    t.register_write(C.HT_BASIC_RATE, 0x00008003)

    reg = t.register_read(C.BKOFF_SLOT_CFG)
    reg = set_field(reg, C.BKOFF_SLOT_CFG_SLOT_TIME, slot_time)
    t.register_write(C.BKOFF_SLOT_CFG, reg)

    reg = t.register_read(C.XIFS_TIME_CFG)
    reg = set_field(reg, C.XIFS_TIME_CFG_EIFS, eifs)
    t.register_write(C.XIFS_TIME_CFG, reg)

    reg = t.register_read(C.BCN_TIME_CFG)
    reg = set_field(reg, C.BCN_TIME_CFG_BEACON_INTERVAL, beacon_int * 16)
    t.register_write(C.BCN_TIME_CFG, reg)


def initial_config(t: RT5372Transport, chip: ChipInfo, ev: EepromValues) -> None:
    """The first ``rt2x00mac_config`` after the radio is up — flags POWER|RETRY|PS, no
    channel change [SRC rt2x00mac.c:307-352]. Same stop/antenna/start frame as
    ``chan.set_channel`` but the body runs txpower + retry + ps instead of a tune."""
    mac.stop_queue_rx(t)
    lna_gain = chan.config_lna_gain(ev, 0)        # rf.channel=0 ⇒ 2.4 GHz BG arm
    chan.config_txpower(t, chip, ev)              # CONF_CHANGE_POWER
    config_retry_limit(t)                         # CONF_CHANGE_RETRY_LIMITS
    config_ps(t)                                  # CONF_CHANGE_PS
    chan.config_ant(t, chip, ev)                  # rt2x00lib_config_antenna
    reset_tuner(t, chip, lna_gain)               # config_antenna's reset_tuner
    mac.start_queue_rx(t)                         # config_antenna's refcounted start_queue


def enable_monitor(t: RT5372Transport, chip: ChipInfo, ev: EepromValues, drv=None) -> None:
    """Bring up monitor mode: interface-up filter (0x97) → initial config → monitor
    filter (0x93). Mirrors the ``airmon-ng start`` callback order. ``drv`` is unused
    (RT5392 has no init-derived calibration)."""
    mac.config_filter(t, MONITOR_FILTER, monitoring=False)   # 0x97
    initial_config(t, chip, ev)
    mac.config_filter(t, MONITOR_FILTER, monitoring=True)    # 0x93
