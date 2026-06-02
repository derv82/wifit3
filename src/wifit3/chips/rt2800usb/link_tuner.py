"""Periodic RX-AGC adaptation — the rt2x00 "link tuner" (BBP66 / VGC).

The rt2800 receiver's analog gain is governed by BBP register 66 (the
"VGC level"). The channel-tune path seeds it once to the most-sensitive
default; the kernel then runs a ~1 Hz tuner that re-seeds it from a
running average of received-frame RSSI. Without that loop the gain is
frozen at the seed: fine for weak/distant signals, but a strong nearby
AP overloads the front-end (AGC saturation / false-CCA bursts) and
beacons get dropped — a strong near AP comes in *worse* than a weak far
one. This module is that missing loop.

[SRC] rt2800lib.c:5723 rt2800_get_default_vgc
      rt2800lib.c:5759 rt2800_set_vgc
      rt2800lib.c:5787 rt2800_link_tuner
      rt2x00link.c:341 rt2x00link_tuner (1 Hz work; DEFAULT_RSSI=-128)
      rt2x00lib.h:21   LINK_TUNE_SECONDS = 1
      rt2x00.h:258     DECLARE_EWMA(rssi, 10, 8)  (new-sample weight 1/8)
      rt2800lib.c:12085 CAPABILITY_LINK_TUNING set unconditionally for rt2800

MONITOR-MODE DEVIATION: the kernel only runs this in STA mode and feeds
the EWMA from beacons of the *associated* BSS (rt2x00link.c:191,205 gate
on intf_sta_count + RXDONE_MY_BSS), and explicitly skips the tuner for a
pure-monitor interface (rt2x00link.c:228). Wifit3 is always-monitor, so
there is no associated BSS — we feed the EWMA from *every* successfully
received frame's RSSI instead. The algorithm (default-VGC + RSSI-gated
delta) is ported verbatim; only the RSSI source differs. The adjustment
is conservative: it can only *raise* VGC (de-sensitise) when the average
signal is strong, and falls back to the most-sensitive default whenever
no frame arrived in the last interval — so weak-signal sensitivity is
never reduced.
"""
from __future__ import annotations

from .bbp import bbp_write, bbp_write_with_rx_chain
from .constants import RT_RT3572, RT_RT5592
from .transport import RT2800USBTransport

# Returned when no usable RSSI is available, telling the tuner to pick the
# most-sensitive settings. [SRC] rt2x00link.c:23.
DEFAULT_RSSI = -128

# Tuner cadence. [SRC] rt2x00lib.h:21 (LINK_TUNE_SECONDS = 1).
LINK_TUNE_SECONDS = 1.0

# EWMA reciprocal weight — new sample contributes 1/8, matching the
# kernel's DECLARE_EWMA(rssi, 10, 8). [SRC] rt2x00.h:258.
_EWMA_WEIGHT_RCP = 8


