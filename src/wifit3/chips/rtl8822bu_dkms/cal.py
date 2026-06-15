"""RTL8822BU post-PHY calibration setup — the sequence after the BB/RF tables (op 9410+).

The vendor runs, in order: config_phydm_trx_mode (TX/RX path + RF mode), then IQK, LCK, the
one-time DPK, and the phydm DM init (DIG/CCK-PD — the RX-detection seed). This module ports them
gate-driven against the cold-boot capture (`verify_pcap.py`); `bringup.cold_bringup` chains them.

config_phydm_trx_mode_8822b `[SRC] phydm_hal_api8822b.c:2449` sets the 2T2R path config for normal
operation: both radios active (0xC08/0xE08 = 0x3231), the TX path (Nsts/1ss antenna map) and RX
path (MRC/antenna-weight) BB regs, an RF mode-table write+poll (RF 0xEF/0x33/0x3E/0x3F), then the
igi-toggle + ccapar re-apply. On this card tx=rx=BB_PATH_AB and the 1ss/CCK path is A; central_ch
is still 0 here, so ccapar takes col 1 (2.4G/2R) and phydm_rfe is a no-op (channel 0).
"""
from __future__ import annotations

from . import chan, sipi

BB_PATH_A, BB_PATH_B, BB_PATH_AB = 1, 2, 3
RF_0xEF, RF_0x33, RF_0x3E, RF_0x3F = 0xEF, 0x33, 0x3E, 0x3F
_FULL = sipi.RFREGOFFSETMASK


def _config_tx_path(t, tx_path: int, sel_1ss: int, sel_cck: int) -> None:
    """[SRC] phydm_config_tx_path_8822b + the CCK/OFDM TX-path helpers."""
    sipi.set_bb_reg(t, 0x093C, (1 << 19) | (1 << 18), 0x3)     # TX antenna by Nsts
    sipi.set_bb_reg(t, 0x080C, (1 << 29) | (1 << 28), 0x1)
    sipi.set_bb_reg(t, 0x080C, 1 << 30, 0x1)                   # CCK TX path by 0xa07[7]
    sipi.set_bb_reg(t, 0x080C, 0xFF, (tx_path << 4) | tx_path)  # TX path HW block enable
    # CCK TX path
    sipi.set_bb_reg(t, 0x0A04, 0xF0000000, {BB_PATH_A: 0x8, BB_PATH_B: 0x4}.get(sel_cck, 0xC))
    # OFDM TX logic map / path-en (tx_path_en == AB on this card)
    if tx_path == BB_PATH_A:
        sipi.set_bb_reg(t, 0x093C, 0xFFF00000, 0x001)
    elif tx_path == BB_PATH_B:
        sipi.set_bb_reg(t, 0x093C, 0xFFF00000, 0x002)
    else:                                                      # BB_PATH_AB, by 1ss selection
        m = {BB_PATH_A: 0x001, BB_PATH_B: 0x002}.get(sel_1ss, 0x043)
        sipi.set_bb_reg(t, 0x093C, 0xFFF00000, m)
        sipi.set_bb_reg(t, 0x0940, 0xFFF0, 0x043)
    if tx_path in (BB_PATH_A, BB_PATH_B):                      # Nsts=2 map (single-path only)
        sipi.set_bb_reg(t, 0x0940, 0xF0, 0x1)
        sipi.set_bb_reg(t, 0x0940, 0xFF00, 0x0)


def _config_rx_path(t, rx_path: int) -> None:
    """[SRC] phydm_config_rx_path_8822b: CCK MRC off, RX path enable, antenna-weight by Nrx."""
    sipi.set_bb_reg(t, 0x0A2C, 1 << 22, 0x0)                   # disable MRC for CCK CCA
    sipi.set_bb_reg(t, 0x0A2C, 1 << 18, 0x0)                   # disable MRC for CCK barker
    if rx_path & BB_PATH_A:
        sipi.set_bb_reg(t, 0x0A04, 0x0F000000, 0x0)
    elif rx_path & BB_PATH_B:
        sipi.set_bb_reg(t, 0x0A04, 0x0F000000, 0x5)
    sipi.set_bb_reg(t, 0x0808, 0xFF, (rx_path << 4) | rx_path)  # RX path enable
    ant_wgt = 0x0 if rx_path in (BB_PATH_A, BB_PATH_B) else 0x1
    sipi.set_bb_reg(t, 0x1904, 1 << 16, ant_wgt)              # antenna weighting
    sipi.set_bb_reg(t, 0x0800, 1 << 28, ant_wgt)             # htstf ant-wgt
    sipi.set_bb_reg(t, 0x0850, 1 << 23, ant_wgt)             # MRC mode (ZF eqz)


def config_trx_mode(t, central_ch: int = 0, tx_path: int = BB_PATH_AB,
                    rx_path: int = BB_PATH_AB, sel_1ss: int = BB_PATH_A) -> None:
    """[SRC] config_phydm_trx_mode_8822b — 2T2R path/mode config after the BB/RF tables."""
    sipi.set_bb_reg(t, 0x0C08, 0xFFFF, 0x3231 if (tx_path | rx_path) & BB_PATH_A else 0x1111)
    sipi.set_bb_reg(t, 0x0E08, 0xFFFF, 0x3231 if (tx_path | rx_path) & BB_PATH_B else 0x1111)
    _config_tx_path(t, tx_path, sel_1ss, sel_1ss)
    _config_rx_path(t, rx_path)
    # RF mode-table write + poll until RF_A 0x33 reads back 0x00001 (replay feeds the read).
    for _ in range(100):
        sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0xEF, _FULL, 0x80000)
        sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0x33, _FULL, 0x00001)
        if sipi.read_rf_reg(t, sipi.RF_PATH_A, RF_0x33, _FULL) == 0x00001:
            break
    # Normal mode (not MP/antenna-test): the path-A 3-wire mode-table tail.
    sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0xEF, _FULL, 0x80000)
    sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0x33, _FULL, 0x00001)
    sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0x3E, _FULL, 0x00034)
    sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0x3F, _FULL, 0x4080C)
    sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0xEF, _FULL, 0x00000)
    sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0xEF, _FULL, 0x00000)
    chan._igi_toggle(t)                                       # let RF enter RX mode
    chan._ccapar_by_rfe(t, central_ch, bw20=True)             # central_ch 0 -> col 1 (2.4G/2R)
    # phydm_rfe_8822b(central_ch): channel 0 -> returns without writing.
