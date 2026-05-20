"""MT76x2U PHY init + channel programming primitives.

SPDX-License-Identifier: GPL-2.0-or-later
Ported from Linux mt76 (kernel v6.18) by wifit3, 2026.

Mirrors:
  - mt76x02_phy.c::mt76x02_phy_set_rxpath / mt76x02_phy_set_txdac
  - mt76x02_phy.c::mt76x02_phy_set_band   / mt76x02_phy_set_bw
  - mt76x2/mcu.c::mt76x2_mcu_set_channel  / mt76x2_mcu_init_gain
  - mt76x2/mcu.c::mt76x2_mcu_load_cr
"""
from __future__ import annotations

import logging
import struct

from .constants import (
    MT_BBP_AGC_R0,
    MT_BBP_AGC_R0_BW_MASK,
    MT_BBP_AGC_R0_BW_SHIFT,
    MT_BBP_AGC_R0_CTRL_CHAN_MASK,
    MT_BBP_AGC_R0_CTRL_CHAN_SHIFT,
    MT_BBP_CORE_R1,
    MT_BBP_CORE_R1_BW_MASK,
    MT_BBP_CORE_R1_BW_SHIFT,
    MT_BBP_TXBE_R5,
    MT_TX_BAND_CFG,
    MT_TX_BAND_CFG_2G,
    MT_TX_BAND_CFG_5G,
    MT_TX_BAND_CFG_UPPER_40M,
)
from .mcu import (
    CMD_INIT_GAIN_OP,
    CMD_LOAD_CR,
    CMD_SWITCH_CHANNEL_OP,
    McuChannel,
)
from .transport import MT76x2UTransport

logger = logging.getLogger(__name__)

# We can't constants.MT_BBP_TXBE_R0 because that constant doesn't exist in our
# constants module. Define it locally.
MT_BBP_TXBE_R0_VAL = 0x2700  # MT_BBP_TXBE_BASE + 0 * 4


def phy_set_rxpath(transport: MT76x2UTransport, chainmask: int) -> None:
    """[SRC] mt76x02_phy.c:12 — chainmask-dependent BBP AGC R0 toggle."""
    val = transport.read32(MT_BBP_AGC_R0)
    val &= ~(1 << 4)
    if (chainmask & 0xF) == 2:
        val |= 1 << 3
    else:
        val &= ~(1 << 3)
    transport.write32(MT_BBP_AGC_R0, val)
    # Force a follow-up read (memory barrier in kernel).
    _ = transport.read32(MT_BBP_AGC_R0)


def phy_set_txdac(transport: MT76x2UTransport, chainmask: int) -> None:
    """[SRC] mt76x02_phy.c:34 — chainmask-dependent BBP TXBE R5 toggle."""
    txpath = (chainmask >> 8) & 0xF
    if txpath == 2:
        transport.rmw32(MT_BBP_TXBE_R5, 0x3, 0x3)
    else:
        transport.rmw32(MT_BBP_TXBE_R5, 0x3, 0)


def phy_set_band(transport: MT76x2UTransport, band_5g: bool,
                 primary_upper: bool = False) -> None:
    """[SRC] mt76x02_phy.c:150."""
    if not band_5g:
        # 2.4 GHz: set 2G, clear 5G.
        cur = transport.read32(MT_TX_BAND_CFG)
        cur = (cur | MT_TX_BAND_CFG_2G) & ~MT_TX_BAND_CFG_5G
    else:
        # 5 GHz: clear 2G, set 5G.
        cur = transport.read32(MT_TX_BAND_CFG)
        cur = (cur | MT_TX_BAND_CFG_5G) & ~MT_TX_BAND_CFG_2G

    if primary_upper:
        cur |= MT_TX_BAND_CFG_UPPER_40M
    else:
        cur &= ~MT_TX_BAND_CFG_UPPER_40M
    transport.write32(MT_TX_BAND_CFG, cur)


def phy_set_bw_20mhz(transport: MT76x2UTransport, ctrl: int = 0) -> None:
    """[SRC] mt76x02_phy.c:124 — width=20MHz default branch.

    For 20MHz: core_val=0, agc_val=1. `ctrl` = upper/lower extension marker
    (irrelevant for 20MHz but the kernel writes it anyway).
    """
    core_val = 0
    agc_val = 1

    transport.rmw32(MT_BBP_CORE_R1,
                    MT_BBP_CORE_R1_BW_MASK,
                    (core_val << MT_BBP_CORE_R1_BW_SHIFT) & MT_BBP_CORE_R1_BW_MASK)
    transport.rmw32(MT_BBP_AGC_R0,
                    MT_BBP_AGC_R0_BW_MASK,
                    (agc_val << MT_BBP_AGC_R0_BW_SHIFT) & MT_BBP_AGC_R0_BW_MASK)
    transport.rmw32(MT_BBP_AGC_R0,
                    MT_BBP_AGC_R0_CTRL_CHAN_MASK,
                    (ctrl << MT_BBP_AGC_R0_CTRL_CHAN_SHIFT) & MT_BBP_AGC_R0_CTRL_CHAN_MASK)
    transport.rmw32(MT_BBP_TXBE_R0_VAL,
                    0x3,
                    ctrl & 0x3)


# ---------------------------------------------------------------------------
# MCU-side commands.
# ---------------------------------------------------------------------------
async def mcu_load_cr(mcu: McuChannel, cr_type: int = 0,
                      temp_level: int = 0, channel: int = 0) -> bool:
    """[SRC] mt76x2/usb_init.c:180 — `mt76x2_mcu_load_cr(MT_RF_BBP_CR, 0, 0)`.

    Payload struct: { u8 type; u8 temp_level; u8 channel; u8 _pad; }
    (Total 4 bytes.) Sent with wait_resp=true.
    """
    payload = struct.pack("<BBBB", cr_type, temp_level, channel, 0)
    return await mcu.send(CMD_LOAD_CR, payload, wait_resp=True,
                          resp_timeout_ms=1000)


async def mcu_set_channel(mcu: McuChannel, channel: int, bw: int,
                          bw_index: int, scan: bool, chainmask: int) -> bool:
    """[SRC] mt76x2/mcu.c:15. CMD_SWITCH_CHANNEL_OP, wait_resp=true.

    Payload struct (8 bytes, 4-byte aligned):
       u8  idx           — channel number
       u8  scan          — 0/1
       u8  bw            — 0=20, 1=40, 2=80
       u8  _pad0
       __le16 chainmask
       u8  ext_chan      — upper/lower for HT40 (0 for 20 MHz)
       u8  _pad1
    """
    payload = struct.pack(
        "<BBBBHBB",
        channel & 0xFF,
        1 if scan else 0,
        bw & 0xFF,
        0,
        chainmask & 0xFFFF,
        bw_index & 0xFF,
        0,
    )
    return await mcu.send(CMD_SWITCH_CHANNEL_OP, payload, wait_resp=True,
                          resp_timeout_ms=1000)


async def mcu_init_gain(mcu: McuChannel, channel: int,
                        gain: int = 0, force: bool = True) -> bool:
    """[SRC] mt76x2/mcu.c:75. CMD_INIT_GAIN_OP, wait_resp=true.

    Payload: __le32 channel (BIT(31) if force) + __le32 gain.
    """
    chan_field = channel | (1 << 31) if force else channel
    payload = struct.pack("<II", chan_field & 0xFFFFFFFF, gain & 0xFFFFFFFF)
    return await mcu.send(CMD_INIT_GAIN_OP, payload, wait_resp=True,
                          resp_timeout_ms=1000)
