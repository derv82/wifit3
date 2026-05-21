"""MT76x0U PHY (BBP + RF) bring-up.

M3b scope: `mt76x0_init_bbp` + `mt76x0_phy_wait_bbp_ready`.
M3d.1 adds RF access primitives + `mt76x0_phy_ant_select`.
M3d.2 will add `mt76x0_phy_rf_init` + `set_rxpath` + `set_txdac` +
`mt76x0_phy_init`.

[SRC] mt76x0/init.c:87-108 (`mt76x0_init_bbp`)
[SRC] mt76x0/phy.c:185-203 (`mt76x0_phy_wait_bbp_ready`)
[SRC] mt76x0/phy.c:103-165 (rf_wr/rr/rmw/set/clear)
[SRC] mt76x0/phy.c:426-470 (mt76x0_phy_ant_select)
"""
from __future__ import annotations

import logging

from .constants import (
    MT_BBP_AGC,
    MT_BBP_CORE,
    MT_BBP_TXBE,
    MT_COEXCFG3,
    MT_EE_ANTENNA,
    MT_EE_ANTENNA_DUAL,
    MT_EE_NIC_CONF_2,
    MT_EE_NIC_CONF_2_ANT_DIV,
    MT_EE_NIC_CONF_2_ANT_OPT,
    MT_MCU_MEMMAP_RF,
    MT_MCU_MEMMAP_WLAN,
    MT_RF,
    MT_WLAN_FUN_CTRL,
    RF_BW_20,
    RF_G_BAND,
)
from .initvals_bbp import BBP_INIT_TAB, DCOC_TAB, filter_bbp_switch_tab
from .initvals_rf import (
    RF_2G_CHANNEL_0_TAB,
    RF_5G_CHANNEL_0_TAB,
    RF_BAND_SWITCH_TAB,
    RF_BW_SWITCH_TAB,
    RF_CENTRAL_TAB,
    RF_VGA_CHANNEL_0_TAB,
)
from .mcu import MCUChannel
from .transport import MT76x0UTransport

logger = logging.getLogger(__name__)


class PHYInitError(RuntimeError):
    """A PHY init step failed (BBP not ready, table upload failure, ...)."""


# ---------------------------------------------------------------------------
# RF register access — [SRC] mt76x0/phy.c:103-165.
#
# On USB, mt76x0_rf_wr / _rr route through the MCU command channel with
# base=MT_MCU_MEMMAP_RF (0x80000000). The "offset" is MT_RF(bank, reg) =
# (bank << 16) | reg. rmw/set/clear are read-modify-write wrappers around
# rf_wr+rr.
# ---------------------------------------------------------------------------


def rf_wr(mcu: MCUChannel, offset: int, val: int) -> None:
    """`mt76x0_rf_wr` — write u8 `val` to RF register at `offset`.
    [SRC] mt76x0/phy.c:103-117.
    """
    mcu.random_write(MT_MCU_MEMMAP_RF, [(offset, val & 0xFF)])


def rf_rr(mcu: MCUChannel, offset: int) -> int:
    """`mt76x0_rf_rr` — read u8 from RF register at `offset`.
    [SRC] mt76x0/phy.c:119-138.
    """
    vals = mcu.random_read(MT_MCU_MEMMAP_RF, [offset])
    return vals[0] & 0xFF


def rf_rmw(mcu: MCUChannel, offset: int, mask: int, val: int) -> int:
    """`mt76x0_rf_rmw` — read, AND with ~mask, OR with `val`, write.
    [SRC] mt76x0/phy.c:140-153.
    """
    cur = rf_rr(mcu, offset)
    new = (cur & ~mask) | val
    rf_wr(mcu, offset, new)
    return new & 0xFF


def rf_set(mcu: MCUChannel, offset: int, val: int) -> int:
    """`mt76x0_rf_set` — OR `val` into the RF register (no clear).
    [SRC] mt76x0/phy.c:155-159 — `mt76x0_rf_rmw(offset, 0, val)`.
    """
    return rf_rmw(mcu, offset, 0, val)


def rf_clear(mcu: MCUChannel, offset: int, mask: int) -> int:
    """`mt76x0_rf_clear` — clear bits in `mask` from the RF register.
    [SRC] mt76x0/phy.c:161-165 — `mt76x0_rf_rmw(offset, mask, 0)`.
    """
    return rf_rmw(mcu, offset, mask, 0)


