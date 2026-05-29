"""MT76x2U power-on + reset + DMA bring-up.

SPDX-License-Identifier: GPL-2.0-or-later
Ported from Linux mt76 (kernel v6.18) by wifit3, 2026.

Mirrors:
  - mt76x2/init.c::mt76x2_reset_wlan + mt76x2_set_wlan_state
  - mt76x2/usb_init.c::mt76x2u_power_on, mt76x2u_power_on_rf,
    mt76x2u_power_on_rf_patch, mt76x2u_init_dma
  - mt76x02_mac.h::mt76x02_wait_for_mac (inline)

Execution order at cold boot:
  reset_wlan(enable=True) -> power_on -> wait_for_mac -> FW upload
  -> wait_for_mac -> init_dma -> mcu_init
"""
from __future__ import annotations

import asyncio
import logging
import time

from .constants import (
    MT_CFG_AD_DA_PWR_DN,
    MT_CFG_BBP_SW_RESET,
    MT_CFG_RF_BG,
    MT_CFG_RF_PATCH_PWR_CTRL_14,
    MT_CFG_RF_PATCH_PWR_CTRL_14C,
    MT_CFG_RF_PATCH_PWR_CTRL_1C,
    MT_CFG_WLAN_FUNC_EN,
    MT_CFG_WLAN_MTC_CTRL,
    MT_MAC_STATUS,
    MT_MAC_STATUS_RX,
    MT_MAC_STATUS_TX,
    MT_USB_DMA_CFG_RX_BULK_AGG_EN,
    MT_USB_DMA_CFG_RX_BULK_EN,
    MT_USB_DMA_CFG_RX_DROP_OR_PAD,
    MT_USB_DMA_CFG_TX_BULK_EN,
    MT_USB_U3DMA_CFG,
    MT_VEND_TYPE_CFG,
    MT_WLAN_FUN_CTRL,
    MT_WLAN_FUN_CTRL_FRC_WL_ANT_SEL,
    MT_WLAN_FUN_CTRL_WLAN_CLK_EN,
    MT_WLAN_FUN_CTRL_WLAN_EN,
    MT_WLAN_FUN_CTRL_WLAN_RESET_RF,
    MT_WLAN_MTC_CTRL_MTCMOS_PWR_UP,
    MT_WLAN_MTC_CTRL_PWR_ACK,
    MT_WLAN_MTC_CTRL_PWR_ACK_S,
    MT_WLAN_MTC_CTRL_STATE_UP,
    MT_WPDMA_GLO_CFG,
    MT_WPDMA_GLO_CFG_RX_DMA_BUSY,
    MT_WPDMA_GLO_CFG_TX_DMA_BUSY,
)
from .transport import MT76x2UTransport

logger = logging.getLogger(__name__)

# MAC_CSR0 lives at 0x1000 on the default bus — wait_for_mac just polls until
# the readback stops returning 0 or 0xFFFFFFFF (chip not yet alive).
_MAC_CSR0 = 0x1000


def _cfg(addr: int) -> int:
    """Virtual-address wrap for CFG-bus registers."""
    return MT_VEND_TYPE_CFG | addr


