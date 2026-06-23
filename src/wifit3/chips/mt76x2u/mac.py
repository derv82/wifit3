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
    MT_BCN_BYPASS_MASK,
    MT_BCN_OFFSET_BASE,
    MT_BEACON_TIME_CFG,
    MT_BEACON_TIME_CFG_BEACON_TX,
    MT_BEACON_TIME_CFG_SYNC_MODE_MASK,
    MT_BEACON_TIME_CFG_TBTT_EN,
    MT_BEACON_TIME_CFG_TIMER_EN,
    MT_CCK_PROT_CFG,
    MT_CH_BUSY,
    MT_CH_CCA_RC_EN,
    MT_CH_IDLE,
    MT_CH_TIME_CFG,
    MT_CH_TIME_CFG_CH_TIMER_CLR_MASK,
    MT_CH_TIME_CFG_CH_TIMER_CLR_SHIFT,
    MT_CH_TIME_CFG_EIFS_AS_BUSY,
    MT_CH_TIME_CFG_NAV_AS_BUSY,
    MT_CH_TIME_CFG_RX_AS_BUSY,
    MT_CH_TIME_CFG_TIMER_EN,
    MT_CH_TIME_CFG_TX_AS_BUSY,
    MT_COEXCFG0,
    MT_COEXCFG0_COEX_EN,
    MT_EE_NIC_CONF_2,
    MT_EE_NIC_CONF_2_XTAL_OPTION_MASK,
    MT_EE_NIC_CONF_2_XTAL_OPTION_SHIFT,
    MT_EE_XTAL_TRIM_1,
    MT_EE_XTAL_TRIM_2,
    MT_MAC_ADDR_DW1_U2ME_MASK,
    MT_MAC_APC_BSSID_BASE,
    MT_MAC_APC_BSSID_H_ADDR_MASK,
    MT_MAC_BSSID_DW1_MBEACON_N_MASK,
    MT_MAC_BSSID_DW1_MBEACON_N_SHIFT,
    MT_MAC_BSSID_DW1_MBSS_LOCAL_BIT,
    MT_MAC_BSSID_DW1_MBSS_MODE_MASK,
    MT_MAC_BSSID_DW1_MBSS_MODE_SHIFT,
    MT_MAC_STATUS,
    MT_MAC_STATUS_RX,
    MT_MAC_STATUS_TX,
    MT_VEND_TYPE_CFG,
    N_BCN_SLOTS,
    MT_XO_CTRL5,
    MT_XO_CTRL5_C2_VAL_MASK,
    MT_XO_CTRL5_C2_VAL_SHIFT,
    MT_XO_CTRL6,
    MT_XO_CTRL6_C2_CTRL_MASK,
    MT_XO_CTRL7,
    MT_DACCLK_EN_DLY_CFG,
    MT_EFUSE_CTRL,
    MT_EXT_CCA_CFG,
    MT_EXP_ACK_TIME,
    MT_FCE_L2_STUFF,
    MT_FCE_L2_STUFF_WR_MPDU_LEN_EN,
    MT_FCE_PSE_CTRL,
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
from .eeprom import read_u16
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

    _mac_fixup_xtal(transport)

    # Stir US_CYC_CFG.CNT to 0x1e (the kernel does this after init_hardware).
    transport.rmw32(MT_US_CYC_CFG, MT_US_CYC_CNT_MASK, 0x1e)
    transport.write32(MT_TXOP_CTRL_CFG, 0x583f)
    return True


def _compute_xtal_trim(trim_2: int, trim_1_byte: int) -> tuple[int, int]:
    """`mt76x2u_mac_fixup_xtal` EEPROM math — [SRC] mt76x2/usb_mac.c:11-29.

    Returns ``(c2_val, offset)`` with c2_val pre-clamped to 7 bits. The
    kernel then writes ``c2_val + offset`` into the XO_CTRL5.C2_VAL field.

      offset:
        - low byte == 0xff → 0 (uninitialized)
        - else: bits 0-6 of low byte, negated if bit 7 set (sign bit)
      c2_val:
        - high byte of TRIM_2
        - if high byte is 0x00 or 0xff: fall back to TRIM_1 low byte
        - if THAT is also 0x00 or 0xff: use 0x14 (kernel default)
        - finally clamp to low 7 bits
    """
    low = trim_2 & 0xFF
    if low == 0xFF:
        offset = 0
    else:
        magnitude = low & 0x7F
        offset = -magnitude if (low & 0x80) else magnitude

    high = (trim_2 >> 8) & 0xFF
    if high == 0x00 or high == 0xFF:
        high = trim_1_byte & 0xFF
        if high == 0x00 or high == 0xFF:
            high = 0x14
    c2_val = high & 0x7F
    return (c2_val, offset)


