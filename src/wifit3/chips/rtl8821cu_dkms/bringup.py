"""RTL8821CU cold bring-up sequence — the canonical init the byte-for-byte gate verifies.

Milestone 1 = the cold-boot prologue + power-on, in wire order:
  1. halmac mount chip-detect  (SYS_CFG2 / SYS_CFG1+1)         — chipid.mount_get_chip_info
  2. chip-version read         (SYS_CFG1 / SYS_STATUS1 / 0x68) — chipid.read_chip_version
  3. EFUSE dump                (autoload + the 512-byte 0x30 loop) — efuse.read_efuse
  4. power-on                  (HALMAC card-enable flow)       — power_on
Later milestones (firmware download, MAC/BB/RF init, monitor entry) extend ``cold_bringup``
past power-on.
"""
from __future__ import annotations

from . import chipid, efuse, pwrseq


def power_on(t) -> None:
    """Run the HALMAC card-enable flow. [SRC] hal_halmac.c rtw_hal_power_on -> the
    8821c card_en_flow (halmac_pwr_seq_8821c.c:338)."""
    pwrseq.run_pwr_seq(t, pwrseq.CARD_EN_FLOW)


def cold_bringup(t) -> None:
    """The cold init the driver's connect() runs, in the order the wire shows.

    Milestone 1 ends here — the chip-id/EFUSE prologue verifies byte-for-byte. A short
    post-EFUSE init block (wire ops ~2068-2105, starting with a re-read of 0x68) precedes
    ``power_on`` (the card-enable flow, already ported in ``pwrseq``); porting that block is
    the next milestone.
    """
    chipid.mount_get_chip_info(t)
    chipid.read_chip_version(t)
    efuse.read_efuse(t)
