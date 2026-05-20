"""rt2800usb 2.4 GHz channel tune.

Minimal port of rt2800_config_channel (rt2800lib.c:4161+) for the
RT5390/RT5392 silicon family. RX-only: skips the EEPROM-dependent TX
power writes and BBP62/63/64/86 noise-floor tweaks (lna_gain comes
from EEPROM too). That means TX may be at minimum power if you try to
inject from this chip before EEPROM bring-up lands. RX works fine
because the synth tune is purely from the rf_vals_3x table.

Channels 1..14 only for now — 5 GHz needs the RF55xx variant + EFUSE
power tables.

[SRC] rt2800lib.c:3387-3483 (config_channel_rf53xx)
      rt2800lib.c:11435-11449 (rf_vals_3x table, 2.4 GHz portion)
      rt2800lib.c:2447-2480 (freq_cal_mode1)
"""
from __future__ import annotations

import logging
import time

from .constants import (
    RFCSR1_PLL_PD,
    RFCSR1_RF_BLOCK_EN,
    RFCSR1_RX0_PD,
    RFCSR1_RX1_PD,
    RFCSR1_TX0_PD,
    RFCSR1_TX1_PD,
    RFCSR3_VCOCAL_EN,
    RFCSR11_R,
    RFCSR30_RX_H20M,
    RFCSR30_TX_H20M,
    RT_RT5392,
)
from .firmware import mcu_request
from .rfcsr import rfcsr_read, rfcsr_write
from .transport import RT2800USBTransport

logger = logging.getLogger(__name__)

# Channels 1..14 from rt2800lib.c:11435-11449 (rf_vals_3x).
# Tuple format: (rf1, rf2, rf3) — rf4 = 0 for the 2.4 GHz entries.
# Kernel struct rf_channel is (channel, rf1, rf2, rf3, rf4); rf53xx
# uses rf1/rf2/rf3 only.
_RF_VALS_2G = {
    1:  (241, 2, 2),
    2:  (241, 2, 7),
    3:  (242, 2, 2),
    4:  (242, 2, 7),
    5:  (243, 2, 2),
    6:  (243, 2, 7),
    7:  (244, 2, 2),
    8:  (244, 2, 7),
    9:  (245, 2, 2),
    10: (245, 2, 7),
    11: (246, 2, 2),
    12: (246, 2, 7),
    13: (247, 2, 2),
    14: (248, 2, 4),
}

# MCU command for freq offset update on USB.  [SRC] rt2800.h
MCU_FREQ_OFFSET = 0x74


def freq_cal_mode1_usb(t: RT2800USBTransport, freq_offset: int = 0) -> None:
    """USB version of freq_cal_mode1 (rt2800lib.c:2447-2480).

    On USB this just sends an MCU command. ``freq_offset`` comes from
    EEPROM in the kernel — we pass 0 since we haven't ported EEPROM
    bring-up. The chip's calibration default usually lands close enough
    for RX.
    """
    rfcsr17 = rfcsr_read(t, 17)
    # rt2800_mcu_request(MCU_FREQ_OFFSET, 0xff, freq_offset, prev_rfcsr17)
    mcu_request(
        t, MCU_FREQ_OFFSET,
        token=0xFF, arg0=freq_offset & 0xFF, arg1=rfcsr17 & 0xFF,
    )


def set_channel(t: RT2800USBTransport, silicon_id: int, channel: int) -> None:
    """Tune to a 2.4 GHz channel.  Supports RT5390/RT5392 only at M-now.

    Raises ValueError on out-of-range channel.
    """
    if channel not in _RF_VALS_2G:
        raise ValueError(f"channel {channel} not in 2.4 GHz range (1..14)")
    if silicon_id != RT_RT5392:
        # RT5390 takes the same code path but we haven't tested.
        # Other silicons need their own RF init + per-RF-chip set_channel.
        raise NotImplementedError(
            f"set_channel for silicon 0x{silicon_id:04x} not yet validated; "
            f"current support is RT5392 only"
        )

    rf1, rf2, rf3 = _RF_VALS_2G[channel]

    # Synthesizer N + mod + R.  [SRC] rt2800lib.c:3395-3399
    rfcsr_write(t, 8, rf1)
    rfcsr_write(t, 9, rf3)
    rfcsr = rfcsr_read(t, 11)
    rfcsr = (rfcsr & ~RFCSR11_R) | (rf2 & RFCSR11_R)
    rfcsr_write(t, 11, rfcsr & 0xFF)

    # Skip TX power writes (RFCSR49/50) — would need EEPROM defaults.

    # RFCSR1: enable RF block + PLL + RX0/TX0/RX1/TX1 powerdown released.
    # [SRC] rt2800lib.c:3418-3427
    rfcsr = rfcsr_read(t, 1)
    rfcsr |= RFCSR1_RX1_PD | RFCSR1_TX1_PD     # RT5392-specific
    rfcsr |= RFCSR1_RF_BLOCK_EN
    rfcsr |= RFCSR1_PLL_PD
    rfcsr |= RFCSR1_RX0_PD
    rfcsr |= RFCSR1_TX0_PD
    rfcsr_write(t, 1, rfcsr & 0xFF)

    # Freq offset cal (USB path → MCU command).
    freq_cal_mode1_usb(t, freq_offset=0)

    # Skip BT coex / R55+R59 channel-specific writes (we don't have
    # the EEPROM cap_bt_coexist bit).

    # 20 MHz bandwidth — clear TX_H20M + RX_H20M bits in RFCSR30.
    # [SRC] rt2800lib.c:4244-4250
    rfcsr = rfcsr_read(t, 30)
    rfcsr &= ~(RFCSR30_TX_H20M | RFCSR30_RX_H20M) & 0xFF
    rfcsr_write(t, 30, rfcsr)

    # Trigger VCO calibration on the new channel.  [SRC] rt2800lib.c:4252-4254
    rfcsr = rfcsr_read(t, 3)
    rfcsr |= RFCSR3_VCOCAL_EN
    rfcsr_write(t, 3, rfcsr & 0xFF)

    # Kernel sleeps ~1ms here implicitly via subsequent register-access
    # latency. We add an explicit small delay.
    time.sleep(0.001)

    logger.debug("set_channel: ch=%d, rf1=%d, rf2=%d, rf3=%d",
                 channel, rf1, rf2, rf3)