async def wait_for_mac(transport: MT76x2UTransport,
                       timeout_ms: int = 5000) -> bool:
    """Wait until MAC_CSR0 (0x1000) returns a non-trivial value.

    [SRC] mt76x02_mac.h:149 (mt76x02_wait_for_mac).
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        val = transport.read32(_MAC_CSR0)
        if val not in (0x00000000, 0xFFFFFFFF):
            return True
        if time.monotonic() >= deadline:
            logger.error("wait_for_mac timeout (last MAC_CSR0=0x%08x)", val)
            return False
        await asyncio.sleep(0.008)


async def wait_for_txrx_idle(transport: MT76x2UTransport,
                             timeout_ms: int = 100) -> bool:
    """[SRC] mt76x02.h:253 (poll MAC_STATUS for TX|RX both clear)."""
    deadline = time.monotonic() + timeout_ms / 1000
    mask = MT_MAC_STATUS_TX | MT_MAC_STATUS_RX
    while True:
        val = transport.read32(MT_MAC_STATUS)
        if (val & mask) == 0:
            return True
        if time.monotonic() >= deadline:
            logger.warning("wait_for_txrx_idle timeout (MAC_STATUS=0x%08x)", val)
            return False
        await asyncio.sleep(0.005)


def _rmw(transport: MT76x2UTransport, addr: int, mask: int, value: int) -> None:
    transport.rmw32(addr, mask, value)


def _set(transport: MT76x2UTransport, addr: int, mask: int) -> None:
    transport.rmw32(addr, mask, mask)


def _clear(transport: MT76x2UTransport, addr: int, mask: int) -> None:
    transport.rmw32(addr, mask, 0)


def _set_wlan_state(transport: MT76x2UTransport, enable: bool) -> None:
    """Toggle WLAN_EN + WLAN_CLK_EN. [SRC] mt76x2/init.c:41."""
    val = transport.read32(MT_WLAN_FUN_CTRL)
    bits = MT_WLAN_FUN_CTRL_WLAN_EN | MT_WLAN_FUN_CTRL_WLAN_CLK_EN
    if enable:
        val |= bits
    else:
        val &= ~bits
    transport.write32(MT_WLAN_FUN_CTRL, val)
    time.sleep(0.000020)  # udelay(20)


async def force_power_cycle(transport: MT76x2UTransport) -> None:
    """Hard power-cycle the WLAN block — clears WLAN_EN + WLAN_CLK_EN, waits,
    re-enables.

    Required when recovering from a wedged warm-reattach state where the
    chip's MCU registers (ROM-patch-applied bit, FCE config, etc.) are
    retained from a previous session. A plain `reset_wlan` + `power_on`
    keeps those registers; a true off-then-on cycle clears them so the
    subsequent firmware upload runs as if cold-booting.

    Not present in the kernel because kernel module load always starts
    from a fresh chip; this is a wifit3-specific recovery path for
    wedged warm reattach.
    """
    logger.info("MT7612U: forcing WLAN power cycle (recovery from wedged warm state)")
    _set_wlan_state(transport, False)
    await asyncio.sleep(0.020)
    _set_wlan_state(transport, True)
    await asyncio.sleep(0.020)


def reset_wlan(transport: MT76x2UTransport) -> None:
    """[SRC] mt76x2/init.c:56 — full WLAN reset (always called with enable=True)."""
    val = transport.read32(MT_WLAN_FUN_CTRL)
    val &= ~MT_WLAN_FUN_CTRL_FRC_WL_ANT_SEL

    if val & MT_WLAN_FUN_CTRL_WLAN_EN:
        val |= MT_WLAN_FUN_CTRL_WLAN_RESET_RF
        transport.write32(MT_WLAN_FUN_CTRL, val)
        time.sleep(0.000020)  # udelay(20)
        val &= ~MT_WLAN_FUN_CTRL_WLAN_RESET_RF

    transport.write32(MT_WLAN_FUN_CTRL, val)
    time.sleep(0.000020)

    _set_wlan_state(transport, True)


def _power_on_rf_patch(transport: MT76x2UTransport) -> None:
    """[SRC] mt76x2/usb_init.c:28."""
    _set(transport, _cfg(MT_CFG_RF_BG), (1 << 0) | (1 << 16))
    time.sleep(0.000002)  # udelay(1)

    _clear(transport, _cfg(MT_CFG_RF_PATCH_PWR_CTRL_1C), 0xFF)
    _set(transport, _cfg(MT_CFG_RF_PATCH_PWR_CTRL_1C), 0x30)

    transport.write32(_cfg(MT_CFG_RF_PATCH_PWR_CTRL_14), 0x484F)
    time.sleep(0.000002)

    _set(transport, _cfg(MT_CFG_RF_BG), 1 << 17)
    time.sleep(0.000200)  # usleep_range(150, 200)

    _clear(transport, _cfg(MT_CFG_RF_BG), 1 << 16)
    time.sleep(0.000100)

    _set(transport, _cfg(MT_CFG_RF_PATCH_PWR_CTRL_14C), (1 << 19) | (1 << 20))


def _power_on_rf(transport: MT76x2UTransport, unit: int) -> None:
    """[SRC] mt76x2/usb_init.c:48 — power up RF unit 0 or 1."""
    shift = 8 if unit else 0
    val = ((1 << 1) | (1 << 3) | (1 << 4) | (1 << 5)) << shift

    _set(transport, _cfg(MT_CFG_RF_BG), 1 << shift)
    time.sleep(0.000020)

    _set(transport, _cfg(MT_CFG_RF_BG), val)
    time.sleep(0.000020)

    _clear(transport, _cfg(MT_CFG_RF_BG), 1 << (shift + 2))
    time.sleep(0.000020)

    _power_on_rf_patch(transport)

    # Final per-RF write on default bus reg 0x530.
    _set(transport, 0x530, 0xF)


def power_on(transport: MT76x2UTransport) -> None:
    """Full MT7612U power-on. [SRC] mt76x2/usb_init.c:70 (mt76x2u_power_on).

    Sequence:
      1. WL MTCMOS power up + state poll
      2. clear MTCMOS settling bits in two stages
      3. enable AD/DA + WLAN function + release BBP reset
      4. power up RF unit 0 + 1
    """
    # Turn on WL MTCMOS.
    _set(transport, _cfg(MT_CFG_WLAN_MTC_CTRL), MT_WLAN_MTC_CTRL_MTCMOS_PWR_UP)

    # Poll for MTCMOS state-up + power-ack. Kernel timeout = 1000 iters @ ~10us.
    target = (MT_WLAN_MTC_CTRL_STATE_UP
              | MT_WLAN_MTC_CTRL_PWR_ACK
              | MT_WLAN_MTC_CTRL_PWR_ACK_S)
    deadline = time.monotonic() + 0.100  # 100ms upper bound
    while True:
        cur = transport.read32(_cfg(MT_CFG_WLAN_MTC_CTRL))
        if (cur & target) == target:
            break
        if time.monotonic() >= deadline:
            logger.warning("power_on: MTCMOS state-up poll timed out "
                           "(MTC_CTRL=0x%08x want bits=0x%08x)", cur, target)
            break
        time.sleep(0.000020)

    _clear(transport, _cfg(MT_CFG_WLAN_MTC_CTRL), 0x7F << 16)
    time.sleep(0.000020)

    _clear(transport, _cfg(MT_CFG_WLAN_MTC_CTRL), 0xF << 24)
    time.sleep(0.000020)

    _set(transport, _cfg(MT_CFG_WLAN_MTC_CTRL), 0xF << 24)
    _clear(transport, _cfg(MT_CFG_WLAN_MTC_CTRL), 0xFFF)

    # Turn on AD/DA power down (i.e. clear the power-down bit).
    _clear(transport, _cfg(MT_CFG_AD_DA_PWR_DN), 1 << 3)

    # WLAN function enable + release BBP software reset (both on CFG bus).
    _set(transport, _cfg(MT_CFG_WLAN_FUNC_EN), 1 << 0)
    _clear(transport, _cfg(MT_CFG_BBP_SW_RESET), 1 << 18)

    _power_on_rf(transport, 0)
    _power_on_rf(transport, 1)


def init_dma(transport: MT76x2UTransport) -> None:
    """Post-FW USB DMA config. [SRC] mt76x2/usb_init.c:13 (mt76x2u_init_dma).

    Sets RX_DROP_OR_PAD + bulk-EN flags, CLEARS RX_BULK_AGG_EN so that we
    receive one frame per URB (no aggregation — simpler to parse).
    """
    val = transport.read32(MT_VEND_TYPE_CFG | MT_USB_U3DMA_CFG)
    val |= (MT_USB_DMA_CFG_RX_DROP_OR_PAD
            | MT_USB_DMA_CFG_RX_BULK_EN
            | MT_USB_DMA_CFG_TX_BULK_EN)
    val &= ~MT_USB_DMA_CFG_RX_BULK_AGG_EN
    transport.write32(MT_VEND_TYPE_CFG | MT_USB_U3DMA_CFG, val)


async def wait_for_wpdma_idle(transport: MT76x2UTransport,
                              timeout_ms: int = 100) -> bool:
    """Poll WPDMA_GLO_CFG until both TX_DMA_BUSY + RX_DMA_BUSY are clear.

    [SRC] mt76x2/usb_init.c:140.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    mask = MT_WPDMA_GLO_CFG_TX_DMA_BUSY | MT_WPDMA_GLO_CFG_RX_DMA_BUSY
    while True:
        val = transport.read32(MT_WPDMA_GLO_CFG)
        if (val & mask) == 0:
            return True
        if time.monotonic() >= deadline:
            logger.warning("wait_for_wpdma_idle timeout (WPDMA_GLO_CFG=0x%08x)", val)
            return False
        await asyncio.sleep(0.005)