# ---------------------------------------------------------------------------
# mt76x0_phy_ant_select — [SRC] mt76x0/phy.c:426-470.
#
# Reads three EFUSE fields (ANTENNA, CFG1_INIT, NIC_CONF_2), reads two
# MAC regs (MT_WLAN_FUN_CTRL, MT_COEXCFG3), updates them per the
# dual-vs-single-antenna logic, writes back. Also writes the modified
# EFUSE values (`ee_ant`) and a CFG1 field — but those are kept in
# the chip's runtime state, not re-written to EFUSE (EFUSE is OTP).
#
# Actually re-reading the kernel: ee_ant and ee_cfg1 are LOCAL variables
# the kernel mutates but DOESN'T write back. The function only writes
# MT_WLAN_FUN_CTRL and MT_COEXCFG3. So we only mirror those two writes.
# ---------------------------------------------------------------------------


def phy_ant_select(
    transport: MT76x0UTransport, has_2ghz: bool, has_5ghz: bool, efuse_cache,
) -> None:
    """Port of `mt76x0_phy_ant_select` (mt76x0/phy.c:426-470).

    Branches on `ee_ant & MT_EE_ANTENNA_DUAL`:
      - Dual: uses ANT_OPT + ANT_DIV bits to choose ant_div mode.
      - Single (our dev card path): if has_5ghz, set COEX3 BIT(3)|BIT(4);
        else set WLAN_FUN_CTRL BIT(6) + COEX3 BIT(1).

    Writes two MAC regs: MT_WLAN_FUN_CTRL (with bits 5/6 cleared then
    optionally set) and MT_COEXCFG3 (with bits 2-5 cleared then per-mode set).
    """
    ee_ant = efuse_cache.get_u16(MT_EE_ANTENNA)
    # ee_cfg1 read in kernel but not used to write anything; skip.
    nic_conf2 = efuse_cache.get_u16(MT_EE_NIC_CONF_2)

    wlan = transport.read32(MT_WLAN_FUN_CTRL)
    coex3 = transport.read32(MT_COEXCFG3)

    # Kernel clears bits 5 and 6 of wlan; bits 2-5 of coex3 (GENMASK(5, 2)).
    wlan &= ~((1 << 5) | (1 << 6))
    coex3 &= ~(0xF << 2)   # GENMASK(5, 2) = 0x3C — bits 2-5 only

    if ee_ant & MT_EE_ANTENNA_DUAL:
        # Dual antenna mode.
        ant_div = (
            not (nic_conf2 & MT_EE_NIC_CONF_2_ANT_OPT)
            and (nic_conf2 & MT_EE_NIC_CONF_2_ANT_DIV)
        )
        # Kernel ALSO sets BIT(12) in local `ee_ant` if ant_div but doesn't
        # write it anywhere — purely local state. Skip.
        if not ant_div:
            coex3 |= 1 << 4
        coex3 |= 1 << 3
        if has_2ghz:
            wlan |= 1 << 6
        path = "dual"
    else:
        # Single antenna mode.
        if has_5ghz:
            coex3 |= (1 << 3) | (1 << 4)
        else:
            wlan |= 1 << 6
            coex3 |= 1 << 1
        path = "single"

    transport.write32(MT_WLAN_FUN_CTRL, wlan)
    transport.write32(MT_COEXCFG3, coex3)
    logger.info("phy_ant_select: %s antenna mode (ee_ant=0x%04x, "
                "nic_conf2=0x%04x) → WLAN_FUN_CTRL=0x%08x COEXCFG3=0x%08x",
                path, ee_ant, nic_conf2, wlan, coex3)


# ---------------------------------------------------------------------------
# M3d.2: mt76x0_phy_rf_init + set_rxpath + set_txdac + phy_init.
# ---------------------------------------------------------------------------


def _apply_rf_patch_override(reg: int, val: int) -> int:
    """Port of kernel mt76x0_rf_patch_reg_array's per-entry switch
    ([SRC] mt76x0/phy.c:1116-1155).

    Overrides the table value for three specific RF registers based on
    chip variant. For our dev card (USB MT7610U, NOT mt7610e or mt7630)
    all three overrides return values that happen to match the table
    verbatim — but we keep the logic so the chip-variant guarantees are
    explicit, not coincidence.
    """
    # Our chip: USB (not mmio), not mt7610e, not mt7630.
    if reg == MT_RF(0, 3):
        return 0x73        # USB branch
    if reg == MT_RF(0, 21):
        return 0x12        # not-mt7610e branch
    if reg == MT_RF(5, 2):
        return 0x0C        # neither mt7630 nor mt7610e branch
    return val