def _mac_fixup_xtal(transport: MT76x2UTransport) -> None:
    """`mt76x2u_mac_fixup_xtal` — [SRC] mt76x2/usb_mac.c:9-60.

    Kernel port. Reads two EEPROM bytes for the per-board XTAL
    trim, programs CFG-bus MT_XO_CTRL5.C2_VAL + MT_XO_CTRL6.C2_CTRL, runs
    the four MAC-engine housekeeping writes around 0x504/0x50c, sets the
    OFDM SIFS / slot CC_DELAY / FCE L2 stuff bits, and conditionally
    writes MT_XO_CTRL7 based on EEPROM `NIC_CONF_2.XTAL_OPTION`.

    Without the XTAL trim writes, the chip's reference oscillator runs at
    the silicon default rather than the per-board calibrated frequency —
    small frequency offsets accumulate into clock drift that the AP sees
    as frame-mistime / FCS errors.
    """
    # Read EEPROM trim bytes (kernel mt76x2u/usb_mac.c:14, 24).
    trim_2 = read_u16(transport, MT_EE_XTAL_TRIM_2)
    trim_1 = read_u16(transport, MT_EE_XTAL_TRIM_1) & 0xFF
    c2_val, offset = _compute_xtal_trim(trim_2, trim_1)

    # CFG-bus XO_CTRL5: RMW C2_VAL = (c2_val + offset).
    transport.rmw32(
        MT_VEND_TYPE_CFG | MT_XO_CTRL5,
        MT_XO_CTRL5_C2_VAL_MASK,
        (((c2_val + offset) & 0x7F) << MT_XO_CTRL5_C2_VAL_SHIFT)
        & MT_XO_CTRL5_C2_VAL_MASK,
    )

    # CFG-bus XO_CTRL6: SET all bits of C2_CTRL (kernel `mt76_set`).
    transport.rmw32(
        MT_VEND_TYPE_CFG | MT_XO_CTRL6,
        MT_XO_CTRL6_C2_CTRL_MASK,
        MT_XO_CTRL6_C2_CTRL_MASK,
    )

    # MAC-engine housekeeping around 0x504/0x50c (default bus).
    # [SRC] mt76x2/usb_mac.c:36-39.
    transport.write32(0x504, 0x06000000)
    transport.write32(0x50c, 0x08800000)
    time.sleep(0.005)   # mdelay(5)
    transport.write32(0x504, 0x00000000)

    # Decrease OFDM SIFS 16us -> 13us. [SRC] mt76x2/usb_mac.c:42-43.
    transport.rmw32(
        MT_XIFS_TIME_CFG,
        MT_XIFS_TIME_CFG_OFDM_SIFS_MASK,
        (0xD << MT_XIFS_TIME_CFG_OFDM_SIFS_SHIFT) & MT_XIFS_TIME_CFG_OFDM_SIFS_MASK,
    )

    # BKOFF slot CC_DELAY = 1. [SRC] mt76x2/usb_mac.c:44.
    transport.rmw32(
        MT_BKOFF_SLOT_CFG,
        MT_BKOFF_SLOT_CFG_CC_DELAY_MASK,
        (0x1 << MT_BKOFF_SLOT_CFG_CC_DELAY_SHIFT) & MT_BKOFF_SLOT_CFG_CC_DELAY_MASK,
    )

    # Clear FCE_L2_STUFF.WR_MPDU_LEN_EN (BIT 4). [SRC] mt76x2/usb_mac.c:47.
    transport.rmw32(MT_FCE_L2_STUFF, MT_FCE_L2_STUFF_WR_MPDU_LEN_EN, 0)

    # Conditional XO_CTRL7 write — kernel switch on NIC_CONF_2.XTAL_OPTION.
    # [SRC] mt76x2/usb_mac.c:49-59. Options 0/1 program a fixed value;
    # option >= 2 leaves XO_CTRL7 alone. Unlike XO_CTRL5/6 above, the kernel
    # writes XO_CTRL7 on the DEFAULT bus (`mt76_wr`), not the CFG bus.
    nic_conf_2 = read_u16(transport, MT_EE_NIC_CONF_2)
    xtal_option = (
        (nic_conf_2 & MT_EE_NIC_CONF_2_XTAL_OPTION_MASK)
        >> MT_EE_NIC_CONF_2_XTAL_OPTION_SHIFT
    )
    if xtal_option == 0:
        transport.write32(MT_XO_CTRL7, 0x5C1FEE80)
    elif xtal_option == 1:
        transport.write32(MT_XO_CTRL7, 0x5C1FEED0)


