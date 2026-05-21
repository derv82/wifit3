"""MT76x0U RF init-value tables — ported 1:1 from kernel `initvals_phy.h`.

[SRC] data_dumps/mt76-source-v6.18/mt76x0/initvals_phy.h

Six tables consumed by `mt76x0_phy_rf_init`:
  - RF_CENTRAL_TAB         — bank 0 init (via rf_patch_reg_array)
  - RF_2G_CHANNEL_0_TAB    — bank 5 init (via rf_patch_reg_array)
  - RF_5G_CHANNEL_0_TAB    — bank 6 init (via RF_RANDOM_WRITE MCU)
  - RF_VGA_CHANNEL_0_TAB   — bank 7 init (via RF_RANDOM_WRITE MCU)
  - RF_BW_SWITCH_TAB       — filtered direct rf_wr per RF_BW_20 or (G|BW20)
  - RF_BAND_SWITCH_TAB     — filtered direct rf_wr per RF_G_BAND match

`rf_patch_reg_array` is per-entry write with three runtime overrides for
MT_RF(0,3), MT_RF(0,21), MT_RF(5,2) — see `apply_rf_patch_override` in phy.py.
"""
from __future__ import annotations

from .constants import (
    MT_RF,
    RF_A_BAND,
    RF_A_BAND_11J,
    RF_A_BAND_HB,
    RF_A_BAND_LB,
    RF_A_BAND_MB,
    RF_BW_20,
    RF_BW_40,
    RF_BW_80,
    RF_G_BAND,
)


# [SRC] initvals_phy.h:11-63 — 44 entries. Per-entry value may be overridden
# at runtime by `apply_rf_patch_override` for MT_RF(0,3) / (0,21).
RF_CENTRAL_TAB: list[tuple[int, int]] = [
    (MT_RF(0,  1), 0x01),
    (MT_RF(0,  2), 0x11),
    # R3 ~ R7: VCO Cal
    (MT_RF(0,  3), 0x73),   # VCO Freq Cal — USB always 0x73 (patched at runtime too)
    (MT_RF(0,  4), 0x30),   # R4 b<7>=1, VCO cal
    (MT_RF(0,  5), 0x00),
    (MT_RF(0,  6), 0x41),
    (MT_RF(0,  7), 0x00),
    (MT_RF(0,  8), 0x00),
    (MT_RF(0,  9), 0x00),
    (MT_RF(0, 10), 0x0C),
    (MT_RF(0, 11), 0x00),
    (MT_RF(0, 12), 0x00),
    # BG
    (MT_RF(0, 13), 0x00),
    (MT_RF(0, 14), 0x00),
    (MT_RF(0, 15), 0x00),
    # LDO
    (MT_RF(0, 19), 0x20),
    (MT_RF(0, 20), 0x22),
    (MT_RF(0, 21), 0x12),   # not-mt7610e → 0x12 (patched)
    (MT_RF(0, 23), 0x00),
    (MT_RF(0, 24), 0x33),
    (MT_RF(0, 25), 0x00),
    # PLL
    (MT_RF(0, 26), 0x00),
    (MT_RF(0, 27), 0x00),
    (MT_RF(0, 28), 0x00),
    (MT_RF(0, 29), 0x00),
    (MT_RF(0, 30), 0x00),
    (MT_RF(0, 31), 0x00),
    (MT_RF(0, 32), 0x00),
    (MT_RF(0, 33), 0x00),
    (MT_RF(0, 34), 0x00),
    (MT_RF(0, 35), 0x00),
    (MT_RF(0, 36), 0x00),
    (MT_RF(0, 37), 0x00),
    # LO Buffer
    (MT_RF(0, 38), 0x2F),
    # Test Ports
    (MT_RF(0, 64), 0x00),
    (MT_RF(0, 65), 0x80),
    (MT_RF(0, 66), 0x01),
    (MT_RF(0, 67), 0x04),
    # ADC-DAC
    (MT_RF(0, 68), 0x00),
    (MT_RF(0, 69), 0x08),
    (MT_RF(0, 70), 0x08),
    (MT_RF(0, 71), 0x40),
    (MT_RF(0, 72), 0xD0),
    (MT_RF(0, 73), 0x93),
]