def rf_patch_reg_array(
    mcu: MCUChannel, table: list[tuple[int, int]],
) -> None:
    """Port of `mt76x0_rf_patch_reg_array` (mt76x0/phy.c:1116-1155).

    Iterates the table writing each entry via rf_wr after applying the
    chip-variant override. Per-entry write (NOT batched via RF_RANDOM_WRITE)
    because the kernel writes one at a time.
    """
    for reg, raw_val in table:
        val = _apply_rf_patch_override(reg, raw_val)
        rf_wr(mcu, reg, val)


def _filter_bw_switch_default(bw_band: int) -> bool:
    """`mt76x0_phy_rf_init`'s bw_switch_tab filter ([SRC] mt76x0/phy.c:1168-1176).

    Kernel:
      if (item->bw_band == RF_BW_20) write;
      else if (((RF_G_BAND | RF_BW_20) & item->bw_band) == (RF_G_BAND | RF_BW_20)) write;

    Captures both bare-BW_20 entries AND entries that have both G_BAND and BW_20
    in their mask.
    """
    if bw_band == RF_BW_20:
        return True
    want = RF_G_BAND | RF_BW_20
    return (bw_band & want) == want


def phy_rf_init(
    mcu: MCUChannel, freq_offset: int,
) -> None:
    """Port of `mt76x0_phy_rf_init` (mt76x0/phy.c:1157-1205).

    Steps in kernel order:
      1. rf_patch_reg_array(RF_CENTRAL_TAB)            — bank 0 init
      2. rf_patch_reg_array(RF_2G_CHANNEL_0_TAB)       — bank 5 init
      3. RF_RANDOM_WRITE(RF_5G_CHANNEL_0_TAB)          — bank 6 init via MCU
      4. RF_RANDOM_WRITE(RF_VGA_CHANNEL_0_TAB)         — bank 7 init via MCU
      5. Filter+write RF_BW_SWITCH_TAB                 — per `_filter_bw_switch_default`
      6. Filter+write RF_BAND_SWITCH_TAB               — only entries with RF_G_BAND
      7. Freq cal: rf_wr(MT_RF(0, 22), min(freq_offset, 0xbf)) + readback
      8. DAC reset: rf_set / rf_clear / rf_set MT_RF(0, 73) BIT(7)
      9. VCO cal trigger: rf_set(MT_RF(0, 4), 0x80)
    """
    logger.info("phy_rf_init: rf_central_tab (%d entries, patched)",
                len(RF_CENTRAL_TAB))
    rf_patch_reg_array(mcu, RF_CENTRAL_TAB)

    logger.info("phy_rf_init: rf_2g_channel_0_tab (%d entries, patched)",
                len(RF_2G_CHANNEL_0_TAB))
    rf_patch_reg_array(mcu, RF_2G_CHANNEL_0_TAB)

    logger.info("phy_rf_init: rf_5g_channel_0_tab (%d entries via MCU)",
                len(RF_5G_CHANNEL_0_TAB))
    mcu.random_write(MT_MCU_MEMMAP_RF, RF_5G_CHANNEL_0_TAB)

    logger.info("phy_rf_init: rf_vga_channel_0_tab (%d entries via MCU)",
                len(RF_VGA_CHANNEL_0_TAB))
    mcu.random_write(MT_MCU_MEMMAP_RF, RF_VGA_CHANNEL_0_TAB)

    bw_writes = sum(1 for bw_band, _, _ in RF_BW_SWITCH_TAB
                    if _filter_bw_switch_default(bw_band))
    logger.info("phy_rf_init: rf_bw_switch_tab: writing %d/%d filtered entries",
                bw_writes, len(RF_BW_SWITCH_TAB))
    for bw_band, reg, value in RF_BW_SWITCH_TAB:
        if _filter_bw_switch_default(bw_band):
            rf_wr(mcu, reg, value)

    band_writes = sum(1 for bw_band, _, _ in RF_BAND_SWITCH_TAB
                      if bw_band & RF_G_BAND)
    logger.info("phy_rf_init: rf_band_switch_tab: writing %d/%d G_BAND entries",
                band_writes, len(RF_BAND_SWITCH_TAB))
    for bw_band, reg, value in RF_BAND_SWITCH_TAB:
        if bw_band & RF_G_BAND:
            rf_wr(mcu, reg, value)

    # Freq cal — kernel: `min_t(u8, dev->cal.rx.freq_offset, 0xbf)`.
    # Coerce to unsigned u8 (matches `min_t(u8, ...)` truncation) then min.
    clamped = min(freq_offset & 0xFF, 0xBF)
    logger.info("phy_rf_init: freq cal MT_RF(0,22) = 0x%02x (freq_offset=%d)",
                clamped, freq_offset)
    rf_wr(mcu, MT_RF(0, 22), clamped)
    rf_rr(mcu, MT_RF(0, 22))   # kernel reads back for sync

    # DAC reset: set / clear / set BIT(7) of MT_RF(0, 73). [SRC] phy.c:1199-1201.
    logger.info("phy_rf_init: DAC reset (toggle MT_RF(0,73) BIT(7))")
    rf_set(mcu, MT_RF(0, 73), 0x80)
    rf_clear(mcu, MT_RF(0, 73), 0x80)
    rf_set(mcu, MT_RF(0, 73), 0x80)

    # VCO calibration trigger. [SRC] phy.c:1204.
    logger.info("phy_rf_init: VCO cal trigger (set MT_RF(0,4) bit 7)")
    rf_set(mcu, MT_RF(0, 4), 0x80)


