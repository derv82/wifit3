"""RX bring-up — ath9k_host_rx_init and the MAC RX-control helpers it drives.

Ported from htc_drv_txrx.c (calcrxfilter, opmode_init, host_rx_init), hw.c (get/set rx filter,
mcast filter), ar9002_mac.c (rx_enable) and mac.c (startpcureceive). This is the RX *control*
path (enable DMA, program filters, start the PCU); the RX *frame* decode lands with the data
pipe later.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import ani
from . import reg as R
from .hw import AthHw


@dataclass
class FilterFlags:
    """The mac80211 FIF_* filter flags configure_filter passes through (priv->rxfilter)."""
    probe_req: bool = False
    control: bool = False
    pspoll: bool = False
    bcn_prbresp_promisc: bool = False
    other_bss: bool = False
    mcast_action: bool = False


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


def calcrxfilter(hw: AthHw, flags: FilterFlags | None = None, nvifs: int | None = None,
                 conf_is_ht: bool | None = None) -> int:
    """ath9k_htc_calcrxfilter [SRC] htc_drv_txrx.c:869 — base ucast/bcast/mcast plus the bits the
    mac80211 FIF flags, monitor state and opmode select. The flags/nvifs/ht inputs persist on the
    hw (priv->rxfilter etc.); the STATION default (no flags, one vif, not monitoring) folds down
    to 0x207 (ucast|bcast|mcast|mybeacon)."""
    flags = flags if flags is not None else (hw.rxfilter_flags or FilterFlags())
    nvifs = hw.nvifs if nvifs is None else nvifs
    conf_is_ht = hw.conf_is_ht if conf_is_ht is None else conf_is_ht
    preserve = R.ATH9K_RX_FILTER_PHYERR | R.ATH9K_RX_FILTER_PHYRADAR
    rfilt = (getrxfilter(hw) & preserve) | R.ATH9K_RX_FILTER_UCAST \
        | R.ATH9K_RX_FILTER_BCAST | R.ATH9K_RX_FILTER_MCAST
    if flags.probe_req:
        rfilt |= R.ATH9K_RX_FILTER_PROBEREQ
    if hw.is_monitoring:
        rfilt |= R.ATH9K_RX_FILTER_PROM
    if flags.control:
        rfilt |= R.ATH9K_RX_FILTER_CONTROL
    if hw.opmode == R.IFTYPE_STATION and nvifs <= 1 and not flags.bcn_prbresp_promisc:
        rfilt |= R.ATH9K_RX_FILTER_MYBEACON
    else:
        rfilt |= R.ATH9K_RX_FILTER_BEACON
    if conf_is_ht:
        rfilt |= R.ATH9K_RX_FILTER_COMP_BAR | R.ATH9K_RX_FILTER_UNCOMP_BA_BAR
    if flags.pspoll:
        rfilt |= R.ATH9K_RX_FILTER_PSPOLL
    if nvifs > 1 or flags.other_bss or flags.mcast_action:
        rfilt |= R.ATH9K_RX_FILTER_MCAST_BCAST_ALL
    return rfilt


def configure_filter(hw: AthHw, flags: FilterFlags) -> None:
    """ath9k_htc_configure_filter [SRC] htc_drv_main.c:1267 — persist the mac80211 flags
    (priv->rxfilter) then recompute and install the RX filter."""
    hw.rxfilter_flags = flags
    setrxfilter(hw, calcrxfilter(hw))


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
