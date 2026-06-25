"""RX bring-up — ath9k_host_rx_init and the MAC RX-control helpers it drives.

Ported from htc_drv_txrx.c (calcrxfilter, opmode_init, host_rx_init), hw.c (get/set rx filter,
mcast filter), ar9002_mac.c (rx_enable) and mac.c (startpcureceive). This is the RX *control*
path (enable DMA, program filters, start the PCU); the RX *frame* decode lands with the data
pipe later.
"""
from __future__ import annotations

from . import ani
from . import reg as R
from .hw import AthHw


def rxena(hw: AthHw) -> None:
    """ar9002_hw_rx_enable [SRC] ar9002_mac.c:22 — kick RX DMA."""
    hw.write(R.AR_CR, R.AR_CR_RXE)


def getrxfilter(hw: AthHw) -> int:
    """ath9k_hw_getrxfilter [SRC] hw.c:2867 — read back the current filter, folding the two
    phy-error enables into the ATH9K_RX_FILTER_PHY* bits."""
    bits = hw.read(R.AR_RX_FILTER)
    phybits = hw.read(R.AR_PHY_ERR)
    if phybits & R.AR_PHY_ERR_RADAR:
        bits |= R.ATH9K_RX_FILTER_PHYRADAR
    if phybits & (R.AR_PHY_ERR_OFDM_TIMING | R.AR_PHY_ERR_CCK_TIMING):
        bits |= R.ATH9K_RX_FILTER_PHYERR
    return bits


def setrxfilter(hw: AthHw, bits: int) -> None:
    """ath9k_hw_setrxfilter [SRC] hw.c:2881 — program the MAC RX filter and the phy-error mask,
    toggling AR_RXCFG zero-length-frame DMA to match."""
    hw.enable_write_buffer()
    hw.write(R.AR_RX_FILTER, bits)
    phybits = 0
    if bits & R.ATH9K_RX_FILTER_PHYRADAR:
        phybits |= R.AR_PHY_ERR_RADAR
    if bits & R.ATH9K_RX_FILTER_PHYERR:
        phybits |= R.AR_PHY_ERR_OFDM_TIMING | R.AR_PHY_ERR_CCK_TIMING
    hw.write(R.AR_PHY_ERR, phybits)
    if phybits:
        hw.rmw(R.AR_RXCFG, R.AR_RXCFG_ZLFDMA, 0)
    else:
        hw.rmw(R.AR_RXCFG, 0, R.AR_RXCFG_ZLFDMA)
    hw.write_flush()


def setmcastfilter(hw: AthHw, filter0: int, filter1: int) -> None:
    """ath9k_hw_setmcastfilter [SRC] hw.c:2989 — install the two multicast hash words."""
    hw.write(R.AR_MCAST_FIL0, filter0)
    hw.write(R.AR_MCAST_FIL1, filter1)


def calcrxfilter(hw: AthHw) -> int:
    """ath9k_htc_calcrxfilter [SRC] htc_drv_txrx.c:869 — STATION default: ucast/bcast/mcast plus
    mybeacon, preserving any phy-error bits already set. No monitor / probe-req / control here."""
    preserve = R.ATH9K_RX_FILTER_PHYERR | R.ATH9K_RX_FILTER_PHYRADAR
    rfilt = (getrxfilter(hw) & preserve) | R.ATH9K_RX_FILTER_UCAST \
        | R.ATH9K_RX_FILTER_BCAST | R.ATH9K_RX_FILTER_MCAST
    # STATION, single vif, not bcn-promisc -> only our own beacons.
    rfilt |= R.ATH9K_RX_FILTER_MYBEACON
    return rfilt


def opmode_init(hw: AthHw) -> None:
    """ath9k_htc_opmode_init [SRC] htc_drv_txrx.c:916 — program the RX + multicast filters."""
    setrxfilter(hw, calcrxfilter(hw))
    setmcastfilter(hw, 0xFFFFFFFF, 0xFFFFFFFF)


def startpcureceive(hw: AthHw, is_scanning: bool = False) -> None:
    """ath9k_hw_startpcureceive [SRC] mac.c:675 — enable MIB counters, reset ANI, then clear the
    PCU RX block/abort bits so the receiver runs."""
    ani.enable_mib_counters(hw)
    ani.ani_reset(hw, is_scanning)
    hw.rmw(R.AR_DIAG_SW, 0, R.AR_DIAG_RX_DIS | R.AR_DIAG_RX_ABORT)


def host_rx_init(hw: AthHw) -> None:
    """ath9k_host_rx_init [SRC] htc_drv_txrx.c:930 — enable RX DMA, program filters, start PCU."""
    rxena(hw)
    opmode_init(hw)
    startpcureceive(hw)