# Default chainmask for MT7610U = 1T1R. Stored as (tx<<8)|rx — 0x0101.
DEFAULT_CHAINMASK = 0x0101


def phy_set_rxpath(
    transport: MT76x0UTransport, chainmask: int = DEFAULT_CHAINMASK,
) -> None:
    """Port of `mt76x02_phy_set_rxpath` (mt76x02_phy.c:12-31).

    Reads MT_BBP(AGC, 0), clears BIT(4), then per `chainmask & 0xf` sets
    BIT(3) for 2-stream RX (only `case 2`) or clears it (default). Re-reads
    for ordering sync.

    For MT7610U (1T1R, chainmask=0x0101 → rx_path=1) we always take the
    default branch — BIT(3) cleared.
    """
    val = transport.read32(MT_BBP_AGC(0))
    val &= ~(1 << 4)
    if (chainmask & 0xF) == 2:
        val |= 1 << 3
    else:
        val &= ~(1 << 3)
    transport.write32(MT_BBP_AGC(0), val)
    # Kernel `mb(); mt76_rr()` — read for ordering. No-op semantically on USB.
    transport.read32(MT_BBP_AGC(0))


def phy_set_txdac(
    transport: MT76x0UTransport, chainmask: int = DEFAULT_CHAINMASK,
) -> None:
    """Port of `mt76x02_phy_set_txdac` (mt76x02_phy.c:34-47).

    For `txpath = (chainmask >> 8) & 0xf`:
      - case 2: set BIT(0)|BIT(1) of MT_BBP(TXBE, 5).
      - default: clear those bits.

    For MT7610U (1T1R → txpath=1) we take the default branch.
    """
    txpath = (chainmask >> 8) & 0xF
    if txpath == 2:
        transport.set_bits(MT_BBP_TXBE(5), 0x3)
    else:
        transport.clear_bits(MT_BBP_TXBE(5), 0x3)


# ---------------------------------------------------------------------------
# M4a.1: set_channel scaffolding + low-level helpers.
# ---------------------------------------------------------------------------


