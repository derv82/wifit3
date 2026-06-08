"""RTL8188EUS silent-reset status check — the DBG_CONFIG_ERROR_DETECT slot of the 2 s tick.

``rtw_dynamic_chk_wk_hdl`` [SRC] rtw_cmd.c:2737 runs every ~2 s and, before the phydm
watchdog, polls the TX/RX DMA + FW status registers to detect a wedged chip:

    rtl8188e_sreset_xmit_status_check    [SRC] rtl8188e_sreset.c:22 -> R REG_TXDMA_STATUS
    rtl8188e_sreset_linked_status_check  [SRC] rtl8188e_sreset.c:75 -> R REG_RXDMA_STATUS,
                                                                       R REG_FMETHR

In healthy operation all three read clean, so the burst is three reads and no recovery.
The recovery path (``rtw_hal_sreset_reset`` — a full chip re-init) only fires on a real
DMA hang; it is deferred behind a clear guard, never silently dropped. This is the same 2 s
timer as the phydm watchdog, so the driver runs it in the same tick (see driver._dig_watchdog)
and the verify dispatches it at each tick alongside ``dig.watchdog_tick``.
"""
from __future__ import annotations

from . import constants as C


def status_check(t) -> None:
    """One ``rtw_dynamic_chk_wk_hdl`` silent-reset poll: TXDMA + RXDMA + FW status."""
    # rtl8188e_sreset_xmit_status_check: TX-DMA wedged -> rtw_hal_sreset_reset.
    txdma = t.read32(C.REG_TXDMA_STATUS)
    if txdma not in (0x00, C.TXDMA_STATUS_IF_GONE):
        raise NotImplementedError(
            f"8188e sreset TX-DMA recovery (rtw_hal_sreset_reset) deferred "
            f"(REG_TXDMA_STATUS=0x{txdma:08x})")

    # rtl8188e_sreset_linked_status_check: a latched RX-DMA status is written back to clear
    # it (no reset); FW status is read for diagnostics only (efuse-fail / cond-no-match).
    rxdma = t.read32(C.REG_RXDMA_STATUS)
    if rxdma != 0x00:
        t.write32(C.REG_RXDMA_STATUS, rxdma)
    t.read8(C.REG_FMETHR)
