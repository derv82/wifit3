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

from . import chipid, efuse, init, pwrseq


def power_on(t) -> None:
    """HALMAC power-on: pre-init system config, then the card-enable power switch.
    [SRC] rtw_halmac_poweron hal_halmac.c:2701 (pre_init_system_cfg -> _power_switch ON)."""
    init.pre_init_system_cfg(t)
    pwrseq.mac_pwr_switch(t, power_on=True)


def cold_bringup(t) -> None:
    """The cold init the driver's connect() runs, in the order the wire shows.

    Through power-on: the chip-id/EFUSE prologue, the pre-power-on system config, and the
    card-enable power sequence. Later milestones (firmware download, MAC/BB/RF init, monitor
    entry) extend this past power-on.
    """
    chipid.mount_get_chip_info(t)
    chipid.read_chip_version(t)
    efuse.read_efuse(t)
    power_on(t)
