"""RTL8188EUS silent-reset status check — the DBG_CONFIG_ERROR_DETECT slot of the 2 s tick.

``rtw_dynamic_chk_wk_hdl`` [SRC] rtw_cmd.c:2737 runs every ~2 s and, before the phydm
watchdog, polls the TX/RX DMA + FW status registers to detect a wedged chip
(DBG_CONFIG_ERROR_DETECT / _RESET are on: include/autoconf.h:296,298):

    rtl8188e_sreset_xmit_status_check    [SRC] rtl8188e_sreset.c:22 -> R REG_TXDMA_STATUS
    rtl8188e_sreset_linked_status_check  [SRC] rtl8188e_sreset.c:75 -> R REG_RXDMA_STATUS,
                                                                       R REG_FMETHR

Neither check returns early or aborts the tick: the phydm watchdog always follows
(see driver._dig_watchdog; the verify dispatches both at each tick).
"""
from __future__ import annotations

import logging

from . import constants as C

logger = logging.getLogger(__name__)


def sreset_reset() -> None:
    """``rtw_hal_sreset_reset`` -> ``sreset_reset`` [SRC] rtw_sreset.c:288. CONFIG_POWER_SAVING=n
    (Makefile:60) compiles out the _ips_enter/_ips_leave power cycle, leaving only the OS
    adapter stop/start (netif queue, timers, xmit tasklet [SRC] rtw_sreset.c:235-286): no
    register I/O in monitor mode (the network restore needs _FW_LINKED, rtw_sreset.c:274)."""
    logger.warning("RTL8188EUS sreset_reset (no-op on the wire in this build)")


def status_check(t) -> None:
    """One ``rtw_dynamic_chk_wk_hdl`` silent-reset poll: TXDMA + RXDMA + FW status."""
    # rtl8188e_sreset_xmit_status_check [SRC] rtl8188e_sreset.c:32-36.
    txdma = t.read32(C.REG_TXDMA_STATUS)
    if txdma not in (0x00, C.TXDMA_STATUS_IF_GONE):
        logger.warning("rtl8188e_sreset_xmit_status_check REG_TXDMA_STATUS:0x%08x", txdma)
        sreset_reset()

    # rtl8188e_sreset_linked_status_check: a latched RX-DMA status is written back to clear
    # it (no reset); FW status is read for diagnostics only (efuse-fail / cond-no-match).
    rxdma = t.read32(C.REG_RXDMA_STATUS)
    if rxdma != 0x00:
        t.write32(C.REG_RXDMA_STATUS, rxdma)
    t.read8(C.REG_FMETHR)