# [SRC] initvals_phy.h:65-140 — 68 entries.
RF_2G_CHANNEL_0_TAB: list[tuple[int, int]] = [
    # RX logic operation
    (MT_RF(5,  2), 0x0C),   # not-mt7630/not-mt7610e → 0x0c (patched)
    (MT_RF(5,  3), 0x00),
    # TX logic operation
    (MT_RF(5,  4), 0x00),
    (MT_RF(5,  5), 0x84),
    (MT_RF(5,  6), 0x02),
    # LDO
    (MT_RF(5,  7), 0x00),
    (MT_RF(5,  8), 0x00),
    (MT_RF(5,  9), 0x00),
    # RX
    (MT_RF(5, 10), 0x51),
    (MT_RF(5, 11), 0x22),
    (MT_RF(5, 12), 0x22),
    (MT_RF(5, 13), 0x0F),
    (MT_RF(5, 14), 0x47),
    (MT_RF(5, 15), 0x25),
    (MT_RF(5, 16), 0xC7),
    (MT_RF(5, 17), 0x00),
    (MT_RF(5, 18), 0x00),
    (MT_RF(5, 19), 0x30),
    (MT_RF(5, 20), 0x33),
    (MT_RF(5, 21), 0x02),
    (MT_RF(5, 22), 0x32),
    (MT_RF(5, 23), 0x00),
    (MT_RF(5, 24), 0x25),
    (MT_RF(5, 26), 0x00),
    (MT_RF(5, 27), 0x12),
    (MT_RF(5, 28), 0x0F),
    (MT_RF(5, 29), 0x00),
    # LOGEN
    (MT_RF(5, 30), 0x51),
    (MT_RF(5, 31), 0x35),
    (MT_RF(5, 32), 0x31),
    (MT_RF(5, 33), 0x31),
    (MT_RF(5, 34), 0x34),
    (MT_RF(5, 35), 0x03),
    (MT_RF(5, 36), 0x00),
    # TX
    (MT_RF(5, 37), 0xDD),
    (MT_RF(5, 38), 0xB3),
    (MT_RF(5, 39), 0x33),
    (MT_RF(5, 40), 0xB1),
    (MT_RF(5, 41), 0x71),
    (MT_RF(5, 42), 0xF2),
    (MT_RF(5, 43), 0x47),
    (MT_RF(5, 44), 0x77),
    (MT_RF(5, 45), 0x0E),
    (MT_RF(5, 46), 0x10),
    (MT_RF(5, 47), 0x00),
    (MT_RF(5, 48), 0x53),
    (MT_RF(5, 49), 0x03),
    (MT_RF(5, 50), 0xEF),
    (MT_RF(5, 51), 0xC7),
    (MT_RF(5, 52), 0x62),
    (MT_RF(5, 53), 0x62),
    (MT_RF(5, 54), 0x00),
    (MT_RF(5, 55), 0x00),
    (MT_RF(5, 56), 0x0F),
    (MT_RF(5, 57), 0x0F),
    (MT_RF(5, 58), 0x16),
    (MT_RF(5, 59), 0x16),
    (MT_RF(5, 60), 0x10),
    (MT_RF(5, 61), 0x10),
    (MT_RF(5, 62), 0xD0),
    (MT_RF(5, 63), 0x6C),
    (MT_RF(5, 64), 0x58),
    (MT_RF(5, 65), 0x58),
    (MT_RF(5, 66), 0xF2),
    (MT_RF(5, 67), 0xE8),
    (MT_RF(5, 68), 0xF0),
    (MT_RF(5, 69), 0xF0),
    (MT_RF(5, 127), 0x04),
]


