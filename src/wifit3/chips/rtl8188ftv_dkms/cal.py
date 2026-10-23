"""RTL8188FTV DKMS LC calibration (M5h).

Ported from ``PHY_LCCalibrate_8188F`` + ``phy_LCCalibrate_8188F``
(hal/phydm/rtl8188f/halphyrf_8188f.c:2936-2990,2083+): packet-TX branch
(TX pause), RF 0x18 backup, LCK start, ready poll, channel recover, TX
unpause. Wrapper gates (MP single-tone, ability, scan wait) take recorded
defaults as parameters.
"""
from __future__ import annotations

import time

from . import rf


def lc_calibrate(t, single_tone: bool = False, carrier_supp: bool = False,
                 ability: bool = True, scanning: bool = False) -> None:
    if single_tone or carrier_supp:
        return
    if not ability:
        return
    if scanning:
        # TODO: verify, untested here (scan-in-progress 50ms-tick wait)
        deadline = time.monotonic() + 2.0
        while scanning and time.monotonic() < deadline:
            time.sleep(0.050)
    worker(t)


def worker(t) -> None:
    tmp = t.read8(0xD03)
    if tmp & 0x70:
        t.write8(0xD03, tmp & 0x8F)
    else:
        t.write8(0x522, 0xFF)
    lc_cal = rf.query_rf_reg(t, rf.RF_PATH_A, 0x18, rf.RF_REG_OFFSET_MASK)
    rf.set_rf_reg(t, rf.RF_PATH_A, 0x18, rf.RF_REG_OFFSET_MASK, lc_cal | 0x08000)
    for _ in range(100):
        if rf.query_rf_reg(t, rf.RF_PATH_A, 0x18, 0x8000) != 0x1:
            break
        time.sleep(0.010)
    rf.set_rf_reg(t, rf.RF_PATH_A, 0x18, rf.RF_REG_OFFSET_MASK, lc_cal)
    if tmp & 0x70:
        t.write8(0xD03, tmp)
    else:
        t.write8(0x522, 0x00)
