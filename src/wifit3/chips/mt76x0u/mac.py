"""MT76x0U MAC bring-up (M3a).

Ports `mt76x0_init_mac_registers` + `mt76x02_wait_for_wpdma` +
`mt76x02_wait_for_txrx_idle` from kernel v6.18.

[SRC] mt76x0/init.c:110-134 (`mt76x0_init_mac_registers`)
[SRC] mt76x02_dma.h:54-60 (`mt76x02_wait_for_wpdma`)
[SRC] mt76x02.h:252-258 (`mt76x02_wait_for_txrx_idle`)

These are the steps `mt76x0_init_hardware` runs in order between
`mt76x0u_load_firmware` (M1) and `mt76x0_init_bbp` (M3b).
"""
from __future__ import annotations

import logging
import time

from .constants import (
    MT_EXT_CCA_CFG,
    MT_FCE_L2_STUFF,
    MT_FCE_L2_STUFF_WR_MPDU_LEN_EN,
    MT_MAC_STATUS,
    MT_MAC_STATUS_RX,
    MT_MAC_STATUS_TX,
    MT_MAC_SYS_CTRL,
    MT_MAC_SYS_CTRL_RESET_BBP,
    MT_MAC_SYS_CTRL_RESET_CSR,
    MT_MCU_MEMMAP_WLAN,
    MT_WMM_CTRL,
    MT_WPDMA_GLO_CFG,
    MT_WPDMA_GLO_CFG_RX_DMA_BUSY,
    MT_WPDMA_GLO_CFG_TX_DMA_BUSY,
)
from .initvals_init import COMMON_MAC_REG_TABLE, MT76X0_MAC_REG_TABLE
from .mcu import MCUChannel
from .transport import MT76x0UTransport

logger = logging.getLogger(__name__)


class MACInitError(RuntimeError):
    """A MAC init step failed (wait_for_wpdma timeout, table upload failure, ...)."""


def wait_for_wpdma(
    transport: MT76x0UTransport, timeout_ms: int = 1000,
) -> bool:
    """`mt76x02_wait_for_wpdma` — poll MT_WPDMA_GLO_CFG until TX_DMA_BUSY
    and RX_DMA_BUSY are both clear.

    [SRC] mt76x02_dma.h:54-60. Kernel uses `__mt76_poll` (per-1ms poll)
    with the given timeout in ms.

    On USB the WPDMA registers may not be meaningful (it's a PCIe-path
    concept), but the kernel still calls this for mt76x0u so we mirror.
    Typically returns immediately on USB. [WIRE] capture-2:f413 (single
    read of 0x0208 — clears on first poll).
    """
    mask = MT_WPDMA_GLO_CFG_TX_DMA_BUSY | MT_WPDMA_GLO_CFG_RX_DMA_BUSY
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        val = transport.read32(MT_WPDMA_GLO_CFG)
        if (val & mask) == 0:
            logger.debug("wait_for_wpdma: WPDMA_GLO_CFG=0x%08x — busy bits clear",
                         val)
            return True
        time.sleep(0.001)
    logger.warning("wait_for_wpdma: timed out after %d ms (last val=0x%08x)",
                   timeout_ms, val)
    return False


def wait_for_txrx_idle(
    transport: MT76x0UTransport, timeout_ms: int = 100,
) -> bool:
    """`mt76x02_wait_for_txrx_idle` — poll MT_MAC_STATUS until TX and RX
    bits are both clear.

    [SRC] mt76x02.h:252-258. Kernel uses `__mt76_poll_msec` (per-10ms poll)
    with timeout 100 ms.
    """
    mask = MT_MAC_STATUS_TX | MT_MAC_STATUS_RX
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        val = transport.read32(MT_MAC_STATUS)
        if (val & mask) == 0:
            logger.debug("wait_for_txrx_idle: MAC_STATUS=0x%08x — TX|RX clear",
                         val)
            return True
        time.sleep(0.010)
    logger.warning("wait_for_txrx_idle: timed out after %d ms (last val=0x%08x)",
                   timeout_ms, val)
    return False


def init_mac_registers(
    transport: MT76x0UTransport, mcu: MCUChannel,
) -> None:
    """Port of `mt76x0_init_mac_registers` (mt76x0/init.c:110-134).

    Steps in kernel order:
      1. RANDOM_WRITE(common_mac_reg_table) — 31 (reg, value) pairs via MCU.
      2. RANDOM_WRITE(mt76x0_mac_reg_table) — 35 pairs via MCU.
      3. mt76_clear(MT_MAC_SYS_CTRL, 0x3) — release CSR+BBP reset.
      4. mt76_set(MT_EXT_CCA_CFG, 0xf000) — set ED_CCA_MASK to 0xF.
      5. mt76_clear(MT_FCE_L2_STUFF, BIT(4)) — disable WR_MPDU_LEN_EN.
      6. mt76_rmw(MT_WMM_CTRL, 0x3ff, 0x201) — WMM RG0/RG1 TXQMA rules.

    The two table writes go through the MCU command channel — the wire
    address is `MT_MCU_MEMMAP_WLAN + reg`. The 4 explicit writes go via
    direct vendor xfers (transport.write32 / set_bits / clear_bits).
    """
    logger.info("init_mac_registers: uploading common_mac_reg_table (%d pairs)",
                len(COMMON_MAC_REG_TABLE))
    mcu.random_write(MT_MCU_MEMMAP_WLAN, COMMON_MAC_REG_TABLE)

    logger.info("init_mac_registers: uploading mt76x0_mac_reg_table (%d pairs)",
                len(MT76X0_MAC_REG_TABLE))
    mcu.random_write(MT_MCU_MEMMAP_WLAN, MT76X0_MAC_REG_TABLE)

    # Step 3: release BBP and MAC reset (clear RESET_CSR | RESET_BBP).
    # Kernel comment: "Release BBP and MAC reset MAC_SYS_CTRL[1:0] = 0x0".
    transport.clear_bits(MT_MAC_SYS_CTRL,
                         MT_MAC_SYS_CTRL_RESET_CSR | MT_MAC_SYS_CTRL_RESET_BBP)

    # Step 4: set MT_EXT_CCA_CFG[15:12] = 0xF (ED_CCA_MASK).
    # Kernel comment: "Set 0x141C[15:12]=0xF".
    transport.set_bits(MT_EXT_CCA_CFG, 0xf000)

    # Step 5: disable MT_FCE_L2_STUFF_WR_MPDU_LEN_EN.
    transport.clear_bits(MT_FCE_L2_STUFF, MT_FCE_L2_STUFF_WR_MPDU_LEN_EN)

    # Step 6: RMW MT_WMM_CTRL — clear bits 0..9, then set value 0x201.
    # Kernel uses `mt76_rmw(reg, mask, val)` = (cur & ~mask) | (val & mask).
    val = transport.read32(MT_WMM_CTRL)
    val = (val & ~0x3ff) | (0x201 & 0x3ff)
    transport.write32(MT_WMM_CTRL, val)

    logger.info("init_mac_registers: done (%d table writes + 4 explicit writes)",
                len(COMMON_MAC_REG_TABLE) + len(MT76X0_MAC_REG_TABLE))