def mac_set_bssid(transport: MT76x2UTransport, idx: int, addr: bytes) -> None:
    """`mt76x02_mac_set_bssid` — [SRC] mt76x02_mac.c:1232-1238.

    Programs one per-vif BSSID slot (8 slots total; `idx &= 7` masks the
    caller's value down to 0-7). Writes the low 4 bytes to APC_BSSID_L
    and RMW's the high 2 bytes into the ADDR field of APC_BSSID_H,
    preserving the EN/control bits in the upper half of the H register.
    """
    if len(addr) != 6:
        raise ValueError(f"mac_set_bssid: addr must be 6 bytes, got {len(addr)}")
    slot = idx & 0x7
    base = MT_MAC_APC_BSSID_BASE + slot * 8
    lo = struct.unpack("<I", addr[:4])[0]
    hi = struct.unpack("<H", addr[4:6])[0]
    transport.write32(base, lo)
    transport.rmw32(base + 4, MT_MAC_APC_BSSID_H_ADDR_MASK, hi)


def mac_setaddr(transport: MT76x2UTransport, mac_bytes: bytes) -> None:
    """`mt76x02_mac_setaddr` — [SRC] mt76x02_mac.c:727-758.

    Kernel port. Writes ADDR_DW0/DW1 (with U2ME_MASK=0xff in
    DW1's high byte), BSSID_DW0/DW1 (with MBSS_MODE=3 + MBSS_LOCAL_BIT
    in DW1's upper bits), then RMW's MBEACON_N=7 onto BSSID_DW1, then
    clears all 8 per-vif APC_BSSID slots via 16 iterations of
    `mac_set_bssid(i, null)` (kernel loops 0..15 but each call masks
    `idx &= 7`, so slots get cleared twice — kernel-verbatim).
    """
    if len(mac_bytes) != 6:
        raise ValueError(f"MAC must be 6 bytes, got {len(mac_bytes)}")
    dw0 = struct.unpack("<I", mac_bytes[:4])[0]
    dw1_mac = struct.unpack("<H", mac_bytes[4:6])[0]

    # MAC_ADDR: low DW + high DW with U2ME_MASK=0xff in bits 23:16.
    transport.write32(MT_MAC_ADDR_DW0, dw0)
    transport.write32(
        MT_MAC_ADDR_DW1,
        dw1_mac | MT_MAC_ADDR_DW1_U2ME_MASK,
    )

    # BSSID DW: low DW = MAC low. High DW = mac high 2 bytes |
    # (MBSS_MODE=3 << 16) | MBSS_LOCAL_BIT. The MBEACON_N field is then
    # RMW'd to 7 in a second write (matches kernel two-step exactly).
    transport.write32(MT_MAC_BSSID_DW0, dw0)
    bssid_dw1 = (
        dw1_mac
        | ((3 << MT_MAC_BSSID_DW1_MBSS_MODE_SHIFT)
           & MT_MAC_BSSID_DW1_MBSS_MODE_MASK)
        | MT_MAC_BSSID_DW1_MBSS_LOCAL_BIT
    )
    transport.write32(MT_MAC_BSSID_DW1, bssid_dw1)
    transport.rmw32(
        MT_MAC_BSSID_DW1,
        MT_MAC_BSSID_DW1_MBEACON_N_MASK,
        (7 << MT_MAC_BSSID_DW1_MBEACON_N_SHIFT)
        & MT_MAC_BSSID_DW1_MBEACON_N_MASK,
    )

    # Clear all per-vif BSSID slots — kernel runs 16 iterations even
    # though only 8 slots exist (idx &= 7 inside set_bssid), so each
    # slot gets cleared twice. Kernel-verbatim.
    null_addr = b"\x00" * 6
    for i in range(16):
        mac_set_bssid(transport, i, null_addr)