def phy_bbp_set_bw(mcu: MCUChannel, width: int) -> None:
    """Port of `mt76x0_phy_bbp_set_bw` (mt76x0/phy.c:472-501).

    Maps the nl80211 channel-width to a BW_SETTING int (0=BW20, 1=BW40,
    2=BW80, 4=BW10) and sends it via CMD_FUN_SET_OP(BW_SETTING, ...) with
    wait=True.
    """
    from .constants import (
        BW_SETTING,
        BW_SETTING_BW10,
        BW_SETTING_BW20,
        BW_SETTING_BW40,
        BW_SETTING_BW80,
        NL80211_CHAN_WIDTH_10,
        NL80211_CHAN_WIDTH_20,
        NL80211_CHAN_WIDTH_20_NOHT,
        NL80211_CHAN_WIDTH_40,
        NL80211_CHAN_WIDTH_80,
    )
    if width in (NL80211_CHAN_WIDTH_20_NOHT, NL80211_CHAN_WIDTH_20):
        bw = BW_SETTING_BW20
    elif width == NL80211_CHAN_WIDTH_40:
        bw = BW_SETTING_BW40
    elif width == NL80211_CHAN_WIDTH_80:
        bw = BW_SETTING_BW80
    elif width == NL80211_CHAN_WIDTH_10:
        bw = BW_SETTING_BW10
    else:
        raise PHYInitError(f"phy_bbp_set_bw: unsupported width {width}")
    # function_select uses CMD_FUN_SET_OP; for BW_SETTING wait=True
    # (kernel `mt76x02_mcu_function_select` line 94: `if (func != Q_SELECT) wait=true`).
    mcu.function_select(BW_SETTING, bw)


def phy_set_bw(
    transport: MT76x0UTransport, width: int, ctrl: int,
) -> None:
    """Port of `mt76x02_phy_set_bw` (mt76x02_phy.c:124-147).

    Per-width: writes (core_val, agc_val) into BBP(CORE, 1).R1_BW and
    BBP(AGC, 0).R0_BW, plus `ctrl` into AGC(0).R0_CTRL_CHAN and
    TXBE(0).R0_CTRL_CHAN.

    Width → (core_val, agc_val):
      WIDTH_80  → (3, 7)
      WIDTH_40  → (2, 3)
      default   → (0, 1)
    """
    from .constants import (
        MT_BBP_AGC_R0_BW_MASK,
        MT_BBP_AGC_R0_BW_SHIFT,
        MT_BBP_AGC_R0_CTRL_CHAN_MASK,
        MT_BBP_AGC_R0_CTRL_CHAN_SHIFT,
        MT_BBP_CORE_R1_BW_MASK,
        MT_BBP_CORE_R1_BW_SHIFT,
        MT_BBP_TXBE_R0_CTRL_CHAN_MASK,
        MT_BBP_TXBE_R0_CTRL_CHAN_SHIFT,
        NL80211_CHAN_WIDTH_40,
        NL80211_CHAN_WIDTH_80,
    )
    if width == NL80211_CHAN_WIDTH_80:
        core_val, agc_val = 3, 7
    elif width == NL80211_CHAN_WIDTH_40:
        core_val, agc_val = 2, 3
    else:
        core_val, agc_val = 0, 1

    def _rmw_field(reg: int, mask: int, shift: int, val: int) -> None:
        cur = transport.read32(reg)
        new = (cur & ~mask) | ((val << shift) & mask)
        transport.write32(reg, new)

    from .constants import MT_BBP_CORE
    _rmw_field(MT_BBP_CORE(1), MT_BBP_CORE_R1_BW_MASK,
               MT_BBP_CORE_R1_BW_SHIFT, core_val)
    _rmw_field(MT_BBP_AGC(0), MT_BBP_AGC_R0_BW_MASK,
               MT_BBP_AGC_R0_BW_SHIFT, agc_val)
    _rmw_field(MT_BBP_AGC(0), MT_BBP_AGC_R0_CTRL_CHAN_MASK,
               MT_BBP_AGC_R0_CTRL_CHAN_SHIFT, ctrl)
    _rmw_field(MT_BBP_TXBE(0), MT_BBP_TXBE_R0_CTRL_CHAN_MASK,
               MT_BBP_TXBE_R0_CTRL_CHAN_SHIFT, ctrl)


