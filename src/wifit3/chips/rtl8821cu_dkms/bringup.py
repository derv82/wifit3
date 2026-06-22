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

from . import btc, chipid, efuse, init, pwrseq


def power_on(t, info) -> None:
    """[SRC] rtw_hal_power_on hal_intf.c:461 — `hal_power_on` (pre-init system config, the
    card-enable power switch, the post-switch system config) then, when the card reports BT
    present, the BT-coex power-on setting (hal_intf.c:470, gated on EEPROMBluetoothCoexist)."""
    init.pre_init_system_cfg(t)
    pwrseq.mac_pwr_switch(t, power_on=True)
    init.init_system_cfg(t)
    if info.bt_coexist:
        btc.power_on_setting(t, info.rfe_type, info.single_ant_path)


def cold_bringup(t) -> None:
    """The cold init the driver's connect() runs, in the order the wire shows.

    Through power-on: the chip-id/EFUSE prologue, the pre-power-on system config, the
    card-enable power sequence, and (combo card) the BT-coex power-on setting. Later milestones
    (firmware download, MAC/BB/RF init, monitor entry) extend this past power-on.
    """
    chipid.mount_get_chip_info(t)
    chipid.read_chip_version(t)
    info = efuse.read_efuse(t)
    power_on(t, info)
