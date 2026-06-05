"""RTL8812AU M1 bring-up — thin wrapper over the shared 88xxA firmware mechanics.

Supplies the three chip-varying inputs the base ``firmware.bring_up`` needs: the 8812
NIC firmware blob, the 8812 power-on flow (cut=ALL, no LDO quirk — see ``pwrseq``), and
the 8812 TX page boundary. The download mechanics (LLT, page writes, FW-ready poll) are
the family-shared base code.
"""
from __future__ import annotations

import time
from pathlib import Path

from ..rtl88xxau_base import firmware as base_fw
from ..rtl88xxau_base import registers as R
from ..rtl88xxau_base.pwrseq import PWR_CUT_ALL_MSK, PWR_FAB_ALL_MSK, PWR_INTF_USB_MSK
from .constants import REG_RF_B_CTRL_8812, TX_PAGE_BOUNDARY_8812
from .pwrseq import CARD_ENABLE_FLOW

FW_BIN = Path(__file__).parent / "assets" / "rtl8812au_fw.bin"


def load_firmware_blob() -> bytes:
    """The 8812a NIC firmware (array_mp_8812a_fw_nic), 32-byte header included."""
    return FW_BIN.read_bytes()


def _hal_init_preamble(t) -> None:
    """[SRC] rtl8812au_hal_init (usb_halinit.c:1416-1448) — the steps before power-on.

    A warm-MAC probe (REG_SYS_CLKR+1 / REG_CR; SW-only, never changes our always-power-on
    path), then a both-path RF reset (5->7 on REG_RF_CTRL path A and REG_RF_B_CTRL_8812
    path B; the 1T1R 8821 skips path B), then rtl8812au_hw_reset — which gates its whole
    body on REG_MCUFWDL bit7 (RAM-download-done). On a cold boot that bit is clear, so the
    reset collapses to the single MCUFWDL read.
    """
    t.read8(R.REG_SYS_CLKR + 1)             # 0x09  warm-MAC check (decision is SW-only)
    t.read8(R.REG_CR)                        # 0x100/1 warm-MAC check
    t.write8(R.REG_RF_CTRL, 5)               # path-A RF reset
    t.write8(R.REG_RF_CTRL, 7)
    t.write8(REG_RF_B_CTRL_8812, 5)          # path-B RF reset (2T2R only)
    t.write8(REG_RF_B_CTRL_8812, 7)
    t.read8(R.REG_MCUFWDL)                    # rtl8812au_hw_reset: bit7 clear -> skip reset


def bring_up(t, fw_blob: bytes, delay=time.sleep) -> bool:
    """Full M1 for the 8812au: hal-init preamble (warm probe + RF reset) -> power-on
    (Rtl8812_NIC_ENABLE_FLOW, cut=ALL, no LDO quirk) -> LLT -> drop-bulkout -> FW
    download -> FW-ready."""
    _hal_init_preamble(t)
    return base_fw.bring_up(
        t, fw_blob, CARD_ENABLE_FLOW, TX_PAGE_BOUNDARY_8812,
        cut=PWR_CUT_ALL_MSK, fab=PWR_FAB_ALL_MSK, intf=PWR_INTF_USB_MSK,
        ldo_quirk=False, delay=delay, reset_8051_bit=R.BIT(3))