# [SRC] initvals_phy.h:142-187 — 38 entries (RANDOM_WRITE).
RF_5G_CHANNEL_0_TAB: list[tuple[int, int]] = [
    # RX logic operation
    (MT_RF(6, 2), 0x0C),
    (MT_RF(6, 3), 0x00),
    # TX logic operation
    (MT_RF(6, 4), 0x00),
    (MT_RF(6, 5), 0x84),
    (MT_RF(6, 6), 0x02),
    # LDO
    (MT_RF(6, 7), 0x00),
    (MT_RF(6, 8), 0x00),
    (MT_RF(6, 9), 0x00),
    # RX
    (MT_RF(6, 10), 0x00),
    (MT_RF(6, 11), 0x01),
    (MT_RF(6, 13), 0x23),
    (MT_RF(6, 14), 0x00),
    (MT_RF(6, 15), 0x04),
    (MT_RF(6, 16), 0x22),
    (MT_RF(6, 18), 0x08),
    (MT_RF(6, 19), 0x00),
    (MT_RF(6, 20), 0x00),
    (MT_RF(6, 21), 0x00),
    (MT_RF(6, 22), 0xFB),
    # LOGEN5G
    (MT_RF(6, 25), 0x76),
    (MT_RF(6, 26), 0x24),
    (MT_RF(6, 27), 0x04),
    (MT_RF(6, 28), 0x00),
    (MT_RF(6, 29), 0x00),
    # TX
    (MT_RF(6, 37), 0xBB),
    (MT_RF(6, 38), 0xB3),
    (MT_RF(6, 40), 0x33),
    (MT_RF(6, 41), 0x33),
    (MT_RF(6, 43), 0x03),
    (MT_RF(6, 44), 0xB3),
    (MT_RF(6, 46), 0x17),
    (MT_RF(6, 47), 0x0E),
    (MT_RF(6, 48), 0x10),
    (MT_RF(6, 49), 0x07),
    (MT_RF(6, 62), 0x00),
    (MT_RF(6, 63), 0x00),
    (MT_RF(6, 64), 0xF1),
    (MT_RF(6, 65), 0x0F),
]


# [SRC] initvals_phy.h:189-226 — 35 entries (RANDOM_WRITE).
RF_VGA_CHANNEL_0_TAB: list[tuple[int, int]] = [
    # E3 CR
    (MT_RF(7,  0), 0x47),
    (MT_RF(7,  1), 0x00),
    (MT_RF(7,  2), 0x00),
    (MT_RF(7,  3), 0x00),
    (MT_RF(7,  4), 0x00),
    (MT_RF(7, 10), 0x13),
    (MT_RF(7, 11), 0x0F),
    (MT_RF(7, 12), 0x13),
    (MT_RF(7, 13), 0x13),
    (MT_RF(7, 14), 0x13),
    (MT_RF(7, 15), 0x20),
    (MT_RF(7, 16), 0x22),
    (MT_RF(7, 17), 0x7C),
    (MT_RF(7, 18), 0x00),
    (MT_RF(7, 19), 0x00),
    (MT_RF(7, 20), 0x00),
    (MT_RF(7, 21), 0xF1),
    (MT_RF(7, 22), 0x11),
    (MT_RF(7, 23), 0xC2),
    (MT_RF(7, 24), 0x41),
    (MT_RF(7, 25), 0x20),
    (MT_RF(7, 26), 0x40),
    (MT_RF(7, 27), 0xD7),
    (MT_RF(7, 28), 0xA2),
    (MT_RF(7, 29), 0x60),
    (MT_RF(7, 30), 0x49),
    (MT_RF(7, 31), 0x20),
    (MT_RF(7, 32), 0x44),
    (MT_RF(7, 33), 0xC1),
    (MT_RF(7, 34), 0x60),
    (MT_RF(7, 35), 0xC0),
    (MT_RF(7, 61), 0x01),
    (MT_RF(7, 72), 0x3C),
    (MT_RF(7, 73), 0x34),
    (MT_RF(7, 74), 0x00),
]


