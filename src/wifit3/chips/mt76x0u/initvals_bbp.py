"""MT76x0U BBP init-value tables — ported 1:1 from kernel.

[SRC] mt76x0/initvals_init.h:86-157 — `mt76x0_bbp_init_tab` + `mt76x0_dcoc_tab`
[SRC] mt76x0/initvals.h:14-83        — `mt76x0_bbp_switch_tab`

The two MCU-loaded tables (init + dcoc) are written via CMD_RANDOM_WRITE
through the MCU channel. The switch tab is iterated host-side; only
entries matching the current band/bandwidth get written via direct
vendor xfers (mt76_wr).
"""
from __future__ import annotations

from .constants import (
    MT_BBP_AGC,
    MT_BBP_CAL,
    MT_BBP_CORE,
    MT_BBP_IBI,
    MT_BBP_RXC,
    MT_BBP_RXFE,
    MT_BBP_RXO,
    MT_BBP_TXBE,
    MT_BBP_TXC,
    MT_BBP_TXO,
    RF_A_BAND,
    RF_BW_20,
    RF_BW_40,
    RF_BW_80,
    RF_G_BAND,
)


# [SRC] initvals_init.h:86-145 — 54 entries.
BBP_INIT_TAB: list[tuple[int, int]] = [
    (MT_BBP_CORE(1),  0x00000002),
    (MT_BBP_CORE(4),  0x00000000),
    (MT_BBP_CORE(24), 0x00000000),
    (MT_BBP_CORE(32), 0x4003000a),
    (MT_BBP_CORE(42), 0x00000000),
    (MT_BBP_CORE(44), 0x00000000),
    (MT_BBP_IBI(11),  0x0FDE8081),
    (MT_BBP_AGC(0),   0x00021400),
    (MT_BBP_AGC(1),   0x00000003),
    (MT_BBP_AGC(2),   0x003A6464),
    (MT_BBP_AGC(15),  0x88A28CB8),
    (MT_BBP_AGC(22),  0x00001E21),
    (MT_BBP_AGC(23),  0x0000272C),
    (MT_BBP_AGC(24),  0x00002F3A),
    (MT_BBP_AGC(25),  0x8000005A),
    (MT_BBP_AGC(26),  0x007C2005),
    (MT_BBP_AGC(33),  0x00003238),
    (MT_BBP_AGC(34),  0x000A0C0C),
    (MT_BBP_AGC(37),  0x2121262C),
    (MT_BBP_AGC(41),  0x38383E45),
    (MT_BBP_AGC(57),  0x00001010),
    (MT_BBP_AGC(59),  0xBAA20E96),
    (MT_BBP_AGC(63),  0x00000001),
    (MT_BBP_TXC(0),   0x00280403),
    (MT_BBP_TXC(1),   0x00000000),
    (MT_BBP_RXC(1),   0x00000012),
    (MT_BBP_RXC(2),   0x00000011),
    (MT_BBP_RXC(3),   0x00000005),
    (MT_BBP_RXC(4),   0x00000000),
    (MT_BBP_RXC(5),   0xF977C4EC),
    (MT_BBP_RXC(7),   0x00000090),
    (MT_BBP_TXO(8),   0x00000000),
    (MT_BBP_TXBE(0),  0x00000000),
    (MT_BBP_TXBE(4),  0x00000004),
    (MT_BBP_TXBE(6),  0x00000000),
    (MT_BBP_TXBE(8),  0x00000014),
    (MT_BBP_TXBE(9),  0x20000000),
    (MT_BBP_TXBE(10), 0x00000000),
    (MT_BBP_TXBE(12), 0x00000000),
    (MT_BBP_TXBE(13), 0x00000000),
    (MT_BBP_TXBE(14), 0x00000000),
    (MT_BBP_TXBE(15), 0x00000000),
    (MT_BBP_TXBE(16), 0x00000000),
    (MT_BBP_TXBE(17), 0x00000000),
    (MT_BBP_RXFE(1),  0x00008800),
    (MT_BBP_RXFE(3),  0x00000000),
    (MT_BBP_RXFE(4),  0x00000000),
    (MT_BBP_RXO(13),  0x00000192),
    (MT_BBP_RXO(14),  0x00060612),
    (MT_BBP_RXO(15),  0xC8321B18),
    (MT_BBP_RXO(16),  0x0000001E),
    (MT_BBP_RXO(17),  0x00000000),
    (MT_BBP_RXO(18),  0xCC00A993),
    (MT_BBP_RXO(19),  0xB9CB9CB9),
    (MT_BBP_RXO(20),  0x26c00057),
    (MT_BBP_RXO(21),  0x00000001),
    (MT_BBP_RXO(24),  0x00000006),
    (MT_BBP_RXO(28),  0x0000003F),
]


