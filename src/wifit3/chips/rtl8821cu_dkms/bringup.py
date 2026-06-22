"""RTL8821CU cold bring-up sequence — the canonical init the byte-for-byte gate verifies.

Milestone 1 is power-on only: the HALMAC card-enable flow (CARDDIS->CARDEMU->ACT) that
ends when the chip ACKs MAC-ready (the POLLING of 0x06 BIT(1) inside CARDEMU_TO_ACT).
Later milestones (chip-id/EFUSE, firmware download, MAC/BB/RF init, monitor entry) extend
``cold_bringup`` past this point.
"""
from __future__ import annotations

from . import pwrseq


def power_on(t) -> None:
    """Run the HALMAC card-enable flow. [SRC] hal_halmac.c rtw_hal_power_on -> the
    8821c card_en_flow (halmac_pwr_seq_8821c.c:338)."""
    pwrseq.run_pwr_seq(t, pwrseq.CARD_EN_FLOW)


def cold_bringup(t) -> None:
    """The full cold init the driver's connect() runs. Milestone 1: power-on only."""
    power_on(t)
