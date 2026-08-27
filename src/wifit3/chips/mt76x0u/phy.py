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
    RF_A_BAND,
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


def _apply_rf_patch_override(reg: int, val: int, is_mt7630: bool = False) -> int:
    """Port of kernel mt76x0_rf_patch_reg_array's per-entry switch
    ([SRC] mt76x0/phy.c:1116-1155).

    Overrides the table value for three specific RF registers based on
    chip variant:
      - MT_RF(0, 3): the 0x70(7630)/0x63 split is inside `if mt76_is_mmio` —
        USB always takes the else, 0x73, for every strap incl. 0x7630.
      - MT_RF(0, 21): 0x10(mt7610e)/0x12; is_mt7610e requires mmio so USB is
        always 0x12.
      - MT_RF(5, 2): 0x1d(7630)/0x00(mt7610e)/0x0c — the is_mt7630 arm is the
        one USB-live strap branch. [SRC] mt76x0/phy.c:1143-1146.

    ``is_mt7630`` defaults False (the captured 0x7650 reference), so the RF(5,2)
    write is 0x0c and the whole override is byte-identical to the recorded card.
    """
    # USB (not mmio) → is_mt7610e is always false; only is_mt7630 is USB-live.
    if reg == MT_RF(0, 3):
        return 0x73        # USB else-branch (mmio-only 7630/else split above)
    if reg == MT_RF(0, 21):
        return 0x12        # not-mt7610e branch (mt7610e is mmio-only)
    if reg == MT_RF(5, 2):
        return 0x1D if is_mt7630 else 0x0C
    return val


