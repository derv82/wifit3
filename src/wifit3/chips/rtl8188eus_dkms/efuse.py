"""RTL8188EUS IOL (initial-offload) engine + efuse patch.

The 8188e build runs with ``rtw_fw_iol = 1`` (IOL always enabled), so the MCU
performs efuse reads / LLT init / efuse patches as offloaded command lists rather
than the host doing byte-by-byte register I/O. The host primitives are:

  ``iol_mode_enable`` [SRC] rtl8188e_hal_init.c:26 — toggle SW_OFFLOAD_EN in
  REG_SYS_CFG (0xF0[7]).
  ``iol_execute``     [SRC] rtl8188e_hal_init.c:49 — write the command bits to
  REG_HMEBOX_E0 (0x88), poll until they clear, then check the matching error bit.

``iol_efuse_patch`` (the HAL_INIT_STAGES_EFUSE_PATCH stage) runs READ_EFUSE_MAP
then EFUSE_PATCH. The probe-phase efuse map read (which recovers crystal_cap /
tx-power / MAC) additionally reads the map back from the TX packet buffer — that
is a later milestone; this module ports the IOL core + patch first.
"""
from __future__ import annotations

from .constants import (
    CMD_EFUSE_PATCH,
    CMD_READ_EFUSE_MAP,
    REG_HMEBOX_E0,
    REG_SYS_CFG,
    SW_OFFLOAD_EN,
)

_IOL_POLL_CAP = 100000   # generous bound; the captured poll always converges


def iol_mode_enable(t, enable: bool) -> None:
    """Toggle initial-offload (SW_OFFLOAD_EN) in REG_SYS_CFG. [SRC] iol_mode_enable.
    (The bFWReady==FALSE 8051-reset branch is skipped: FW is ready post-M1.)"""
    reg = t.read8(REG_SYS_CFG)
    if enable:
        t.write8(REG_SYS_CFG, reg | SW_OFFLOAD_EN)
    else:
        t.write8(REG_SYS_CFG, reg & ~SW_OFFLOAD_EN)


def iol_execute(t, control: int) -> bool:
    """Trigger an MCU command and wait for it to clear. [SRC] iol_execute.

    Writes ``control`` into REG_HMEBOX_E0, polls until those bits clear, then reads
    once more for the status (the command bit clear AND its <<4 error bit clear)."""
    control &= 0x0F
    reg = t.read8(REG_HMEBOX_E0)
    t.write8(REG_HMEBOX_E0, reg | control)
    for _ in range(_IOL_POLL_CAP):
        reg = t.read8(REG_HMEBOX_E0)
        if not (reg & control):
            break
    reg = t.read8(REG_HMEBOX_E0)                     # final status read
    return not (reg & control) and not (reg & (control << 4))


def iol_efuse_patch(t) -> bool:
    """``rtl8188e_iol_efuse_patch`` (HAL_INIT_STAGES_EFUSE_PATCH) [SRC]
    rtl8188e_hal_init.c:422 — read the efuse map into the MCU, then apply patches."""
    iol_mode_enable(t, True)
    ok = iol_execute(t, CMD_READ_EFUSE_MAP)
    if ok:
        ok = iol_execute(t, CMD_EFUSE_PATCH)
    iol_mode_enable(t, False)
    return ok