async def wait_for_txrx_idle(transport: MT76x2UTransport,
                             timeout_ms: int = 100) -> bool:
    """`mt76x02_wait_for_txrx_idle` — [SRC] mt76x02.h:252-258.

    Polls MT_MAC_STATUS waiting for both TX and RX activity bits to clear,
    100 ms timeout. Kernel calls this between `mac_setaddr` and the 256-
    iter WCID reset loop ([SRC] usb_init.c:162) — without it, our WCID +
    SKEY clears may race with the chip's in-flight TX/RX state, leaving
    stale data in wcid 0xff (the slot we use for inject TX) and producing
    the "first fake-auth fails, retry succeeds" symptom.

    Returns True if idle reached within ``timeout_ms``, False on timeout.
    Kernel uses 1 ms poll interval; we match.
    """
    mask = MT_MAC_STATUS_TX | MT_MAC_STATUS_RX
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    while True:
        val = transport.read32(MT_MAC_STATUS)
        if (val & mask) == 0:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.001)


def mac_cc_reset(transport: MT76x2UTransport) -> None:
    """`mt76x02_mac_cc_reset` — [SRC] mt76x02_mac.c:1213-1229.

    Programs the channel-time counters and clears the busy/idle accumulators.
    Kernel calls this on every channel change (from mt76x2u_set_channel).

    The full value is:
      TIMER_EN | TX_AS_BUSY | RX_AS_BUSY | NAV_AS_BUSY | EIFS_AS_BUSY |
      CCA_RC_EN | (CH_TIMER_CLR=1)
    """
    val = (
        MT_CH_TIME_CFG_TIMER_EN
        | MT_CH_TIME_CFG_TX_AS_BUSY
        | MT_CH_TIME_CFG_RX_AS_BUSY
        | MT_CH_TIME_CFG_NAV_AS_BUSY
        | MT_CH_TIME_CFG_EIFS_AS_BUSY
        | MT_CH_CCA_RC_EN
        | ((1 << MT_CH_TIME_CFG_CH_TIMER_CLR_SHIFT)
           & MT_CH_TIME_CFG_CH_TIMER_CLR_MASK)
    )
    transport.write32(MT_CH_TIME_CFG, val)
    # Read-and-clear channel busy/idle counters.
    transport.read32(MT_CH_BUSY)
    transport.read32(MT_CH_IDLE)


def init_beacon_config(transport: MT76x2UTransport) -> None:
    """`mt76x02_init_beacon_config` — [SRC] mt76x02_beacon.c:205-213.

    Kernel init even though wifit3 never TX's beacons:
      - CLEAR BEACON_TIME_CFG.{TIMER_EN | TBTT_EN | BEACON_TX}
      - SET   BEACON_TIME_CFG.SYNC_MODE
      - WRITE BCN_BYPASS_MASK = 0xFFFF
      - WRITE the 4 BCN_OFFSET regs with the per-slot offsets.
    """
    transport.rmw32(
        MT_BEACON_TIME_CFG,
        (MT_BEACON_TIME_CFG_TIMER_EN
         | MT_BEACON_TIME_CFG_TBTT_EN
         | MT_BEACON_TIME_CFG_BEACON_TX),
        0,
    )
    transport.rmw32(
        MT_BEACON_TIME_CFG,
        MT_BEACON_TIME_CFG_SYNC_MODE_MASK,
        MT_BEACON_TIME_CFG_SYNC_MODE_MASK,
    )
    transport.write32(MT_BCN_BYPASS_MASK, 0xFFFF)
    _set_beacon_offsets(transport)


def _set_beacon_offsets(transport: MT76x2UTransport) -> None:
    """`mt76x02_set_beacon_offsets` — [SRC] mt76x02_beacon.c:10-22.

    With ``nslots = N_BCN_SLOTS = 5`` and
    ``slot_size = (8192 / 5) & ~63 = 1600``, the 5 slot offsets pack into 4
    u32 registers (one byte per slot, value = offset/64):

      slot 0: 0      / 64 =   0
      slot 1: 1600   / 64 =  25 (0x19)
      slot 2: 3200   / 64 =  50 (0x32)
      slot 3: 4800   / 64 =  75 (0x4B)
      slot 4: 6400   / 64 = 100 (0x64)

      regs[0] = 0x00 | (0x19 << 8) | (0x32 << 16) | (0x4B << 24) = 0x4B321900
      regs[1] = 0x64
      regs[2] = 0
      regs[3] = 0
    """
    slot_size = (8192 // N_BCN_SLOTS) & ~63
    regs = [0, 0, 0, 0]
    for i in range(N_BCN_SLOTS):
        val = (i * slot_size) // 64
        regs[i // 4] |= (val & 0xFF) << (8 * (i % 4))
    for i in range(4):
        transport.write32(MT_BCN_OFFSET_BASE + i * 4, regs[i])


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