def phy_set_band_common(
    transport: MT76x0UTransport, band: int, primary_upper: bool,
) -> None:
    """Port of `mt76x02_phy_set_band` (mt76x02_phy.c:150-167).

    Sets MT_TX_BAND_CFG bit 2 (2G) or bit 1 (5G), clears the other; then
    RMW-field MT_TX_BAND_CFG.UPPER_40M (BIT 0) to `primary_upper`.
    """
    from .constants import (
        MT_TX_BAND_CFG,
        MT_TX_BAND_CFG_2G,
        MT_TX_BAND_CFG_5G,
        MT_TX_BAND_CFG_UPPER_40M,
        NL80211_BAND_2GHZ,
        NL80211_BAND_5GHZ,
    )
    if band == NL80211_BAND_2GHZ:
        transport.set_bits(MT_TX_BAND_CFG, MT_TX_BAND_CFG_2G)
        transport.clear_bits(MT_TX_BAND_CFG, MT_TX_BAND_CFG_5G)
    elif band == NL80211_BAND_5GHZ:
        transport.clear_bits(MT_TX_BAND_CFG, MT_TX_BAND_CFG_2G)
        transport.set_bits(MT_TX_BAND_CFG, MT_TX_BAND_CFG_5G)
    # RMW UPPER_40M bit per `primary_upper`.
    cur = transport.read32(MT_TX_BAND_CFG)
    new = (cur & ~MT_TX_BAND_CFG_UPPER_40M) | (
        MT_TX_BAND_CFG_UPPER_40M if primary_upper else 0
    )
    transport.write32(MT_TX_BAND_CFG, new)


def phy_set_band_mt76x0(
    transport: MT76x0UTransport, mcu: MCUChannel, band: int,
) -> None:
    """Port of `mt76x0_phy_set_band` (mt76x0/phy.c:205-230).

    Per band: bulk-writes the channel-0 RF table for that band via MCU,
    sets MT_RF(5, 0) and MT_RF(6, 0) to band-specific values, then writes
    MT_TX_ALC_VGA3 + MT_TX0_RF_GAIN_CORR.
    """
    from .constants import (
        MT_TX0_RF_GAIN_CORR,
        MT_TX_ALC_VGA3,
        NL80211_BAND_2GHZ,
        NL80211_BAND_5GHZ,
    )
    if band == NL80211_BAND_2GHZ:
        mcu.random_write(MT_MCU_MEMMAP_RF, RF_2G_CHANNEL_0_TAB)
        rf_wr(mcu, MT_RF(5, 0), 0x45)
        rf_wr(mcu, MT_RF(6, 0), 0x44)
        transport.write32(MT_TX_ALC_VGA3, 0x00050007)
        transport.write32(MT_TX0_RF_GAIN_CORR, 0x003E0002)
    elif band == NL80211_BAND_5GHZ:
        from .initvals_rf import RF_5G_CHANNEL_0_TAB as _5G
        mcu.random_write(MT_MCU_MEMMAP_RF, _5G)
        rf_wr(mcu, MT_RF(5, 0), 0x44)
        rf_wr(mcu, MT_RF(6, 0), 0x45)
        transport.write32(MT_TX_ALC_VGA3, 0x00000005)
        transport.write32(MT_TX0_RF_GAIN_CORR, 0x01010102)
    else:
        raise PHYInitError(f"phy_set_band_mt76x0: invalid band {band}")


# ext_cca_chan[4] table — [SRC] mt76x0/phy.c:916-937.
# Each entry packs CCA0..CCA3 + CCA_MASK fields for a given group_index.
def _build_ext_cca_chan() -> list[int]:
    from .constants import (
        MT_EXT_CCA_CFG_CCA0_SHIFT,
        MT_EXT_CCA_CFG_CCA1_SHIFT,
        MT_EXT_CCA_CFG_CCA2_SHIFT,
        MT_EXT_CCA_CFG_CCA3_SHIFT,
        MT_EXT_CCA_CFG_CCA_MASK_SHIFT,
    )
    def pack(c0, c1, c2, c3, mask_bit):
        return (
            (c0 << MT_EXT_CCA_CFG_CCA0_SHIFT)
            | (c1 << MT_EXT_CCA_CFG_CCA1_SHIFT)
            | (c2 << MT_EXT_CCA_CFG_CCA2_SHIFT)
            | (c3 << MT_EXT_CCA_CFG_CCA3_SHIFT)
            | ((1 << mask_bit) << MT_EXT_CCA_CFG_CCA_MASK_SHIFT)
        )
    return [
        pack(0, 1, 2, 3, 0),   # group_index 0
        pack(1, 0, 2, 3, 1),
        pack(2, 3, 1, 0, 2),
        pack(3, 2, 1, 0, 3),
    ]


EXT_CCA_CHAN = _build_ext_cca_chan()


