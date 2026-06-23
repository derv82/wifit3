"""RTL8822BU TX-beamforming / MU-MIMO HW init (the post-`odm_dm_init` step in `rtl8822b_init`).

`rtl8822b_phy_bf_init` `[SRC] rtl8822b_phy.c:1974` seeds the MU-MIMO / sounding control registers to
their defaults: MU TX control (retry limit, MU disabled until sounding), the WMAC MU-BF option/control,
the NDPA rate/BW source, the fixed-6M CSI rate, and the grouping bitmap. The driver never sounds in
monitor mode (no association), so this is one-time default seeding only — but it is part of the cold
wire sequence the gate reproduces, so it lives here as a port of that sequence. Only the live data (the actual
MU sounding/precoding) would arrive once TX/association is wired; that is out of scope for passive RX.
"""
from __future__ import annotations


def phy_bf_init(t) -> None:
    """[SRC] rtl8822b_phy_bf_init (rtl8822b_phy.c:1974) — MU-MIMO/sounding default seed.

    Mirrors the source's access widths exactly: REG_MU_TX_CTL is one read32 → field-edits in software
    → one write32 (NOT four RMW); REG_TXBF_CTRL+3 and 0x6DF are 1-byte RMW."""
    # REG_MU_TX_CTL (0x14C0): P1-aggr wait-state (BIT16), MU retry limit [15:12]=0xA,
    # disable Tx MU-MIMO until sounding (clear BIT7), clear MU-STA table validity [5:0]=0.
    v = t.read32(0x14C0)
    v |= 1 << 16
    v = (v & ~0xF000) | (0xA << 12)
    v &= ~(1 << 7)
    v &= ~0x3F
    t.write32(0x14C0, v & 0xFFFFFFFF)
    # MU-MIMO option/control defaults: ACKPOLICY(3) | ACKPOLICY_EN -> 0x70; control = 0.
    t.write8(0x167C, 0x70)                              # REG_WMAC_MU_BF_OPTION
    t.write16(0x1680, 0x0000)                           # REG_WMAC_MU_BF_CTL
    # MU NDPA rate & BW source: REG_TXBF_CTRL+3 (0x42F) BIT(6) = use 0x45F (not Tx desc);
    # REG_NDPA_OPT_CTRL (0x45F) = 0x10 (rate OFDM-6M, BW20).
    v8 = t.read8(0x042F)                               # BIT_USE_NDPA_PARAMETER (BIT30>>24 = BIT6)
    t.write8(0x042F, (v8 | (1 << 6)) & 0xFF)
    t.write8(0x045F, 0x10)
    # STA2's CSI rate fixed at 6M: 0x6DF = (cur & 0xC0) | 0x4. Grouping bitmap: 0x1C94 = 0xAFFFAFFF.
    v8 = t.read8(0x06DF)
    t.write8(0x06DF, (v8 & 0xC0) | 0x04)
    t.write32(0x1C94, 0xAFFFAFFF)