def get_default_vgc(
    silicon_id: int, channel: int, lna_gain: int, *, ht40: bool = False
) -> int:
    """Most-sensitive VGC seed for this band/silicon — rt2800_get_default_vgc.

    Every silicon this driver supports (RT3572 / RT5390 / RT5392 / RT5592)
    is in the kernel's ``0x1c + 2*lna_gain`` group for 2.4 GHz; the older
    ``0x2e + lna_gain`` group (RT2860/RT2872-class) is not reachable here.
    [SRC] rt2800lib.c:5723.
    """
    if channel <= 14:                       # 2.4 GHz
        return (0x1C + 2 * lna_gain) & 0xFF
    # 5 GHz
    if silicon_id == RT_RT5592:
        return (0x24 + 2 * lna_gain) & 0xFF
    base = 0x3A if ht40 else 0x32           # RT3572 (and other non-RT3593/5592)
    return (base + (lna_gain * 5) // 3) & 0xFF


def compute_link_vgc(
    silicon_id: int, channel: int, lna_gain: int, rssi: int, *, ht40: bool = False
) -> int:
    """Default seed plus the RSSI-gated link-tuner delta — rt2800_link_tuner.

    A strong average signal raises VGC by a chip-specific amount to keep
    the front-end out of saturation; a weak/absent signal leaves it at the
    most-sensitive seed. [SRC] rt2800lib.c:5787.
    """
    vgc = get_default_vgc(silicon_id, channel, lna_gain, ht40=ht40)
    is_2g = channel <= 14
    if silicon_id == RT_RT3572:
        if rssi > -65:
            vgc += 0x20 if is_2g else 0x10
    elif silicon_id == RT_RT5592:
        if rssi > -65:
            vgc += 0x20
    else:                                   # RT5390 / RT5392 (default case)
        if rssi > -80:
            vgc += 0x10
    return vgc & 0xFF


def set_vgc(
    t: RT2800USBTransport, silicon_id: int, vgc: int, *, rx_chain_num: int, rssi: int
) -> None:
    """Write the VGC level to BBP66 per the silicon's path — rt2800_set_vgc.

    RT3572 fans the write across each RX chain; RT5592 additionally nudges
    BBP83 by RSSI; the RT539x parts write BBP66 directly. [SRC] rt2800lib.c:5759.
    """
    if silicon_id == RT_RT3572:
        bbp_write_with_rx_chain(t, 66, vgc, rx_chain_num=rx_chain_num)
    elif silicon_id == RT_RT5592:
        bbp_write(t, 83, 0x4A if rssi > -65 else 0x7A)
        bbp_write_with_rx_chain(t, 66, vgc, rx_chain_num=rx_chain_num)
    else:                                   # RT5390 / RT5392
        bbp_write(t, 66, vgc)


class LinkTuner:
    """RSSI accumulator + VGC bookkeeping for one driver instance.

    ``feed`` runs from RX dispatch and ``avg_rssi``/``end_interval`` from
    the periodic tick — both on the event-loop thread (RxReaderThread hands
    buffers over via ``call_soon_threadsafe``), so the state needs no lock.
    """

    def __init__(self) -> None:
        self._ewma_mag: float | None = None   # running |RSSI| magnitude
        self._rx_this_interval: int = 0       # frames fed since last tick
        # Last VGC we actually wrote; None forces the next tick to write,
        # mirroring the kernel's vgc_level reset to 0 in reset_tuner.
        self.vgc_level: int | None = None

    def feed(self, rssi_dbm: int) -> None:
        """Fold one successfully-received frame's RSSI into the EWMA."""
        mag = -float(rssi_dbm)
        if mag <= 0:                          # implausible (RSSI >= 0) — ignore
            return
        self._rx_this_interval += 1
        if self._ewma_mag is None:
            self._ewma_mag = mag
        else:
            self._ewma_mag += (mag - self._ewma_mag) / _EWMA_WEIGHT_RCP

    def avg_rssi(self) -> int:
        """Average RSSI for this interval, or DEFAULT_RSSI if nothing came in.

        The kernel uses DEFAULT_RSSI whenever ``qual->rx_success == 0`` for
        the interval, which pins the receiver to maximum sensitivity during
        quiet stretches. [SRC] rt2x00link.c:314.
        """
        if self._rx_this_interval == 0 or self._ewma_mag is None:
            return DEFAULT_RSSI
        return -int(self._ewma_mag)

    def end_interval(self) -> None:
        """Clear the per-interval frame tally; the EWMA itself persists."""
        self._rx_this_interval = 0

    def reset(self) -> None:
        """Channel/radio change — drop the averaged RSSI and force a rewrite.

        Mirrors rt2x00link_reset_tuner: the channel-tune path has just
        rewritten BBP66 to its own AGC value, so the next tick must
        re-establish the tuner's default. [SRC] rt2x00link.c:252.
        """
        self._ewma_mag = None
        self._rx_this_interval = 0
        self.vgc_level = None
