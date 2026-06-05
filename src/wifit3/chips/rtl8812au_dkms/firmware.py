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
from ..rtl88xxau_base.pwrseq import PWR_CUT_ALL_MSK, PWR_FAB_ALL_MSK, PWR_INTF_USB_MSK
from .constants import TX_PAGE_BOUNDARY_8812
from .pwrseq import CARD_ENABLE_FLOW

FW_BIN = Path(__file__).parent / "assets" / "rtl8812au_fw.bin"


def load_firmware_blob() -> bytes:
    """The 8812a NIC firmware (array_mp_8812a_fw_nic), 32-byte header included."""
    return FW_BIN.read_bytes()


def bring_up(t, fw_blob: bytes, delay=time.sleep) -> bool:
    """Full M1 for the 8812au: power-on (Rtl8812_NIC_ENABLE_FLOW, cut=ALL, no LDO
    quirk) -> LLT (boundary 0xF9) -> drop-bulkout -> FW download -> FW-ready."""
    return base_fw.bring_up(
        t, fw_blob, CARD_ENABLE_FLOW, TX_PAGE_BOUNDARY_8812,
        cut=PWR_CUT_ALL_MSK, fab=PWR_FAB_ALL_MSK, intf=PWR_INTF_USB_MSK,
        ldo_quirk=False, delay=delay)