# [SRC] initvals_init.h:147-157 — 9 entries.
DCOC_TAB: list[tuple[int, int]] = [
    (MT_BBP_CAL(47), 0x000010F0),
    (MT_BBP_CAL(48), 0x00008080),
    (MT_BBP_CAL(49), 0x00000F07),
    (MT_BBP_CAL(50), 0x00000040),
    (MT_BBP_CAL(51), 0x00000404),
    (MT_BBP_CAL(52), 0x00080803),
    (MT_BBP_CAL(53), 0x00000704),
    (MT_BBP_CAL(54), 0x00002828),
    (MT_BBP_CAL(55), 0x00005050),
]


# [SRC] initvals.h:14-83 — 48 entries, each tagged with band/bw mask.
# Format: (bw_band_mask, reg, value). The host filters by:
#   `((RF_G_BAND | RF_BW_20) & item.bw_band) == (RF_G_BAND | RF_BW_20)`
# (or other band/bw combo). On the 2.4 GHz / 20 MHz default we use, exactly
# 20 of these 48 entries qualify — confirmed by [WIRE] capture-2:f465-503.
BBP_SWITCH_TAB: list[tuple[int, int, int]] = [
    (RF_G_BAND | RF_BW_20 | RF_BW_40,         MT_BBP_AGC(4),  0x1FEDA049),
    (RF_A_BAND | RF_BW_20 | RF_BW_40 | RF_BW_80, MT_BBP_AGC(4),  0x1FECA054),

    (RF_G_BAND | RF_BW_20 | RF_BW_40,         MT_BBP_AGC(6),  0x00000045),
    (RF_A_BAND | RF_BW_20 | RF_BW_40 | RF_BW_80, MT_BBP_AGC(6),  0x0000000A),

    (RF_G_BAND | RF_BW_20 | RF_BW_40,         MT_BBP_AGC(8),  0x16344EF0),
    (RF_A_BAND | RF_BW_20 | RF_BW_40 | RF_BW_80, MT_BBP_AGC(8),  0x122C54F2),

    (RF_G_BAND | RF_BW_20,                    MT_BBP_AGC(12), 0x05052879),
    (RF_G_BAND | RF_BW_40,                    MT_BBP_AGC(12), 0x050528F9),
    (RF_A_BAND | RF_BW_20 | RF_BW_40 | RF_BW_80, MT_BBP_AGC(12), 0x050528F9),

    (RF_G_BAND | RF_BW_20 | RF_BW_40,         MT_BBP_AGC(13), 0x35050004),
    (RF_A_BAND | RF_BW_20 | RF_BW_40 | RF_BW_80, MT_BBP_AGC(13), 0x2C3A0406),

    (RF_G_BAND | RF_BW_20 | RF_BW_40,         MT_BBP_AGC(14), 0x310F2E3C),
    (RF_A_BAND | RF_BW_20 | RF_BW_40 | RF_BW_80, MT_BBP_AGC(14), 0x310F2A3F),

    (RF_G_BAND | RF_BW_20 | RF_BW_40,         MT_BBP_AGC(26), 0x007C2005),
    (RF_A_BAND | RF_BW_20 | RF_BW_40 | RF_BW_80, MT_BBP_AGC(26), 0x007C2005),

    (RF_G_BAND | RF_BW_20 | RF_BW_40,         MT_BBP_AGC(27), 0x000000E1),
    (RF_A_BAND | RF_BW_20 | RF_BW_40 | RF_BW_80, MT_BBP_AGC(27), 0x000000EC),

    (RF_G_BAND | RF_BW_20,                    MT_BBP_AGC(28), 0x00060806),
    (RF_G_BAND | RF_BW_40,                    MT_BBP_AGC(28), 0x00050806),
    (RF_A_BAND | RF_BW_40,                    MT_BBP_AGC(28), 0x00060801),
    (RF_A_BAND | RF_BW_20 | RF_BW_80,         MT_BBP_AGC(28), 0x00060806),

    (RF_A_BAND | RF_BW_20 | RF_BW_40 | RF_BW_80, MT_BBP_RXO(28), 0x0000008A),

    (RF_G_BAND | RF_BW_20 | RF_BW_40,         MT_BBP_AGC(31), 0x00000E23),
    (RF_A_BAND | RF_BW_20 | RF_BW_40 | RF_BW_80, MT_BBP_AGC(31), 0x00000E13),

    (RF_G_BAND | RF_BW_20 | RF_BW_40,         MT_BBP_AGC(32), 0x00003218),
    (RF_A_BAND | RF_BW_20 | RF_BW_40 | RF_BW_80, MT_BBP_AGC(32), 0x0000181C),

    (RF_G_BAND | RF_BW_20 | RF_BW_40,         MT_BBP_AGC(33), 0x00003240),
    (RF_A_BAND | RF_BW_20 | RF_BW_40 | RF_BW_80, MT_BBP_AGC(33), 0x00003218),

    (RF_G_BAND | RF_BW_20,                    MT_BBP_AGC(35), 0x11111616),
    (RF_G_BAND | RF_BW_40,                    MT_BBP_AGC(35), 0x11111516),
    (RF_A_BAND | RF_BW_20 | RF_BW_40 | RF_BW_80, MT_BBP_AGC(35), 0x11111111),

    (RF_G_BAND | RF_BW_20,                    MT_BBP_AGC(39), 0x2A2A3036),
    (RF_G_BAND | RF_BW_40,                    MT_BBP_AGC(39), 0x2A2A2C36),
    (RF_A_BAND | RF_BW_20 | RF_BW_40 | RF_BW_80, MT_BBP_AGC(39), 0x2A2A2A2A),

    (RF_G_BAND | RF_BW_20,                    MT_BBP_AGC(43), 0x27273438),
    (RF_G_BAND | RF_BW_40,                    MT_BBP_AGC(43), 0x27272D38),
    (RF_A_BAND | RF_BW_20 | RF_BW_40 | RF_BW_80, MT_BBP_AGC(43), 0x27271A1A),

    (RF_G_BAND | RF_BW_20 | RF_BW_40,         MT_BBP_AGC(51), 0x17171C1C),
    (RF_A_BAND | RF_BW_20 | RF_BW_40 | RF_BW_80, MT_BBP_AGC(51), 0xFFFFFFFF),

    (RF_G_BAND | RF_BW_20,                    MT_BBP_AGC(53), 0x26262A2F),
    (RF_G_BAND | RF_BW_40,                    MT_BBP_AGC(53), 0x2626322F),
    (RF_A_BAND | RF_BW_20 | RF_BW_40 | RF_BW_80, MT_BBP_AGC(53), 0xFFFFFFFF),

    (RF_G_BAND | RF_BW_20 | RF_BW_40,         MT_BBP_AGC(55), 0x40404040),
    (RF_A_BAND | RF_BW_20 | RF_BW_40 | RF_BW_80, MT_BBP_AGC(55), 0xFFFFFFFF),

    (RF_G_BAND | RF_BW_20 | RF_BW_40,         MT_BBP_AGC(58), 0x00001010),
    (RF_A_BAND | RF_BW_20 | RF_BW_40 | RF_BW_80, MT_BBP_AGC(58), 0x00000000),

    (RF_G_BAND | RF_BW_20 | RF_BW_40,         MT_BBP_RXFE(0), 0x3D5000E0),
    (RF_A_BAND | RF_BW_20 | RF_BW_40 | RF_BW_80, MT_BBP_RXFE(0), 0x895000E0),
]


def filter_bbp_switch_tab(want_mask: int) -> list[tuple[int, int]]:
    """Return the (reg, value) pairs from BBP_SWITCH_TAB whose bw_band mask
    matches `want_mask`. Mirrors the kernel:

        if (((RF_G_BAND | RF_BW_20) & item->bw_band) == (RF_G_BAND | RF_BW_20))
            mt76_wr(...)

    [SRC] mt76x0/init.c:97-103. For initial init the kernel passes
    `RF_G_BAND | RF_BW_20`. For per-channel switches the relevant band/bw
    is passed at the call site.
    """
    return [
        (reg, val)
        for bw_band, reg, val in BBP_SWITCH_TAB
        if (bw_band & want_mask) == want_mask
    ]
