"""Runtime device state threaded from init into the operational phase.

The kernel keeps this in ``struct rt2800_drv_data`` (calibration_bw20/40, bbp25/26)
and on ``struct rt2x00_dev`` (lna_gain, curr_band). We carry the init-derived
calibration here so channel tuning can consume it [SRC rt2800lib.c
rt2800_rx_filter_calibration → drv_data->calibration_bw20].

``lna_gain`` is recomputed from the EEPROM on every config (``config_lna_gain``),
so it lives on the per-config call, not here.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DrvData:
    """Init-derived calibration consumed by ``config_channel_rf3xxx``.

    ``calibration_bw20`` is the RX-filter calibration result (rfcsr24 after
    ``init_rx_filter(bw20)``); it becomes RFCSR24_TX_CALIB / RFCSR31_RX_CALIB on
    every 20 MHz channel tune. ``bbp25``/``bbp26`` are saved for RF3052 channel
    switching (unused on this RF3020 card, but captured faithfully)."""

    calibration_bw20: int
    calibration_bw40: int
    bbp25: int
    bbp26: int