# [SRC] initvals_phy.h:228-271 — 41 entries (bw_band filtered direct rf_wr).
# Each entry: (bw_band_mask, reg, value).
RF_BW_SWITCH_TAB: list[tuple[int, int, int]] = [
    (RF_G_BAND | RF_BW_20,                MT_RF(0, 17), 0x00),
    (RF_G_BAND | RF_BW_40,                MT_RF(0, 17), 0x00),
    (RF_A_BAND | RF_BW_20,                MT_RF(0, 17), 0x00),
    (RF_A_BAND | RF_BW_40,                MT_RF(0, 17), 0x00),
    (RF_A_BAND | RF_BW_80,                MT_RF(0, 17), 0x00),
    (RF_G_BAND | RF_BW_20,                MT_RF(7,  6), 0x40),
    (RF_G_BAND | RF_BW_40,                MT_RF(7,  6), 0x1C),
    (RF_A_BAND | RF_BW_20,                MT_RF(7,  6), 0x40),
    (RF_A_BAND | RF_BW_40,                MT_RF(7,  6), 0x20),
    (RF_A_BAND | RF_BW_80,                MT_RF(7,  6), 0x10),
    (RF_G_BAND | RF_BW_20,                MT_RF(7,  7), 0x40),
    (RF_G_BAND | RF_BW_40,                MT_RF(7,  7), 0x20),
    (RF_A_BAND | RF_BW_20,                MT_RF(7,  7), 0x40),
    (RF_A_BAND | RF_BW_40,                MT_RF(7,  7), 0x20),
    (RF_A_BAND | RF_BW_80,                MT_RF(7,  7), 0x10),
    (RF_G_BAND | RF_BW_20,                MT_RF(7,  8), 0x03),
    (RF_G_BAND | RF_BW_40,                MT_RF(7,  8), 0x01),
    (RF_A_BAND | RF_BW_20,                MT_RF(7,  8), 0x03),
    (RF_A_BAND | RF_BW_40,                MT_RF(7,  8), 0x01),
    (RF_A_BAND | RF_BW_80,                MT_RF(7,  8), 0x00),
    (RF_G_BAND | RF_BW_20,                MT_RF(7, 58), 0x40),
    (RF_G_BAND | RF_BW_40,                MT_RF(7, 58), 0x40),
    (RF_A_BAND | RF_BW_20,                MT_RF(7, 58), 0x40),
    (RF_A_BAND | RF_BW_40,                MT_RF(7, 58), 0x40),
    (RF_A_BAND | RF_BW_80,                MT_RF(7, 58), 0x10),
    (RF_G_BAND | RF_BW_20,                MT_RF(7, 59), 0x40),
    (RF_G_BAND | RF_BW_40,                MT_RF(7, 59), 0x40),
    (RF_A_BAND | RF_BW_20,                MT_RF(7, 59), 0x40),
    (RF_A_BAND | RF_BW_40,                MT_RF(7, 59), 0x40),
    (RF_A_BAND | RF_BW_80,                MT_RF(7, 59), 0x10),
    (RF_G_BAND | RF_BW_20,                MT_RF(7, 60), 0xAA),
    (RF_G_BAND | RF_BW_40,                MT_RF(7, 60), 0xAA),
    (RF_A_BAND | RF_BW_20,                MT_RF(7, 60), 0xAA),
    (RF_A_BAND | RF_BW_40,                MT_RF(7, 60), 0xAA),
    (RF_A_BAND | RF_BW_80,                MT_RF(7, 60), 0xAA),
    (RF_BW_20,                            MT_RF(7, 76), 0x40),
    (RF_BW_40,                            MT_RF(7, 76), 0x40),
    (RF_BW_80,                            MT_RF(7, 76), 0x10),
    (RF_BW_20,                            MT_RF(7, 77), 0x40),
    (RF_BW_40,                            MT_RF(7, 77), 0x40),
    (RF_BW_80,                            MT_RF(7, 77), 0x10),
]


