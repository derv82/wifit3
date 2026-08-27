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
    MT76X02_CIPHER_NONE,
    MT_EXT_CCA_CFG,
    MT_FCE_L2_STUFF,
    MT_FCE_L2_STUFF_WR_MPDU_LEN_EN,
    MT_MAC_ADDR_DW0,
    MT_MAC_ADDR_DW1,
    MT_MAC_ADDR_DW1_U2ME_MASK,
    MT_MAC_APC_BSSID_H,
    MT_MAC_APC_BSSID_H_ADDR_MASK,
    MT_MAC_APC_BSSID_L,
    MT_MAC_BSSID_DW0,
    MT_MAC_BSSID_DW1,
    MT_MAC_BSSID_DW1_MBEACON_N_MASK,
    MT_MAC_BSSID_DW1_MBEACON_N_SHIFT,
    MT_MAC_BSSID_DW1_MBSS_LOCAL_BIT,
    MT_MAC_BSSID_DW1_MBSS_MODE_SHIFT,
    MT_MAC_STATUS,
    MT_MAC_STATUS_RX,
    MT_MAC_STATUS_TX,
    MT_MAC_SYS_CTRL,
    MT_MAC_SYS_CTRL_RESET_BBP,
    MT_MAC_SYS_CTRL_RESET_CSR,
    MT_MCU_MEMMAP_WLAN,
    MT_SKEY,
    MT_SKEY_MODE,
    MT_SKEY_MODE_MASK,
    MT_SKEY_MODE_SHIFT,
    MT_WCID_ADDR,
    MT_WCID_ATTR,
    MT_WCID_ATTR_BSS_IDX_EXT,
    MT_WCID_ATTR_BSS_IDX_SHIFT,
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
    logger.debug("init_mac_registers: uploading common_mac_reg_table (%d pairs)",
                len(COMMON_MAC_REG_TABLE))
    mcu.random_write(MT_MCU_MEMMAP_WLAN, COMMON_MAC_REG_TABLE)

    logger.debug("init_mac_registers: uploading mt76x0_mac_reg_table (%d pairs)",
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

    logger.debug("init_mac_registers: done (%d table writes + 4 explicit writes)",
                len(COMMON_MAC_REG_TABLE) + len(MT76X0_MAC_REG_TABLE))


# ---------------------------------------------------------------------------
# M3c: per-vif BSSID + MAC writes + key/WCID clearing.
# ---------------------------------------------------------------------------

def mac_set_bssid(
    transport: MT76x0UTransport, idx: int, addr: bytes,
) -> None:
    """`mt76x02_mac_set_bssid` — write one of 8 per-vif BSSID slots.

    [SRC] mt76x02_mac.c:1232-1238. The kernel masks `idx &= 7` so only 8
    unique slots exist; the outer 16-iteration loop writes each slot twice.
    """
    if len(addr) < 6:
        raise MACInitError(f"mac_set_bssid: addr must be 6 bytes, got {len(addr)}")
    idx &= 7
    lo = int.from_bytes(addr[:4], "little")
    hi = int.from_bytes(addr[4:6], "little") & MT_MAC_APC_BSSID_H_ADDR_MASK
    transport.write32(MT_MAC_APC_BSSID_L(idx), lo)
    # `mt76_rmw_field` for the H register only updates ADDR field bits 0-15.
    cur = transport.read32(MT_MAC_APC_BSSID_H(idx))
    new = (cur & ~MT_MAC_APC_BSSID_H_ADDR_MASK) | hi
    transport.write32(MT_MAC_APC_BSSID_H(idx), new)


def mac_setaddr(transport: MT76x0UTransport, mac: bytes) -> None:
    """`mt76x02_mac_setaddr` — [SRC] mt76x02_mac.c:727-758.

    Writes the 6-byte `mac` to MT_MAC_ADDR_DW0/DW1 and MT_MAC_BSSID_DW0/DW1,
    then loops 16× through `mac_set_bssid` clearing per-vif BSSID slots.

    Per the kernel: BSSID_DW1 also gets MBSS_MODE=3 (8 APs + 8 STAs),
    MBSS_LOCAL_BIT, and an RMW-set of MBEACON_N=7.
    """
    if len(mac) != 6:
        raise MACInitError(f"mac_setaddr: mac must be 6 bytes, got {len(mac)}")

    mac_lo = int.from_bytes(mac[:4], "little")
    mac_hi = int.from_bytes(mac[4:6], "little")

    # MT_MAC_ADDR_DW0/DW1 — DW1 also carries U2ME_MASK = 0xff.
    transport.write32(MT_MAC_ADDR_DW0, mac_lo)
    transport.write32(MT_MAC_ADDR_DW1, mac_hi | MT_MAC_ADDR_DW1_U2ME_MASK)

    # MT_MAC_BSSID_DW0/DW1 — DW1 also carries MBSS config.
    transport.write32(MT_MAC_BSSID_DW0, mac_lo)
    bssid_dw1 = (
        mac_hi
        | (3 << MT_MAC_BSSID_DW1_MBSS_MODE_SHIFT)   # MBSS_MODE = 3
        | MT_MAC_BSSID_DW1_MBSS_LOCAL_BIT
    )
    transport.write32(MT_MAC_BSSID_DW1, bssid_dw1)

    # rmw_field(MT_MAC_BSSID_DW1, MBEACON_N, 7).
    cur = transport.read32(MT_MAC_BSSID_DW1)
    new = (cur & ~MT_MAC_BSSID_DW1_MBEACON_N_MASK) | (7 << MT_MAC_BSSID_DW1_MBEACON_N_SHIFT)
    transport.write32(MT_MAC_BSSID_DW1, new)

    # Clear all 16 per-vif BSSID slots (kernel loops 16x; first 8 are unique).
    null_addr = b"\x00" * 6
    for i in range(16):
        mac_set_bssid(transport, i, null_addr)


def clear_shared_keys(transport: MT76x0UTransport) -> None:
    """Port of `for (i=0..16; for k=0..4) mt76x02_mac_shared_key_setup(NULL)`.

    [SRC] mt76x02_mac.c:58-77 (`mt76x02_mac_shared_key_setup`) when called
    with key=NULL: cipher=MT76X02_CIPHER_NONE=0, key_data=32 zero bytes.

    Per (vif_idx, key_idx) iteration:
      1. RMW MT_SKEY_MODE(vif_idx): clear the 4 cipher bits for this key.
      2. Write 32 zero bytes to MT_SKEY(vif_idx, key_idx) (= 8× u32 writes).

    For 16 vifs × 4 keys = 64 iterations × 10 transactions = ~640 transactions.
    """
    logger.debug("clear_shared_keys: 16 vifs × 4 keys = 64 entries")
    zero32 = b"\x00" * 32
    for vif_idx in range(16):
        skey_mode_reg = MT_SKEY_MODE(vif_idx)
        for key_idx in range(4):
            # mt76_rr + mask-out cipher field for this key_idx + mt76_wr.
            shift = MT_SKEY_MODE_SHIFT(vif_idx, key_idx)
            val = transport.read32(skey_mode_reg)
            val &= ~(MT_SKEY_MODE_MASK << shift)
            val |= MT76X02_CIPHER_NONE << shift   # = 0
            transport.write32(skey_mode_reg, val)
            # mt76_wr_copy(MT_SKEY(...), zero_data, 32) → 8× u32 writes.
            skey_base = MT_SKEY(vif_idx, key_idx)
            for word_i in range(8):
                word_val = int.from_bytes(
                    zero32[word_i * 4: word_i * 4 + 4], "little"
                )
                transport.write32(skey_base + word_i * 4, word_val)
    logger.debug("clear_shared_keys: done")


def clear_wcids(transport: MT76x0UTransport) -> None:
    """Port of `for (i=0..256) mt76x02_mac_wcid_setup(i, 0, NULL)`.

    [SRC] mt76x02_mac.c:148-167 (`mt76x02_mac_wcid_setup`) when called with
    vif_idx=0, mac=NULL:
      - attr = (BSS_IDX=0, BSS_IDX_EXT=0) = 0.
      - mt76_wr(MT_WCID_ATTR(idx), 0).
      - If idx < 128: write zero mt76_wcid_addr (8 bytes = 2× u32) to
        MT_WCID_ADDR(idx).

    256 iterations × 1-3 transactions each = ~512 transactions.
    """
    logger.debug("clear_wcids: 256 entries")
    for idx in range(256):
        # attr = 0 (BSS_IDX=0, no BSS_IDX_EXT bit, etc.)
        attr = (
            ((0 & 7) << MT_WCID_ATTR_BSS_IDX_SHIFT)
            | (0 & MT_WCID_ATTR_BSS_IDX_EXT)
        )
        transport.write32(MT_WCID_ATTR(idx), attr)
        if idx < 128:
            # mt76_wr_copy(MT_WCID_ADDR(idx), zero_addr, 8) → 2× u32 writes.
            transport.write32(MT_WCID_ADDR(idx), 0)
            transport.write32(MT_WCID_ADDR(idx) + 4, 0)
    logger.debug("clear_wcids: done")
