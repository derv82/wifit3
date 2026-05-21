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