def set_channel_20mhz(
    transport: MT76x0UTransport, mcu: MCUChannel, channel: int,
) -> dict:
    """M4a.1 scaffold of `mt76x0_phy_set_channel` for 20 MHz monitor RX.

    [SRC] mt76x0/phy.c:913-1016. Steps 1-7 + channel-14 BBP bit only.
    Steps 8-9 (set_chan_rf_params, set_chan_bbp_params), VCO enable,
    AGC init, calibrate, set_txpower are DEFERRED to M4a.2 / M4a.3.

    For a 20 MHz channel:
      - ch_group_index = 0
      - rf_bw_band = (G_BAND if channel<=14 else A_BAND) | RF_BW_20
      - width = NL80211_CHAN_WIDTH_20

    Returns a dict with the post-state register values for assertion.
    """
    from .constants import (
        MT_BBP_CORE,
        MT_EXT_CCA_CFG,
        MT_EXT_CCA_CFG_CCA0_MASK,
        MT_EXT_CCA_CFG_CCA1_MASK,
        MT_EXT_CCA_CFG_CCA2_MASK,
        MT_EXT_CCA_CFG_CCA3_MASK,
        MT_EXT_CCA_CFG_CCA_MASK_MASK,
        MT_TX_BAND_CFG,
        NL80211_BAND_2GHZ,
        NL80211_BAND_5GHZ,
        NL80211_CHAN_WIDTH_20,
    )
    if not (1 <= channel <= 14 or 36 <= channel <= 196):
        raise PHYInitError(f"set_channel_20mhz: unsupported channel {channel}")

    band = NL80211_BAND_2GHZ if channel <= 14 else NL80211_BAND_5GHZ
    rf_band = RF_G_BAND if band == NL80211_BAND_2GHZ else 0x0200  # RF_A_BAND
    rf_bw_band = rf_band | RF_BW_20
    ch_group_index = 0
    width = NL80211_CHAN_WIDTH_20

    logger.info("set_channel_20mhz: ch=%d band=%s rf_bw_band=0x%04x",
                channel, "2.4G" if band == NL80211_BAND_2GHZ else "5G",
                rf_bw_band)

    # Step 4 (USB branch): mt76x0_phy_bbp_set_bw via MCU CMD_FUN_SET_OP.
    phy_bbp_set_bw(mcu, width)

    # Step 5: mt76x02_phy_set_bw — BBP CORE/AGC/TXBE bit fields.
    phy_set_bw(transport, width, ch_group_index)

    # Step 6: mt76x02_phy_set_band — MT_TX_BAND_CFG 2G/5G + UPPER_40M.
    # primary_upper = ch_group_index & 1 = 0 for our default.
    phy_set_band_common(transport, band, primary_upper=bool(ch_group_index & 1))

    # Step 7: MT_EXT_CCA_CFG RMW with ext_cca_chan[group_index].
    cca_mask = (
        MT_EXT_CCA_CFG_CCA0_MASK | MT_EXT_CCA_CFG_CCA1_MASK
        | MT_EXT_CCA_CFG_CCA2_MASK | MT_EXT_CCA_CFG_CCA3_MASK
        | MT_EXT_CCA_CFG_CCA_MASK_MASK
    )
    cur = transport.read32(MT_EXT_CCA_CFG)
    new = (cur & ~cca_mask) | (EXT_CCA_CHAN[ch_group_index] & cca_mask)
    transport.write32(MT_EXT_CCA_CFG, new)

    # Step 8: mt76x0_phy_set_band — RF table + per-band tweaks.
    phy_set_band_mt76x0(transport, mcu, band)

    # Step 9 (DEFERRED to M4a.2): mt76x0_phy_set_chan_rf_params.
    logger.info("set_channel_20mhz: M4a.2 deferred — set_chan_rf_params "
                "(freq_plan PLL programming for actual frequency lock)")

    # Step 10: Japan TX filter at channel 14.
    if channel == 14:
        transport.set_bits(MT_BBP_CORE(1), 0x20)
    else:
        transport.clear_bits(MT_BBP_CORE(1), 0x20)

    # Step 11 (skipped — display only): mt76x0_read_rx_gain.
    # Step 12 (DEFERRED to M4a.3): mt76x0_phy_set_chan_bbp_params.
    # Step 13 (DEFERRED to M4a.3): VCO enable.
    # Steps 15-17 (DEFERRED to M4a.3): init_agc_gain, phy_calibrate, set_txpower.
    logger.info("set_channel_20mhz: M4a.3 deferred — VCO enable + calibrate "
                "+ AGC init")

    # Read post-state for assertions.
    from .constants import MT_BBP_TXBE
    return {
        "tx_band_cfg": transport.read32(MT_TX_BAND_CFG),
        "ext_cca_cfg": transport.read32(MT_EXT_CCA_CFG),
        "bbp_core_1":  transport.read32(MT_BBP_CORE(1)),
        "bbp_agc_0":   transport.read32(MT_BBP_AGC(0)),
        "bbp_txbe_0":  transport.read32(MT_BBP_TXBE(0)),
    }