# [SRC] initvals_phy.h:273-318 — 43 entries (band filtered direct rf_wr).
RF_BAND_SWITCH_TAB: list[tuple[int, int, int]] = [
    (RF_G_BAND,                           MT_RF(0,  16), 0x20),
    (RF_A_BAND,                           MT_RF(0,  16), 0x20),
    (RF_G_BAND,                           MT_RF(0,  18), 0x00),
    (RF_A_BAND,                           MT_RF(0,  18), 0x00),
    (RF_G_BAND,                           MT_RF(0,  39), 0x36),
    (RF_A_BAND_LB,                        MT_RF(0,  39), 0x34),
    (RF_A_BAND_MB,                        MT_RF(0,  39), 0x33),
    (RF_A_BAND_HB,                        MT_RF(0,  39), 0x31),
    (RF_A_BAND_11J,                       MT_RF(0,  39), 0x36),
    (RF_A_BAND_LB,                        MT_RF(6,  12), 0x44),
    (RF_A_BAND_MB,                        MT_RF(6,  12), 0x44),
    (RF_A_BAND_HB,                        MT_RF(6,  12), 0x55),
    (RF_A_BAND_11J,                       MT_RF(6,  12), 0x44),
    (RF_A_BAND_LB,                        MT_RF(6,  17), 0x02),
    (RF_A_BAND_MB,                        MT_RF(6,  17), 0x00),
    (RF_A_BAND_HB,                        MT_RF(6,  17), 0x00),
    (RF_A_BAND_11J,                       MT_RF(6,  17), 0x05),
    (RF_A_BAND_LB,                        MT_RF(6,  24), 0xA1),
    (RF_A_BAND_MB,                        MT_RF(6,  24), 0x41),
    (RF_A_BAND_HB,                        MT_RF(6,  24), 0x21),
    (RF_A_BAND_11J,                       MT_RF(6,  24), 0xE1),
    (RF_A_BAND_LB,                        MT_RF(6,  39), 0x36),
    (RF_A_BAND_MB,                        MT_RF(6,  39), 0x34),
    (RF_A_BAND_HB,                        MT_RF(6,  39), 0x32),
    (RF_A_BAND_11J,                       MT_RF(6,  39), 0x37),
    (RF_A_BAND_LB,                        MT_RF(6,  42), 0xFB),
    (RF_A_BAND_MB,                        MT_RF(6,  42), 0xF3),
    (RF_A_BAND_HB,                        MT_RF(6,  42), 0xEB),
    (RF_A_BAND_11J,                       MT_RF(6,  42), 0xEB),
    (RF_G_BAND,                           MT_RF(6, 127), 0x84),
    (RF_A_BAND,                           MT_RF(6, 127), 0x04),
    (RF_G_BAND,                           MT_RF(7,   5), 0x40),
    (RF_A_BAND,                           MT_RF(7,   5), 0x00),
    (RF_G_BAND,                           MT_RF(7,   9), 0x00),
    (RF_A_BAND,                           MT_RF(7,   9), 0x00),
    (RF_G_BAND,                           MT_RF(7,  70), 0x00),
    (RF_A_BAND,                           MT_RF(7,  70), 0x6D),
    (RF_G_BAND,                           MT_RF(7,  71), 0x00),
    (RF_A_BAND,                           MT_RF(7,  71), 0xB0),
    (RF_G_BAND,                           MT_RF(7,  78), 0x00),
    (RF_A_BAND,                           MT_RF(7,  78), 0x55),
    (RF_G_BAND,                           MT_RF(7,  79), 0x00),
    (RF_A_BAND,                           MT_RF(7,  79), 0x55),
]
