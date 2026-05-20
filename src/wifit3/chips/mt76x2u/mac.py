"""MT76x2U MAC bring-up: reset, init-values, setaddr, start.

SPDX-License-Identifier: GPL-2.0-or-later
Ported from Linux mt76 (kernel v6.18) by wifit3, 2026.

Mirrors:
  - mt76x2/init.c::mt76_write_mac_initvals (the big static reg_pair table)
  - mt76x2/usb_mac.c::mt76x2u_mac_reset
  - mt76x02_mac.c::mt76x02_mac_setaddr
  - mt76x02_usb_core.c::mt76x02u_mac_start

Per-port deviations from kernel:
  - We skip mt76x2u_mac_fixup_xtal (EEPROM-dependent XTAL trim). Defaults
    on this silicon (rev E4) work for RX.
  - We skip wcid + key table resets (not needed for monitor RX).
  - We do NOT call mt76x02_wait_for_txrx_idle after mac_reset — kernel does,
    but we just rely on the MAC_SYS_CTRL=0 in the initvals table to leave
    everything stopped until mac_start.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time

from .constants import (
    MT_AMPDU_MAX_LEN_20M1S,
    MT_AMPDU_MAX_LEN_20M2S,
    MT_AUTO_RSP_CFG,
    MT_AUX_CLK_CFG,
    MT_BKOFF_SLOT_CFG,
    MT_BKOFF_SLOT_CFG_CC_DELAY_MASK,
    MT_BKOFF_SLOT_CFG_CC_DELAY_SHIFT,
    MT_CCK_PROT_CFG,
    MT_COEXCFG0,
    MT_COEXCFG0_COEX_EN,
    MT_DACCLK_EN_DLY_CFG,
    MT_EFUSE_CTRL,
    MT_EXT_CCA_CFG,
    MT_EXP_ACK_TIME,
    MT_FCE_L2_STUFF,
    MT_FCE_L2_STUFF_WR_MPDU_LEN_EN,
    MT_FCE_PDMA_GLOBAL_CONF,
    MT_FCE_PSE_CTRL,
    MT_FCE_SKIP_FS,
    MT_FCE_WLAN_FLOW_CONTROL1,
    MT_GF20_PROT_CFG,
    MT_GF40_PROT_CFG,
    MT_HEADER_TRANS_CTRL_REG,
    MT_HT_BASIC_RATE,
    MT_HT_CTRL_CFG,
    MT_HT_FBK_TO_LEGACY,
    MT_LEGACY_BASIC_RATE,
    MT_MAC_ADDR_DW0,
    MT_MAC_ADDR_DW1,
    MT_MAC_BSSID_DW0,
    MT_MAC_BSSID_DW1,
    MT_MAC_STATUS,
    MT_MAC_STATUS_RX,
    MT_MAC_STATUS_TX,
    MT_MAC_SYS_CTRL,
    MT_MAC_SYS_CTRL_ENABLE_RX,
    MT_MAC_SYS_CTRL_ENABLE_TX,
    MT_MAC_SYS_CTRL_RESET_BBP,
    MT_MAC_SYS_CTRL_RESET_CSR,
    MT_MAX_LEN_CFG,
    MT_MM20_PROT_CFG,
    MT_MM40_PROT_CFG,
    MT_OFDM_PROT_CFG,
    MT_PAUSE_ENABLE_CONTROL1,
    MT_PBF_CFG,
    MT_PBF_RX_MAX_PCNT,
    MT_PBF_SYS_CTRL,
    MT_PBF_TX_MAX_PCNT,
    MT_PIFS_TX_CFG,
    MT_PN_PAD_MODE,
    MT_PROT_AUTO_TX_CFG,
    MT_PWR_PIN_CFG,
    MT_RX_FILTR_CFG,
    MT_TBTT_SYNC_CFG,
    MT_TSO_CTRL,
    MT_TX_ALC_CFG_4,
    MT_TX_ALC_VGA3,
    MT_TX_LINK_CFG,
    MT_TX_PROT_CFG6,
    MT_TX_PROT_CFG7,
    MT_TX_PROT_CFG8,
    MT_TX_PWR_CFG_0,
    MT_TX_PWR_CFG_1,
    MT_TX_PWR_CFG_2,
    MT_TX_PWR_CFG_3,
    MT_TX_PWR_CFG_4,
    MT_TX_PWR_CFG_7,
    MT_TX_PWR_CFG_8,
    MT_TX_PWR_CFG_9,
    MT_TX_RETRY_CFG,
    MT_TX_RTS_CFG,
    MT_TX_SW_CFG0,
    MT_TX_SW_CFG1,
    MT_TX_SW_CFG2,
    MT_TX_SW_CFG3,
    MT_TX_TIMEOUT_CFG,
    MT_TXOP_CTRL_CFG,
    MT_TXOP_HLDR_ET,
    MT_US_CYC_CFG,
    MT_US_CYC_CNT_MASK,
    MT_VHT_HT_FBK_CFG1,
    MT_WMM_AIFSN,
    MT_WMM_CWMAX,
    MT_WMM_CWMIN,
    MT_WPDMA_DELAY_INT_CFG,
    MT_WPDMA_GLO_CFG,
    MT_XIFS_TIME_CFG,
    MT_XIFS_TIME_CFG_OFDM_SIFS_MASK,
    MT_XIFS_TIME_CFG_OFDM_SIFS_SHIFT,
)
from .transport import MT76x2UTransport

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# mt76_write_mac_initvals — [SRC] mt76x2/init.c:83
# 53 register writes + 6 protection-cfg writes. Copied verbatim from the
# kernel struct mt76_reg_pair `vals[]` array. Values are mediatek-reference
# defaults the chip needs to operate.
# ---------------------------------------------------------------------------

# Protection-config helper values — [SRC] mt76x2/init.c:85-107
# MT_PROT_CFG_RATE = GENMASK(15, 0); NAV = bit 18 region; CTRL = bit 16-17;
# TXOP_ALLOW = bits 19-24; RTS_THRESH = BIT(26)
def _prot_cfg(rate: int, nav: int, txop_allow: int,
              rts_thresh: bool, ctrl: int = 0) -> int:
    val = rate & 0xFFFF
    val |= (ctrl & 0x3) << 16
    val |= (nav & 0x3) << 18
    val |= (txop_allow & 0x3F) << 20
    if rts_thresh:
        val |= 1 << 26
    return val


_DEFAULT_PROT_CFG_CCK = _prot_cfg(0x0003, 1, 0x3F, True)
_DEFAULT_PROT_CFG_OFDM = _prot_cfg(0x2004, 1, 0x3F, True)
_DEFAULT_PROT_CFG_20 = _prot_cfg(0x2004, 1, 0x17, False, ctrl=1)
_DEFAULT_PROT_CFG_40 = _prot_cfg(0x2084, 1, 0x3F, False, ctrl=1)


_MAC_INITVALS = (
    (MT_PBF_SYS_CTRL,              0x00080c00),
    (MT_PBF_CFG,                   0x1efebcff),
    (MT_FCE_PSE_CTRL,              0x00000001),
    (MT_MAC_SYS_CTRL,              0x00000000),
    (MT_MAX_LEN_CFG,               0x003e3f00),
    (MT_AMPDU_MAX_LEN_20M1S,       0xaaa99887),
    (MT_AMPDU_MAX_LEN_20M2S,       0x000000aa),
    (MT_XIFS_TIME_CFG,             0x33a40d0a),
    (MT_BKOFF_SLOT_CFG,            0x00000209),
    (MT_TBTT_SYNC_CFG,             0x00422010),
    (MT_PWR_PIN_CFG,               0x00000000),
    (0x1238,                       0x001700c8),
    (MT_TX_SW_CFG0,                0x00101001),
    (MT_TX_SW_CFG1,                0x00010000),
    (MT_TX_SW_CFG2,                0x00000000),
    (MT_TXOP_CTRL_CFG,             0x0400583f),
    (MT_TX_RTS_CFG,                0x00ffff20),
    (MT_TX_TIMEOUT_CFG,            0x000a2290),
    (MT_TX_RETRY_CFG,              0x47f01f0f),
    (MT_EXP_ACK_TIME,              0x002c00dc),
    (MT_TX_PROT_CFG6,              0xe3f42004),
    (MT_TX_PROT_CFG7,              0xe3f42084),
    (MT_TX_PROT_CFG8,              0xe3f42104),
    (MT_PIFS_TX_CFG,               0x00060fff),
    (MT_RX_FILTR_CFG,              0x00015f97),
    (MT_LEGACY_BASIC_RATE,         0x0000017f),
    (MT_HT_BASIC_RATE,             0x00004003),
    (MT_PN_PAD_MODE,               0x00000003),
    (MT_TXOP_HLDR_ET,              0x00000002),
    (0x0a44,                       0x00000000),
    (MT_HEADER_TRANS_CTRL_REG,     0x00000000),
    (MT_TSO_CTRL,                  0x00000000),
    (MT_AUX_CLK_CFG,               0x00000000),
    (MT_DACCLK_EN_DLY_CFG,         0x00000000),
    (MT_TX_ALC_CFG_4,              0x00000000),
    (MT_TX_ALC_VGA3,               0x00000000),
    (MT_TX_PWR_CFG_0,              0x3a3a3a3a),
    (MT_TX_PWR_CFG_1,              0x3a3a3a3a),
    (MT_TX_PWR_CFG_2,              0x3a3a3a3a),
    (MT_TX_PWR_CFG_3,              0x3a3a3a3a),
    (MT_TX_PWR_CFG_4,              0x3a3a3a3a),
    (MT_TX_PWR_CFG_7,              0x3a3a3a3a),
    (MT_TX_PWR_CFG_8,              0x0000003a),
    (MT_TX_PWR_CFG_9,              0x0000003a),
    (MT_EFUSE_CTRL,                0x0000d000),
    (MT_PAUSE_ENABLE_CONTROL1,     0x0000000a),
    (MT_FCE_WLAN_FLOW_CONTROL1,    0x60401c18),
    (MT_WPDMA_DELAY_INT_CFG,       0x94ff0000),
    (MT_TX_SW_CFG3,                0x00000004),
    (MT_HT_FBK_TO_LEGACY,          0x00001818),
    (MT_VHT_HT_FBK_CFG1,           0xedcba980),
    (MT_PROT_AUTO_TX_CFG,          0x00830083),
    (MT_HT_CTRL_CFG,               0x000001ff),
    (MT_TX_LINK_CFG,               0x00001020),
    # Protection-cfg block
    (MT_CCK_PROT_CFG,              _DEFAULT_PROT_CFG_CCK),
    (MT_OFDM_PROT_CFG,             _DEFAULT_PROT_CFG_OFDM),
    (MT_MM20_PROT_CFG,             _DEFAULT_PROT_CFG_20),
    (MT_MM40_PROT_CFG,             _DEFAULT_PROT_CFG_40),
    (MT_GF20_PROT_CFG,             _DEFAULT_PROT_CFG_20),
    (MT_GF40_PROT_CFG,             _DEFAULT_PROT_CFG_40),
)


async def mac_reset(transport: MT76x2UTransport) -> bool:
    """Full MAC reset + initvals load.

    [SRC] mt76x2/usb_mac.c:62 (mt76x2u_mac_reset).
    """
    transport.write32(MT_WPDMA_GLO_CFG, (1 << 4) | (1 << 5))
    transport.write32(MT_PBF_TX_MAX_PCNT, 0xefef3f1f)
    transport.write32(MT_PBF_RX_MAX_PCNT, 0x0000febf)

    for addr, val in _MAC_INITVALS:
        transport.write32(addr, val)

    # Post-table overrides (these override entries in the initvals).
    transport.write32(MT_TX_LINK_CFG, 0x1020)
    transport.write32(MT_AUTO_RSP_CFG, 0x13)
    transport.write32(MT_MAX_LEN_CFG, 0x2f00)
    transport.write32(MT_WMM_AIFSN, 0x2273)
    transport.write32(MT_WMM_CWMIN, 0x2344)
    transport.write32(MT_WMM_CWMAX, 0x34aa)

    # Clear MAC reset bits (initvals leaves MAC_SYS_CTRL=0; this RMW is a
    # safety net in case the chip leaked any reset bits across the table).
    transport.rmw32(
        MT_MAC_SYS_CTRL,
        MT_MAC_SYS_CTRL_RESET_CSR | MT_MAC_SYS_CTRL_RESET_BBP,
        0,
    )

    # MT7612 disables BT coexistence at the MAC layer.
    transport.rmw32(MT_COEXCFG0, MT_COEXCFG0_COEX_EN, 0)

    transport.rmw32(MT_EXT_CCA_CFG, 0xf000, 0xf000)
    transport.rmw32(MT_TX_ALC_CFG_4, 1 << 31, 0)

    _xtal_fixup_minimal(transport)

    # Stir US_CYC_CFG.CNT to 0x1e (the kernel does this after init_hardware).
    transport.rmw32(MT_US_CYC_CFG, MT_US_CYC_CNT_MASK, 0x1e)
    transport.write32(MT_TXOP_CTRL_CFG, 0x583f)
    return True


def _xtal_fixup_minimal(transport: MT76x2UTransport) -> None:
    """Non-EEPROM pieces of mt76x2u_mac_fixup_xtal.

    [SRC] mt76x2/usb_mac.c:36 — the writes that DO NOT depend on EEPROM
    XTAL_TRIM_1/2 values. We skip the XO_CTRL5/6 trim itself and the
    NIC_CONF_2-keyed XO_CTRL7 write. The remaining writes still matter for
    RX (SIFS, slot-time, FCE L2 stuffing).
    """
    # 0x504 / 0x50c: kernel writes these without comment. From context
    # (between XTAL trim and SIFS adjust) they look like MAC-engine
    # housekeeping. Magic values verbatim from kernel.
    transport.write32(0x504, 0x06000000)
    transport.write32(0x50c, 0x08800000)
    time.sleep(0.005)   # mdelay(5)
    transport.write32(0x504, 0x00000000)

    # Decrease OFDM SIFS 16us -> 13us.
    transport.rmw32(
        MT_XIFS_TIME_CFG,
        MT_XIFS_TIME_CFG_OFDM_SIFS_MASK,
        (0xD << MT_XIFS_TIME_CFG_OFDM_SIFS_SHIFT) & MT_XIFS_TIME_CFG_OFDM_SIFS_MASK,
    )

    # BKOFF slot CC_DELAY = 1.
    transport.rmw32(
        MT_BKOFF_SLOT_CFG,
        MT_BKOFF_SLOT_CFG_CC_DELAY_MASK,
        (0x1 << MT_BKOFF_SLOT_CFG_CC_DELAY_SHIFT) & MT_BKOFF_SLOT_CFG_CC_DELAY_MASK,
    )

    # Clear FCE_L2_STUFF.WR_MPDU_LEN_EN (BIT 4).
    transport.rmw32(MT_FCE_L2_STUFF, MT_FCE_L2_STUFF_WR_MPDU_LEN_EN, 0)


def mac_setaddr(transport: MT76x2UTransport, mac_bytes: bytes) -> None:
    """Write MAC into hardware registers.

    [SRC] mt76x02_mac.c::mt76x02_mac_setaddr (loops over 6 bytes -> ADDR_DW0/1).
    """
    if len(mac_bytes) != 6:
        raise ValueError(f"MAC must be 6 bytes, got {len(mac_bytes)}")
    dw0 = struct.unpack("<I", mac_bytes[:4])[0]
    dw1 = struct.unpack("<H", mac_bytes[4:6])[0]  # upper 16 bits = 0
    transport.write32(MT_MAC_ADDR_DW0, dw0)
    transport.write32(MT_MAC_ADDR_DW1, dw1)
    # BSSID identical to MAC for STA/monitor (kernel does this too).
    transport.write32(MT_MAC_BSSID_DW0, dw0)
    transport.write32(MT_MAC_BSSID_DW1, dw1)


async def mac_start(transport: MT76x2UTransport,
                    rxfilter: int = 0x00015f97,
                    monitor: bool = True) -> bool:
    """Enable TX (always) + RX. [SRC] mt76x02_usb_core.c::mt76x02u_mac_start.

    In monitor mode we open the RX filter further than the kernel default:
    clear DROP_UC_NOME (bit 2) so frames addressed to other STAs make it
    through, and DROP_NOT_MYBSSID (bit 3) so non-matching BSSID frames
    survive. This is the mt76 analog of [[station-vs-monitor-rcr]] for rtw88.
    """
    # Always enable TX first per kernel order.
    transport.write32(MT_MAC_SYS_CTRL, MT_MAC_SYS_CTRL_ENABLE_TX)

    # Wait for WPDMA idle (TX_DMA_BUSY|RX_DMA_BUSY clear). Kernel uses 200ms.
    from .power import wait_for_wpdma_idle
    if not await wait_for_wpdma_idle(transport, timeout_ms=200):
        logger.warning("mac_start: WPDMA never went idle (continuing anyway)")

    if monitor:
        # Clear DROP_UC_NOME (BIT 2) + DROP_NOT_MYBSSID (BIT 3) for monitor.
        rxfilter &= ~((1 << 2) | (1 << 3))
    transport.write32(MT_RX_FILTR_CFG, rxfilter)

    transport.write32(MT_MAC_SYS_CTRL,
                      MT_MAC_SYS_CTRL_ENABLE_TX | MT_MAC_SYS_CTRL_ENABLE_RX)

    # Brief settle.
    await asyncio.sleep(0.005)
    return True


async def mac_stop(transport: MT76x2UTransport) -> None:
    """Clear ENABLE_TX|RX. Lightweight stop — full kernel `mt76x2u_mac_stop`
    drains queues etc. but for our close() path this is sufficient.
    """
    transport.rmw32(
        MT_MAC_SYS_CTRL,
        MT_MAC_SYS_CTRL_ENABLE_TX | MT_MAC_SYS_CTRL_ENABLE_RX,
        0,
    )
    await asyncio.sleep(0.005)