def phy_init(
    transport: MT76x0UTransport, mcu: MCUChannel, efuse_full,
) -> None:
    """Port of `mt76x0_phy_init` (mt76x0/phy.c:1207-1215).

    Wraps the four PHY init steps:
      1. phy_ant_select
      2. phy_rf_init
      3. phy_set_rxpath
      4. phy_set_txdac

    Skips the kernel's `INIT_DELAYED_WORK(&dev->cal_work, ...)` — that
    schedules periodic calibration, which is a wifit3 monitor-mode no-op.
    """
    phy_ant_select(
        transport,
        has_2ghz=efuse_full.has_2ghz,
        has_5ghz=efuse_full.has_5ghz,
        efuse_cache=efuse_full.cache,
    )
    phy_rf_init(mcu, freq_offset=efuse_full.freq_offset)
    phy_set_rxpath(transport)
    phy_set_txdac(transport)


def phy_wait_bbp_ready(transport: MT76x0UTransport) -> int:
    """Port of `mt76x0_phy_wait_bbp_ready` (mt76x0/phy.c:185-203).

    Polls `MT_BBP(CORE, 0)` up to 20 times, breaking when the value is
    neither 0 nor all-1s. Kernel uses a busy-poll (no sleep) — on USB each
    read is a control transfer (~ms), so the wall-clock is ~20 ms worst-case.

    Returns the BBP version (the read value). Raises PHYInitError on
    failure.
    """
    bbp_core0 = MT_BBP_CORE(0)
    val = 0
    for _ in range(20):
        val = transport.read32(bbp_core0)
        # Kernel: `if (val && ~val)` — val is not 0 AND not all-1s.
        if val and (val & 0xFFFFFFFF) != 0xFFFFFFFF:
            logger.debug("phy_wait_bbp_ready: BBP version 0x%08x", val)
            return val
    raise PHYInitError(
        f"phy_wait_bbp_ready: BBP not ready after 20 polls (last val=0x{val:08x})"
    )


def init_bbp(transport: MT76x0UTransport, mcu: MCUChannel) -> int:
    """Port of `mt76x0_init_bbp` (mt76x0/init.c:87-108).

    Steps in kernel order:
      1. phy_wait_bbp_ready
      2. RANDOM_WRITE(bbp_init_tab) — 54 pairs via MCU.
      3. For each switch_tab entry matching `RF_G_BAND | RF_BW_20`, write
         directly via mt76_wr (20 entries on the dev card). [WIRE] f465-503.
      4. RANDOM_WRITE(dcoc_tab) — 9 pairs via MCU.

    Returns the BBP version from step 1.
    """
    bbp_version = phy_wait_bbp_ready(transport)
    logger.info("init_bbp: BBP version = 0x%08x", bbp_version)

    logger.info("init_bbp: uploading bbp_init_tab (%d pairs via MCU)",
                len(BBP_INIT_TAB))
    mcu.random_write(MT_MCU_MEMMAP_WLAN, BBP_INIT_TAB)

    # Switch-tab: filter by RF_G_BAND | RF_BW_20 default mask, then direct-write.
    # [SRC] mt76x0/init.c:97-103.
    want = RF_G_BAND | RF_BW_20
    switch_pairs = filter_bbp_switch_tab(want)
    logger.info("init_bbp: writing %d filtered bbp_switch_tab entries "
                "via direct vendor xfers (mask=0x%04x)",
                len(switch_pairs), want)
    for reg, value in switch_pairs:
        transport.write32(reg, value)

    logger.info("init_bbp: uploading dcoc_tab (%d pairs via MCU)",
                len(DCOC_TAB))
    mcu.random_write(MT_MCU_MEMMAP_WLAN, DCOC_TAB)

    logger.info("init_bbp: done")
    return bbp_version