def rf_patch_reg_array(
    mcu: MCUChannel, table: list[tuple[int, int]], is_mt7630: bool = False,
) -> None:
    """Port of `mt76x0_rf_patch_reg_array` (mt76x0/phy.c:1116-1155).

    Iterates the table writing each entry via rf_wr after applying the
    chip-variant override. Per-entry write (NOT batched via RF_RANDOM_WRITE)
    because the kernel writes one at a time. ``is_mt7630`` gates the RF(5,2)
    value; default False = reference.
    """
    for reg, raw_val in table:
        val = _apply_rf_patch_override(reg, raw_val, is_mt7630=is_mt7630)
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
    mcu: MCUChannel, freq_offset: int, is_mt7630: bool = False,
) -> None:
    """Port of `mt76x0_phy_rf_init` (mt76x0/phy.c:1157-1205).

    ``is_mt7630`` is forwarded to the RF-patch overrides (RF(5,2)); default
    False = the captured 0x7650 reference.

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
    logger.debug("phy_rf_init: rf_central_tab (%d entries, patched)",
                len(RF_CENTRAL_TAB))
    rf_patch_reg_array(mcu, RF_CENTRAL_TAB, is_mt7630=is_mt7630)

    logger.debug("phy_rf_init: rf_2g_channel_0_tab (%d entries, patched)",
                len(RF_2G_CHANNEL_0_TAB))
    rf_patch_reg_array(mcu, RF_2G_CHANNEL_0_TAB, is_mt7630=is_mt7630)

    logger.debug("phy_rf_init: rf_5g_channel_0_tab (%d entries via MCU)",
                len(RF_5G_CHANNEL_0_TAB))
    mcu.random_write(MT_MCU_MEMMAP_RF, RF_5G_CHANNEL_0_TAB)

    logger.debug("phy_rf_init: rf_vga_channel_0_tab (%d entries via MCU)",
                len(RF_VGA_CHANNEL_0_TAB))
    mcu.random_write(MT_MCU_MEMMAP_RF, RF_VGA_CHANNEL_0_TAB)

    bw_writes = sum(1 for bw_band, _, _ in RF_BW_SWITCH_TAB
                    if _filter_bw_switch_default(bw_band))
    logger.debug("phy_rf_init: rf_bw_switch_tab: writing %d/%d filtered entries",
                bw_writes, len(RF_BW_SWITCH_TAB))
    for bw_band, reg, value in RF_BW_SWITCH_TAB:
        if _filter_bw_switch_default(bw_band):
            rf_wr(mcu, reg, value)

    band_writes = sum(1 for bw_band, _, _ in RF_BAND_SWITCH_TAB
                      if bw_band & RF_G_BAND)
    logger.debug("phy_rf_init: rf_band_switch_tab: writing %d/%d G_BAND entries",
                band_writes, len(RF_BAND_SWITCH_TAB))
    for bw_band, reg, value in RF_BAND_SWITCH_TAB:
        if bw_band & RF_G_BAND:
            rf_wr(mcu, reg, value)

    # Freq cal — kernel: `min_t(u8, dev->cal.rx.freq_offset, 0xbf)`.
    # Coerce to unsigned u8 (matches `min_t(u8, ...)` truncation) then min.
    clamped = min(freq_offset & 0xFF, 0xBF)
    logger.debug("phy_rf_init: freq cal MT_RF(0,22) = 0x%02x (freq_offset=%d)",
                clamped, freq_offset)
    rf_wr(mcu, MT_RF(0, 22), clamped)
    rf_rr(mcu, MT_RF(0, 22))   # kernel reads back for sync

    # DAC reset: set / clear / set BIT(7) of MT_RF(0, 73). [SRC] phy.c:1199-1201.
    logger.debug("phy_rf_init: DAC reset (toggle MT_RF(0,73) BIT(7))")
    rf_set(mcu, MT_RF(0, 73), 0x80)
    rf_clear(mcu, MT_RF(0, 73), 0x80)
    rf_set(mcu, MT_RF(0, 73), 0x80)

    # VCO calibration trigger. [SRC] phy.c:1204.
    logger.debug("phy_rf_init: VCO cal trigger (set MT_RF(0,4) bit 7)")
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
        # 2 single-pair writes → 1 batched (saves 1 MCU round-trip).
        mcu.random_write(MT_MCU_MEMMAP_RF, [
            (MT_RF(5, 0), 0x45),
            (MT_RF(6, 0), 0x44),
        ])
        transport.write32(MT_TX_ALC_VGA3, 0x00050007)
        transport.write32(MT_TX0_RF_GAIN_CORR, 0x003E0002)
    elif band == NL80211_BAND_5GHZ:
        from .initvals_rf import RF_5G_CHANNEL_0_TAB as _5G
        mcu.random_write(MT_MCU_MEMMAP_RF, _5G)
        mcu.random_write(MT_MCU_MEMMAP_RF, [
            (MT_RF(5, 0), 0x44),
            (MT_RF(6, 0), 0x45),
        ])
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


def phy_set_chan_bbp_params(
    transport: MT76x0UTransport, rf_bw_band: int, lna_gain: int = 0,
) -> int:
    """Port of `mt76x0_phy_set_chan_bbp_params` (mt76x0/phy.c:399-424).

    Iterates BBP_SWITCH_TAB; writes each entry where `(rf_bw_band & item.bw_band)
    == rf_bw_band` (item's mask is a SUPERSET of rf_bw_band). Special-case
    for MT_BBP(AGC, 8): adjusts the AGC_GAIN field by `lna_gain * 2`
    before writing.

    Returns the count of writes performed.
    """
    from .constants import (
        MT_BBP_AGC_GAIN_MASK,
        MT_BBP_AGC_GAIN_SHIFT,
    )
    from .initvals_bbp import BBP_SWITCH_TAB

    agc8_reg = MT_BBP_AGC(8)
    writes = 0
    for bw_band, reg, value in BBP_SWITCH_TAB:
        if (bw_band & rf_bw_band) != rf_bw_band:
            continue
        if reg == agc8_reg:
            # Kernel: gain = FIELD_GET(MT_BBP_AGC_GAIN, val); gain -= lna_gain*2;
            #         val &= ~MT_BBP_AGC_GAIN; val |= FIELD_PREP(...);
            gain = (value & MT_BBP_AGC_GAIN_MASK) >> MT_BBP_AGC_GAIN_SHIFT
            gain = (gain - lna_gain * 2) & 0x7F   # u8 wrap to GAIN field width
            adjusted = (value & ~MT_BBP_AGC_GAIN_MASK) | (gain << MT_BBP_AGC_GAIN_SHIFT)
            transport.write32(reg, adjusted)
        else:
            transport.write32(reg, value)
        writes += 1
    logger.debug("phy_set_chan_bbp_params: %d writes for rf_bw_band=0x%04x "
                "lna_gain=%d", writes, rf_bw_band, lna_gain)
    return writes


def init_agc_gain(transport: MT76x0UTransport) -> tuple[int, int]:
    """Port of `mt76x02_init_agc_gain` (mt76x02_phy.c:193-203).

    Reads MT_BBP(AGC, 8) and MT_BBP(AGC, 9), extracts MT_BBP_AGC_GAIN field
    from each. Caller is responsible for storing these for the runtime
    calibration loop (we don't have that loop in monitor mode, but the
    values are read for parity + future use).

    Returns (agc_gain_init_0, agc_gain_init_1).
    """
    from .constants import (
        MT_BBP_AGC_GAIN_MASK,
        MT_BBP_AGC_GAIN_SHIFT,
    )
    val8 = transport.read32(MT_BBP_AGC(8))
    val9 = transport.read32(MT_BBP_AGC(9))
    g0 = (val8 & MT_BBP_AGC_GAIN_MASK) >> MT_BBP_AGC_GAIN_SHIFT
    g1 = (val9 & MT_BBP_AGC_GAIN_MASK) >> MT_BBP_AGC_GAIN_SHIFT
    return (g0, g1)


def phy_calibrate(
    transport: MT76x0UTransport, mcu: MCUChannel,
    channel: int, power_on: bool = False, is_mt7630: bool = False,
) -> None:
    """Port of `mt76x0_phy_calibrate` (mt76x0/phy.c:861-911).

    For `power_on=False` (per-channel cal):
      1. Save MT_TX_ALC_CFG_0, zero it.
      2. Sleep 500-700 us.
      3. Save MT_BBP(IBI, 9), write 0xffffff7e.
      4. val = 0x600 (2.4 GHz) or 0x701/0x801/0x901 (5 GHz tiered).
      5. CMD_CALIBRATION_OP(MCU_CAL_FULL, val).
      6. CMD_CALIBRATION_OP(MCU_CAL_LC, is_5ghz).
      7. Sleep 15-20 ms.
      8. Restore MT_BBP(IBI, 9) and MT_TX_ALC_CFG_0.
      9. CMD_CALIBRATION_OP(MCU_CAL_RXDCOC, 1).

    For `power_on=True` (post-firmware): adds MCU_CAL_R + MCU_CAL_VCO prelude.
    We skip the TSSI dc-calibrate sub-block since tssi_enabled requires
    EFUSE NIC_CONF_1 BIT(13) which is set per-card; for our card we treat
    it as disabled (display-only field).

    ``is_mt7630`` short-circuits the whole routine — the combo strap skips
    calibration entirely. [SRC] mt76x0/phy.c:867. Default False = the captured
    0x7650 reference (calibration runs), so its wire is unchanged.
    """
    import time as _time
    from .constants import (
        MCU_CAL_FULL,
        MCU_CAL_LC,
        MCU_CAL_R,
        MCU_CAL_RXDCOC,
        MCU_CAL_VCO,
        MT_BBP_IBI,
        MT_TX_ALC_CFG_0,
    )

    if is_mt7630:
        # [SRC] mt76x0/phy.c:866-867 — `if (is_mt7630(dev)) return;`
        logger.info("phy_calibrate: is_mt7630 → skipped (combo strap)")
        return

    is_5ghz = channel > 14

    if power_on:
        mcu.calibrate(MCU_CAL_R, 0)
        mcu.calibrate(MCU_CAL_VCO, channel)
        _time.sleep(0.000020)   # usleep_range(10, 20)
        # We skip the tssi_enabled branch — RX-only monitor mode.

    tx_alc = transport.read32(MT_TX_ALC_CFG_0)
    transport.write32(MT_TX_ALC_CFG_0, 0)
    _time.sleep(0.0007)         # usleep_range(500, 700)

    ibi9_reg = MT_BBP_IBI(9)
    reg_val = transport.read32(ibi9_reg)
    transport.write32(ibi9_reg, 0xFFFFFF7E)

    if is_5ghz:
        if channel < 100:
            val = 0x701
        elif channel < 140:
            val = 0x801
        else:
            val = 0x901
    else:
        val = 0x600
    logger.debug("phy_calibrate: CMD_CAL_FULL param=0x%x", val)
    mcu.calibrate(MCU_CAL_FULL, val)
    mcu.calibrate(MCU_CAL_LC, 1 if is_5ghz else 0)
    _time.sleep(0.018)          # usleep_range(15000, 20000) ~= 15-20 ms

    transport.write32(ibi9_reg, reg_val)
    transport.write32(MT_TX_ALC_CFG_0, tx_alc)
    mcu.calibrate(MCU_CAL_RXDCOC, 1)


def phy_set_chan_rf_params(
    transport: MT76x0UTransport, mcu: MCUChannel,
    channel: int, rf_bw_band: int, efuse_full=None,
) -> None:
    """Port of `mt76x0_phy_set_chan_rf_params` (mt76x0/phy.c:232-397).

    Programs the PLL for the target channel:
      1. Check `SDM_CHANNEL` membership → use SDM_FREQUENCY_PLAN if so.
      2. Look up the freq_item for `channel`; updates `rf_band` from the
         item's `band` field.
      3. Write 5 PLL regs (R37..R33) directly + 11 RF RMW operations
         (R32 b7b5/b4b0, R31 b7b5/b4b0, R30 sdm_reset_n/mash/bp/n_high,
         R28 isi_iso/pfd_dly/clk_sel, R24 xo_div) + 3 plain rf_wr
         (R29 pll_n_low, R26/R27 sdm_k low/mid).
      4. bw_switch_tab pass: writes entries matching `rf_bw == bw_band` OR
         (`rf_bw == bw_band & 0xFF` AND `rf_band & bw_band`).
      5. band_switch_tab pass: writes entries with `bw_band & rf_band`.
      6. Clear MT_RF_MISC bits 2-3.
      7. Ext-PA branch (only if EFUSE PA_INT_X bit is clear): set MT_RF_MISC
         BIT(2) for 5G or BIT(3) for 2G, then write matching ext_pa_tab entries.
      8. Per-band TX gain / ALC registers (2G uses 0x63707400, 5G uses 0x686A7800).
    """
    from .constants import (
        MT_EE_NIC_CONF_0_PA_INT_2G,
        MT_EE_NIC_CONF_0_PA_INT_5G,
        MT_RF_CLK_SEL_MASK,
        MT_RF_ISI_ISO_MASK,
        MT_RF_MISC,
        MT_RF_MISC_EXT_PA_A_BAND,
        MT_RF_MISC_EXT_PA_G_BAND,
        MT_RF_PFD_DLY_MASK,
        MT_RF_PLL_DEN_MASK,
        MT_RF_PLL_K_MASK,
        MT_RF_SDM_BP_MASK,
        MT_RF_SDM_MASH_PRBS_MASK,
        MT_RF_SDM_RESET_MASK,
        MT_RF_XO_DIV_MASK,
        MT_TX0_RF_GAIN_ATTEN,
        MT_TX_ALC_CFG_1,
    )
    from .initvals_rf import RF_BAND_SWITCH_TAB, RF_BW_SWITCH_TAB
    from .initvals_freq import RF_EXT_PA_TAB, SDM_CHANNEL, find_freq_item

    rf_band = rf_bw_band & 0xFF00
    rf_bw = rf_bw_band & 0x00FF

    use_sdm = channel in SDM_CHANNEL
    fi = find_freq_item(channel, use_sdm)
    if fi is None:
        raise PHYInitError(
            f"phy_set_chan_rf_params: no freq_item for channel {channel}"
        )
    # Per kernel: `rf_band = mt76x0_frequency_plan[i].band;` — overrides
    # the rf_bw_band-derived band with the per-channel-specific band tag.
    rf_band = fi.band
    logger.debug("phy_set_chan_rf_params: ch=%d sdm=%s pll_n=0x%04x "
                "pll_sdm_k=0x%05x rf_band=0x%04x",
                channel, use_sdm, fi.pll_n, fi.pll_sdm_k, rf_band)

    # PLL R37..R33 — 5 pairs batched into 1 CMD_RANDOM_WRITE (was 5
    # separate MCU commands, each costing one host->chip->host round-trip).
    # Chip accepts up to 24 pairs per CMD_RANDOM_WRITE.
    mcu.random_write(MT_MCU_MEMMAP_RF, [
        (MT_RF(0, 37), fi.pllR37),
        (MT_RF(0, 36), fi.pllR36),
        (MT_RF(0, 35), fi.pllR35),
        (MT_RF(0, 34), fi.pllR34),
        (MT_RF(0, 33), fi.pllR33),
    ])

    # R32: top 3 bits (mask 0xE0), then bottom 5 bits (PLL_DEN_MASK).
    rf_rmw(mcu, MT_RF(0, 32), 0xE0, fi.pllR32_b7b5)
    rf_rmw(mcu, MT_RF(0, 32), MT_RF_PLL_DEN_MASK, fi.pllR32_b4b0)

    # R31: top 3 bits, then bottom 5 (PLL_K).
    rf_rmw(mcu, MT_RF(0, 31), 0xE0, fi.pllR31_b7b5)
    rf_rmw(mcu, MT_RF(0, 31), MT_RF_PLL_K_MASK, fi.pllR31_b4b0)

    # R30 bit 7 (SDM reset). On SDM channels, pulse it (clear then set).
    if use_sdm:
        rf_clear(mcu, MT_RF(0, 30), MT_RF_SDM_RESET_MASK)
        rf_set(mcu, MT_RF(0, 30), MT_RF_SDM_RESET_MASK)
    else:
        rf_rmw(mcu, MT_RF(0, 30), MT_RF_SDM_RESET_MASK, fi.pllR30_b7)

    # R30 bits 6:2 (sdmmash_prbs,sin) + R30 bit 1 (sdm_bp).
    rf_rmw(mcu, MT_RF(0, 30), MT_RF_SDM_MASH_PRBS_MASK, fi.pllR30_b6b2)
    rf_rmw(mcu, MT_RF(0, 30), MT_RF_SDM_BP_MASK, fi.pllR30_b1 << 1)

    # pll_n: low 8 bits to R29, top bit to R30 bit 0.
    rf_wr(mcu, MT_RF(0, 29), fi.pll_n & 0xFF)
    rf_rmw(mcu, MT_RF(0, 30), 0x1, (fi.pll_n >> 8) & 0x1)

    # R28: bits 7:6 isi_iso, bits 5:4 pfd_dly, bits 3:2 clksel.
    rf_rmw(mcu, MT_RF(0, 28), MT_RF_ISI_ISO_MASK, fi.pllR28_b7b6)
    rf_rmw(mcu, MT_RF(0, 28), MT_RF_PFD_DLY_MASK, fi.pllR28_b5b4)
    rf_rmw(mcu, MT_RF(0, 28), MT_RF_CLK_SEL_MASK, fi.pllR28_b3b2)

    # pll_sdm_k (3 bytes): R26 = low, R27 = mid, R28 bits 1:0 = high.
    rf_wr(mcu, MT_RF(0, 26), fi.pll_sdm_k & 0xFF)
    rf_wr(mcu, MT_RF(0, 27), (fi.pll_sdm_k >> 8) & 0xFF)
    rf_rmw(mcu, MT_RF(0, 28), 0x3, (fi.pll_sdm_k >> 16) & 0x3)

    # R24 bits 1:0 — xo_div.
    rf_rmw(mcu, MT_RF(0, 24), MT_RF_XO_DIV_MASK, fi.pllR24_b1b0)

    # bw_switch_tab — per-channel-bw RF tweaks. Kernel:
    #   if (rf_bw == item->bw_band) write;
    #   else if ((rf_bw == (item->bw_band & 0xFF)) && (rf_band & item->bw_band)) write;
    #
    # Filter then batch into a single CMD_RANDOM_WRITE — was N
    # separate MCU commands (typically 9 for our default ch6 G+BW20).
    bw_pairs = [
        (reg, value)
        for bw_band, reg, value in RF_BW_SWITCH_TAB
        if rf_bw == bw_band
        or (rf_bw == (bw_band & 0xFF) and (rf_band & bw_band))
    ]
    if bw_pairs:
        mcu.random_write(MT_MCU_MEMMAP_RF, bw_pairs)
    logger.debug("phy_set_chan_rf_params: rf_bw_switch_tab → %d writes "
                "(batched) for rf_bw=0x%02x rf_band=0x%04x",
                len(bw_pairs), rf_bw, rf_band)

    # band_switch_tab — per-band RF tweaks. Filter + batch (was 10
    # separate MCU commands for our G band default).
    band_pairs = [
        (reg, value)
        for bw_band, reg, value in RF_BAND_SWITCH_TAB
        if bw_band & rf_band
    ]
    if band_pairs:
        mcu.random_write(MT_MCU_MEMMAP_RF, band_pairs)
    logger.debug("phy_set_chan_rf_params: rf_band_switch_tab → %d writes (batched)",
                len(band_pairs))

    # Clear MT_RF_MISC bits 2-3 (will be set in the ext_pa branch below).
    transport.clear_bits(MT_RF_MISC, 0xC)

    # Ext PA branch — only if EFUSE PA_INT_X bit is CLEAR for this band.
    is_2g = bool(rf_band & RF_G_BAND)
    if efuse_full is not None:
        nic0 = efuse_full.nic_conf_0
        if is_2g:
            pa_int = bool(nic0 & MT_EE_NIC_CONF_0_PA_INT_2G)
        else:
            pa_int = bool(nic0 & MT_EE_NIC_CONF_0_PA_INT_5G)
        ext_pa_enabled = not pa_int
    else:
        # Caller didn't pass efuse_full — assume ext PA off (conservative).
        ext_pa_enabled = False

    if ext_pa_enabled:
        if rf_band & RF_A_BAND:
            transport.set_bits(MT_RF_MISC, MT_RF_MISC_EXT_PA_A_BAND)
        else:
            transport.set_bits(MT_RF_MISC, MT_RF_MISC_EXT_PA_G_BAND)
        # Filter + batch (was up to 11 per-entry MCU commands on 5G).
        ext_pairs = [
            (reg, value)
            for bw_band, reg, value in RF_EXT_PA_TAB
            if bw_band & rf_band
        ]
        if ext_pairs:
            mcu.random_write(MT_MCU_MEMMAP_RF, ext_pairs)
        logger.debug("phy_set_chan_rf_params: ext PA enabled → %d ext_pa_tab writes (batched)",
                    len(ext_pairs))

    # Per-band TX gain / ALC.
    if rf_band & RF_G_BAND:
        transport.write32(MT_TX0_RF_GAIN_ATTEN, 0x63707400)
        # ALC_CFG_1: clear most bits, preserve the upper-masked subset.
        cur = transport.read32(MT_TX_ALC_CFG_1)
        transport.write32(MT_TX_ALC_CFG_1, cur & 0x896400FF)
    else:
        transport.write32(MT_TX0_RF_GAIN_ATTEN, 0x686A7800)
        cur = transport.read32(MT_TX_ALC_CFG_1)
        transport.write32(MT_TX_ALC_CFG_1, cur & 0x890400FF)


def set_channel_20mhz(
    transport: MT76x0UTransport, mcu: MCUChannel, channel: int,
    efuse_full=None, is_mt7630: bool = False,
) -> dict:
    """Port of `mt76x0_phy_set_channel` for 20 MHz monitor RX.

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

    logger.debug("set_channel_20mhz: ch=%d band=%s rf_bw_band=0x%04x",
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

    # Step 9: mt76x0_phy_set_chan_rf_params — PLL programming for the
    # target frequency. [SRC] mt76x0/phy.c:232-397.
    phy_set_chan_rf_params(
        transport, mcu, channel, rf_bw_band, efuse_full=efuse_full,
    )

    # Step 10: Japan TX filter at channel 14.
    if channel == 14:
        transport.set_bits(MT_BBP_CORE(1), 0x20)
    else:
        transport.clear_bits(MT_BBP_CORE(1), 0x20)

    # Step 11: mt76x0_read_rx_gain — read this channel's RX LNA gain from EEPROM
    # so the AGC,8 gain is corrected per-band/subband (the kernel's per-tune RX
    # sensitivity cal). [SRC] mt76x0/phy.c:1002. With no cache (caller passed no
    # efuse_full) lna_gain stays 0 — the pre-fix behaviour.
    from .eeprom import lna_gain_for_channel
    lna_gain = 0
    if efuse_full is not None:
        lna_gain = lna_gain_for_channel(efuse_full.cache, channel)

    # Step 12: mt76x0_phy_set_chan_bbp_params — per-channel BBP tweaks, incl.
    # AGC,8 gain -= lna_gain*2. [SRC] mt76x0/phy.c:400-418.
    phy_set_chan_bbp_params(transport, rf_bw_band, lna_gain=lna_gain)

    # Step 13: enable VCO. [SRC] mt76x0/phy.c:1006.
    rf_set(mcu, MT_RF(0, 4), 0x80)

    # Step 15: mt76x02_init_agc_gain — read AGC gain init values.
    agc_gain_init = init_agc_gain(transport)
    logger.debug("set_channel_20mhz: agc_gain_init = [0x%02x, 0x%02x]",
                agc_gain_init[0], agc_gain_init[1])

    # Step 16: mt76x0_phy_calibrate(power_on=False).
    phy_calibrate(transport, mcu, channel=channel, power_on=False,
                  is_mt7630=is_mt7630)

    # Step 17 SKIPPED: mt76x0_phy_set_txpower — TX path only.
    logger.debug("set_channel_20mhz: M4a complete (TX power deferred - RX-only)")

    # Read post-state for assertions.
    from .constants import MT_BBP_TXBE
    state = {
        "tx_band_cfg": transport.read32(MT_TX_BAND_CFG),
        "ext_cca_cfg": transport.read32(MT_EXT_CCA_CFG),
        "bbp_core_1":  transport.read32(MT_BBP_CORE(1)),
        "bbp_agc_0":   transport.read32(MT_BBP_AGC(0)),
        "bbp_txbe_0":  transport.read32(MT_BBP_TXBE(0)),
    }

    # PLL readbacks for M4a.2 assertion — confirm the freq_item landed.
    rf_regs_to_read = [MT_RF(0, r) for r in (4, 29, 33, 34, 35, 36, 37)]
    try:
        rf_vals = mcu.random_read(MT_MCU_MEMMAP_RF, rf_regs_to_read)
        state["rf_b0_r4"]  = rf_vals[0] & 0xFF   # VCO enable bit 7
        state["rf_b0_r29"] = rf_vals[1] & 0xFF   # pll_n low byte
        state["rf_b0_r33"] = rf_vals[2] & 0xFF
        state["rf_b0_r34"] = rf_vals[3] & 0xFF
        state["rf_b0_r35"] = rf_vals[4] & 0xFF
        state["rf_b0_r36"] = rf_vals[5] & 0xFF
        state["rf_b0_r37"] = rf_vals[6] & 0xFF
    except Exception as e:
        logger.warning("set_channel_20mhz: PLL readback failed (non-fatal): %s", e)

    # M4a.3 readbacks: BBP(AGC, 8) and AGC init gains.
    state["bbp_agc_8"] = transport.read32(MT_BBP_AGC(8))
    state["agc_gain_init_0"] = agc_gain_init[0]
    state["agc_gain_init_1"] = agc_gain_init[1]
    return state


def phy_init(
    transport: MT76x0UTransport, mcu: MCUChannel, efuse_full, is_mt7630: bool = False,
) -> None:
    """Port of `mt76x0_phy_init` (mt76x0/phy.c:1207-1215).

    Wraps the four PHY init steps:
      1. phy_ant_select
      2. phy_rf_init
      3. phy_set_rxpath
      4. phy_set_txdac

    ``is_mt7630`` is forwarded to phy_rf_init (RF(5,2) override); default False =
    the captured 0x7650 reference. The band caps (has_2ghz/has_5ghz) that drive
    phy_ant_select already carry any is_mt7630/no_2ghz mask applied when the
    efuse was decoded.

    Skips the kernel's `INIT_DELAYED_WORK(&dev->cal_work, ...)` — that
    schedules periodic calibration, which is a wifit3 monitor-mode no-op.
    """
    phy_ant_select(
        transport,
        has_2ghz=efuse_full.has_2ghz,
        has_5ghz=efuse_full.has_5ghz,
        efuse_cache=efuse_full.cache,
    )
    phy_rf_init(mcu, freq_offset=efuse_full.freq_offset, is_mt7630=is_mt7630)
    phy_set_rxpath(transport)
    phy_set_txdac(transport)


def phy_wait_bbp_ready(transport: MT76x0UTransport) -> int:
    """Port of `mt76x0_phy_wait_bbp_ready` (mt76x0/phy.c:185-203).

    Polls `MT_BBP(CORE, 0)` up to 20 times, breaking when the value is
    neither 0 nor all-1s. Kernel busy-polls — on USB each
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
    logger.debug("init_bbp: BBP version = 0x%08x", bbp_version)

    logger.debug("init_bbp: uploading bbp_init_tab (%d pairs via MCU)",
                len(BBP_INIT_TAB))
    mcu.random_write(MT_MCU_MEMMAP_WLAN, BBP_INIT_TAB)

    # Switch-tab: filter by RF_G_BAND | RF_BW_20 default mask, then direct-write.
    # [SRC] mt76x0/init.c:97-103.
    want = RF_G_BAND | RF_BW_20
    switch_pairs = filter_bbp_switch_tab(want)
    logger.debug("init_bbp: writing %d filtered bbp_switch_tab entries "
                "via direct vendor xfers (mask=0x%04x)",
                len(switch_pairs), want)
    for reg, value in switch_pairs:
        transport.write32(reg, value)

    logger.debug("init_bbp: uploading dcoc_tab (%d pairs via MCU)",
                len(DCOC_TAB))
    mcu.random_write(MT_MCU_MEMMAP_WLAN, DCOC_TAB)

    logger.debug("init_bbp: done")
    return bbp_version
