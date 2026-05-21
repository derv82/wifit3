"""MT76x0U PHY (BBP + RF) bring-up.

M3b scope: `mt76x0_init_bbp` + `mt76x0_phy_wait_bbp_ready`.
M3d will add `mt76x0_phy_init` (ant_select + rf_init + rxpath + txdac).

[SRC] mt76x0/init.c:87-108 (`mt76x0_init_bbp`)
[SRC] mt76x0/phy.c:185-203 (`mt76x0_phy_wait_bbp_ready`)
"""
from __future__ import annotations

import logging

from .constants import (
    MT_BBP_CORE,
    MT_MCU_MEMMAP_WLAN,
    RF_BW_20,
    RF_G_BAND,
)
from .initvals_bbp import BBP_INIT_TAB, DCOC_TAB, filter_bbp_switch_tab
from .mcu import MCUChannel
from .transport import MT76x0UTransport

logger = logging.getLogger(__name__)


class PHYInitError(RuntimeError):
    """A PHY init step failed (BBP not ready, table upload failure, ...)."""


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
