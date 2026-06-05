"""RTL8812AU I/Q calibration (IQK) — 2T2R, 2.4 GHz, 20 MHz, B-cut.

Ports the phydm CE IQK ``_phy_iq_calibrate_8812a`` and its helpers
([SRC] halrf_8812a_ce.c, aircrack-rtl8812au-v5.6.4.2 / morrownr build — byte-identical to
the captures-tree CE variant). This is the routine the morrownr ``8812au`` vendor driver
actually runs (``halrf_iqk_trigger`` -> ``phy_iq_calibrate_8812a`` ->
``_phy_iq_calibrate_8812a``), NOT the legacy ADDA_REG[]/0x8AC-tone ``PHY_IQCalibrate_8812A``
from the classic out-of-tree driver. The IQK locks the RX demodulator: without it the I/Q
imbalance leaves every MPDU a CRC-error.

Flow ([SRC] _phy_iq_calibrate_8812a, halrf_8812a_ce.c:1127):
    backup MAC/BB + AFE + RF -> configure MAC for cal -> the unified TX+RX IQK loop
    (``_iqk_tx_8812a``: AFE-on, per-path TX-IQK measure/vote, LOK load, per-path RX-IQK
    measure/vote, fill coefficients) -> restore RF -> restore AFE -> restore MAC/BB.

The single ``_iqk_tx_8812a`` mega-function (despite its name) drives BOTH paths' TX *and*
RX IQK in one interleaved one-shot loop; ``cal_num``=10 caps the retry/averaging passes.
Each path's (X,Y) is accepted only when two passes agree within +/-4 (the "vote"), then
averaged. Page select: 0x82C[31]=0 -> Page C, =1 -> Page C1 (a real BB write replayed
verbatim; addresses are flat in our control-transfer transport).

Gated for this card (USB / rfe_type 0 / 2T2R / B-cut / 2.4 GHz / 20 MHz):
  * ``VDF_enable`` is hardcoded False in the vendor source (only the dead ``#if 0`` set it
    for 80 MHz), so the VDF branches are omitted — we run the non-VDF (20/40 MHz) path.
  * ``dpk_done`` is always False on 8812a (DPK is 8814/8822+; the setter is ``#if 0``), so
    the ``if (!dpk_done)`` writes (0xCC4/0xEC4 BIT29) always fire.
  * 5 GHz (``band_type == ODM_BAND_5G``) and PCIE-interface branches resolve to their
    2.4 GHz / USB arms here; ``is_2g`` / fixed USB select them.
  * ``ext_pa_5g`` / ``ext_pa`` = 0 and ``rfe_type`` = 0 select the no-ext-PA register
    values throughout (AWUS036ACH is a no-BT, no-ext-PA board).
"""
from __future__ import annotations

import time

from ..rtl88xxau_base.sipi import (
    RF_PATH_A,
    RF_PATH_B,
    query_rf,
    set_bb,
    set_rf_reg,
)

RFREGOFFSETMASK = 0x000FFFFF       # RFREGOFFSETMASK / RF full-dword mask
_PAGE = 0x082C                     # R_0x82c BIT31: 0 -> Page C, 1 -> Page C1
_CAL_NUM = 10                      # cal_num: max retry/averaging passes

# [SRC] _phy_iq_calibrate_8812a:1130-1134 — backup register lists (ported verbatim).
_MACBB_REG = (0x520, 0x550, 0x808, 0xA04, 0x90C, 0xC00, 0xE00, 0x838, 0x82C)
_AFE_REG = (0xC5C, 0xC60, 0xC64, 0xC68, 0xCB0, 0xCB4,
            0xE5C, 0xE60, 0xE64, 0xE68, 0xEB0, 0xEB4)
_RF_REG = (0x65, 0x8F, 0x00)


def _ms(t_ms: float) -> None:
    """ODM_delay_ms — IQK tone-measurement settling; the delays are load-bearing."""
    time.sleep(t_ms / 1000.0)


# --- backup / restore -------------------------------------------------------

def _backup_mac_bb(t) -> dict:
    # [SRC] _iqk_backup_mac_bb_8812a:521
    set_bb(t, _PAGE, 0x80000000, 0x0)                      # Page C
    return {r: t.read32(r) for r in _MACBB_REG}


def _backup_afe(t) -> dict:
    # [SRC] _iqk_backup_afe_8812a:544
    set_bb(t, _PAGE, 0x80000000, 0x0)                      # Page C
    return {r: t.read32(r) for r in _AFE_REG}


def _backup_rf(t) -> tuple[dict, dict]:
    # [SRC] _iqk_backup_rf_8812a:532 — both paths, full dword.
    set_bb(t, _PAGE, 0x80000000, 0x0)                      # Page C
    a = {r: query_rf(t, RF_PATH_A, r, RFREGOFFSETMASK) for r in _RF_REG}
    b = {r: query_rf(t, RF_PATH_B, r, RFREGOFFSETMASK) for r in _RF_REG}
    return a, b


def _restore_mac_bb(t, backup: dict) -> None:
    # [SRC] _iqk_restore_mac_bb_8812a:554
    set_bb(t, _PAGE, 0x80000000, 0x0)                      # Page C
    for r in _MACBB_REG:
        t.write32(r, backup[r])


def _restore_rf(t, path: int, backup: dict) -> None:
    # [SRC] _iqk_restore_rf_8812a:564
    set_bb(t, _PAGE, 0x80000000, 0x0)                      # Page C
    for r in _RF_REG:
        set_rf_reg(t, path, r, RFREGOFFSETMASK, backup[r])
    set_rf_reg(t, path, 0xEF, RFREGOFFSETMASK, 0x0)


def _restore_afe(t, backup: dict) -> None:
    # [SRC] _iqk_restore_afe_8812a:586 — reload AFE, then re-arm the IQC fill registers
    # on both paths (dpk_done is always False on 8812a, so the BIT29 writes always fire).
    set_bb(t, _PAGE, 0x80000000, 0x0)                      # Page C
    for r in _AFE_REG:
        t.write32(r, backup[r])
    set_bb(t, _PAGE, 0x80000000, 0x1)                      # Page C1
    t.write32(0xC80, 0x0)
    t.write32(0xC84, 0x0)
    t.write32(0xC88, 0x0)
    t.write32(0xC8C, 0x3C000000)
    set_bb(t, 0xC90, 0x80, 0x1)
    set_bb(t, 0xCC4, 0x40000, 0x1)
    set_bb(t, 0xCC4, 0x20000000, 0x1)                      # !dpk_done
    set_bb(t, 0xCC8, 0x20000000, 0x1)
    t.write32(0xE80, 0x0)
    t.write32(0xE84, 0x0)
    t.write32(0xE88, 0x0)
    t.write32(0xE8C, 0x3C000000)
    set_bb(t, 0xE90, 0x80, 0x1)
    set_bb(t, 0xEC4, 0x40000, 0x1)
    set_bb(t, 0xEC4, 0x20000000, 0x1)                      # !dpk_done
    set_bb(t, 0xEC8, 0x20000000, 0x1)


def _configure_mac(t) -> None:
    # [SRC] _iqk_configure_mac_8812a:619 — MAC register setting for calibration.
    set_bb(t, _PAGE, 0x80000000, 0x0)                      # Page C
    t.write8(0x522, 0x3F)
    set_bb(t, 0x550, (1 << 11) | (1 << 3), 0x0)
    t.write8(0x808, 0x00)                                  # RX ante off
    set_bb(t, 0x838, 0xF, 0xC)                             # CCA off
    t.write8(0xA07, 0xF)                                   # CCK RX path off


# --- coefficient fill -------------------------------------------------------

def _rx_fill_iqc(t, path: int, rx_x: int, rx_y: int) -> None:
    # [SRC] _iqk_rx_fill_iqc_8812a:431 — fill the RX IQC into 0xC10 (A) / 0xE10 (B).
    # The C takes RX_X/RX_Y as ``unsigned int``: a negative averaged coefficient becomes a
    # huge unsigned value, so its ``>> 1`` trips the >= 0x112 out-of-range clamp. Reinterpret
    # the signed coefficient as unsigned 32-bit before the range test to match that.
    rx_x &= 0xFFFFFFFF
    rx_y &= 0xFFFFFFFF
    reg = 0xC10 if path == RF_PATH_A else 0xE10
    set_bb(t, _PAGE, 0x80000000, 0x0)                      # Page C
    if (rx_x >> 1) >= 0x112 or (0x12 <= (rx_y >> 1) <= 0x3EE):
        set_bb(t, reg, 0x000003FF, 0x100)
        set_bb(t, reg, 0x03FF0000, 0x0)
    else:
        set_bb(t, reg, 0x000003FF, (rx_x >> 1) & 0x3FF)
        set_bb(t, reg, 0x03FF0000, (rx_y >> 1) & 0x3FF)


def _tx_fill_iqc(t, path: int, tx_x: int, tx_y: int) -> None:
    # [SRC] _iqk_tx_fill_iqc_8812a:476 — fill the TX IQC into Page-C1 coefficient regs.
    # dpk_done is always False on 8812a, so the 0xCC4/0xEC4 BIT29 write always fires.
    if path == RF_PATH_A:
        r90, rc4, rc8, rcc, rd4 = 0xC90, 0xCC4, 0xCC8, 0xCCC, 0xCD4
    else:
        r90, rc4, rc8, rcc, rd4 = 0xE90, 0xEC4, 0xEC8, 0xECC, 0xED4
    set_bb(t, _PAGE, 0x80000000, 0x1)                      # Page C1
    set_bb(t, r90, 0x80, 0x1)
    set_bb(t, rc4, 0x40000, 0x1)
    set_bb(t, rc4, 0x20000000, 0x1)                        # !dpk_done
    set_bb(t, rc8, 0x20000000, 0x1)
    set_bb(t, rcc, 0x000007FF, tx_y & 0x7FF)
    set_bb(t, rd4, 0x000007FF, tx_x & 0x7FF)


# --- the unified TX + RX IQK loop (both paths) ------------------------------

def _coeff(reg_val: int) -> int:
    """Decode an IQK coefficient field as the vendor's signed 11-bit value.

    The vendor stores ``odm_get_bb_reg(R_0xd00, 0x07ff0000) << 21`` in a signed int and
    later reads it back with an arithmetic ``>> 21`` — net effect: the 11-bit field is
    sign-extended (bit 10 = sign). We compute that directly so the +/-4 vote, the average,
    and the (negative) coefficients written back all match the C exactly.
    """
    field = (reg_val >> 16) & 0x7FF
    return field - 0x800 if field & 0x400 else field


def _avg2(a: int, b: int) -> int:
    """C ``int`` division truncates toward zero (not Python floor); coefficients are signed."""
    s = a + b
    return -((-s) // 2) if s < 0 else s // 2


def _vote(temp: list, n: int, xi: int, yi: int) -> tuple[bool, int, int]:
    """[SRC] the +/-4 stability vote + average over collected passes.

    ``temp`` rows hold the signed-11-bit X/Y per pass; accept the first pair within +/-4 on
    both axes and return their (truncate-toward-zero) average.
    """
    for i in range(n):
        for ii in range(i + 1, n):
            dx = temp[i][xi] - temp[ii][xi]
            if -4 < dx < 4:
                dy = temp[i][yi] - temp[ii][yi]
                if -4 < dy < 4:
                    return True, _avg2(temp[i][xi], temp[ii][xi]), _avg2(temp[i][yi], temp[ii][yi])
    return False, 0, 0


def _iqk_measure(t, is_2g: bool) -> tuple:
    """[SRC] _iqk_tx_8812a:632 — TX-IQK then RX-IQK, both paths, with retry/averaging.

    Returns (tx0_ok, TX0_X, TX0_Y, tx1_ok, TX1_X, TX1_Y, rx0_ok, RX0_X, RX0_Y,
             rx1_ok, RX1_X, RX1_Y). Coefficients are in the raw (>>21) coefficient units.
    """
    tx_iqc = [0, 0, 0, 0]   # [TX0_X, TX0_Y, TX1_X, TX1_Y]
    rx_iqc = [0, 0, 0, 0]   # [RX0_X, RX0_Y, RX1_X, RX1_Y]
    tx0_fin = tx1_fin = rx0_fin = rx1_fin = False

    set_bb(t, _PAGE, 0x80000000, 0x0)                      # Page C
    # ====== path-A + path-B AFE all on ======
    t.write32(0xC60, 0x77777777)                           # Port 0 DAC/ADC on
    t.write32(0xC64, 0x77777777)
    t.write32(0xE60, 0x77777777)                           # Port 1 DAC/ADC on
    t.write32(0xE64, 0x77777777)
    t.write32(0xC68, 0x19791979)
    t.write32(0xE68, 0x19791979)
    set_bb(t, 0xC00, 0xF, 0x4)                             # hardware 3-wire off
    set_bb(t, 0xE00, 0xF, 0x4)
    set_bb(t, 0xC5C, (1 << 26) | (1 << 25) | (1 << 24), 0x7)   # DAC/ADC 160 MHz
    set_bb(t, 0xE5C, (1 << 26) | (1 << 25) | (1 << 24), 0x7)

    # ====== TX IQK RF setting, both paths ======
    set_bb(t, _PAGE, 0x80000000, 0x0)                      # Page C
    for p in (RF_PATH_A, RF_PATH_B):
        set_rf_reg(t, p, 0xEF, RFREGOFFSETMASK, 0x80002)
        set_rf_reg(t, p, 0x30, RFREGOFFSETMASK, 0x20000)
        set_rf_reg(t, p, 0x31, RFREGOFFSETMASK, 0x3FFFD)
        set_rf_reg(t, p, 0x32, RFREGOFFSETMASK, 0xFE83F)
        set_rf_reg(t, p, 0x65, RFREGOFFSETMASK, 0x931D5)
        set_rf_reg(t, p, 0x8F, RFREGOFFSETMASK, 0x8A001)
    t.write32(0x90C, 0x00008000)
    set_bb(t, 0xC94, 0x1, 0x1)
    set_bb(t, 0xE94, 0x1, 0x1)
    t.write32(0x978, 0x29002000)                           # TX (X,Y)
    t.write32(0x97C, 0xA9002000)                           # RX (X,Y)
    t.write32(0x984, 0x00462910)                           # [0]:AGC_en [15]:idac_K_Mask
    set_bb(t, _PAGE, 0x80000000, 0x1)                      # Page C1

    # ext_pa_5g == 0 -> 0x821403f1 (no-ext-PA); rfe_type 0.
    t.write32(0xC88, 0x821403F1)
    t.write32(0xE88, 0x821403F1)
    # band 2.4G -> 0x28163e96 (5G would be 0x68163e96).
    band_c8c = 0x28163E96 if is_2g else 0x68163E96
    t.write32(0xC8C, band_c8c)
    t.write32(0xE8C, band_c8c)

    # --- TX IQK measurement loop (VDF disabled: 20/40 MHz path) ---
    t.write32(0xC80, 0x18008C10)                           # TX_Tone_idx[9:0], TX_Tone=16
    t.write32(0xC84, 0x38008C10)                           # RX_Tone_idx[9:0]
    t.write32(0xCE8, 0x00000000)
    t.write32(0xE80, 0x18008C10)
    t.write32(0xE84, 0x38008C10)
    t.write32(0xEE8, 0x00000000)

    tx_temp = [[0, 0, 0, 0] for _ in range(_CAL_NUM)]
    tx0_avg = tx1_avg = cal0_retry = cal1_retry = 0
    # IQK*_ready persist across one-shots: a finished path keeps its last (ready) value, so
    # the (ready0 && ready1) break still fires while only the unfinished path is re-polled.
    iqk0_ready = iqk1_ready = False
    while True:
        # one shot
        t.write32(0xCB8, 0x00100000)
        t.write32(0xEB8, 0x00100000)
        t.write32(0x980, 0xFA000000)
        t.write32(0x980, 0xF8000000)
        _ms(10)
        t.write32(0xCB8, 0x00000000)
        t.write32(0xEB8, 0x00000000)
        delay_count = 0
        while True:
            if not tx0_fin:
                iqk0_ready = bool(t.read32(0xD00) & (1 << 10))
            if not tx1_fin:
                iqk1_ready = bool(t.read32(0xD40) & (1 << 10))
            if (iqk0_ready and iqk1_ready) or delay_count > 20:
                break
            _ms(1)
            delay_count += 1
        if delay_count < 20:
            tx0_fail = bool(t.read32(0xD00) & (1 << 12))
            tx1_fail = bool(t.read32(0xD40) & (1 << 12))
            if not (tx0_fail or tx0_fin):
                t.write32(0xCB8, 0x02000000)
                tx_temp[tx0_avg][0] = _coeff(t.read32(0xD00))
                t.write32(0xCB8, 0x04000000)
                tx_temp[tx0_avg][1] = _coeff(t.read32(0xD00))
                tx0_avg += 1
            else:
                cal0_retry += 1
                if cal0_retry == 10:
                    break
            if not (tx1_fail or tx1_fin):
                t.write32(0xEB8, 0x02000000)
                tx_temp[tx1_avg][2] = _coeff(t.read32(0xD40))
                t.write32(0xEB8, 0x04000000)
                tx_temp[tx1_avg][3] = _coeff(t.read32(0xD40))
                tx1_avg += 1
            else:
                cal1_retry += 1
                if cal1_retry == 10:
                    break
        else:
            cal0_retry += 1
            cal1_retry += 1
            if cal0_retry == 10:
                break
        if tx0_avg >= 2:
            ok, x, y = _vote(tx_temp, tx0_avg, 0, 1)
            if ok:
                tx_iqc[0], tx_iqc[1], tx0_fin = x, y, True
        if tx1_avg >= 2:
            ok, x, y = _vote(tx_temp, tx1_avg, 2, 3)
            if ok:
                tx_iqc[2], tx_iqc[3], tx1_fin = x, y, True
        if tx0_fin and tx1_fin:
            break
        if (cal0_retry + tx0_avg) >= 10 or (cal1_retry + tx1_avg) >= 10:
            break

    # Load LOK: RF 0x58[18:8] <- RF 0x08[19:10], both paths.
    set_bb(t, _PAGE, 0x80000000, 0x0)                      # Page C
    set_rf_reg(t, RF_PATH_A, 0x58, 0x7FE00, query_rf(t, RF_PATH_A, 0x08, 0xFFC00))
    set_rf_reg(t, RF_PATH_B, 0x58, 0x7FE00, query_rf(t, RF_PATH_B, 0x08, 0xFFC00))
    set_bb(t, _PAGE, 0x80000000, 0x1)                      # Page C1

    # --- RX IQK setup (per path, only where TX IQK succeeded) ---
    set_bb(t, _PAGE, 0x80000000, 0x0)                      # Page C
    if tx0_fin:
        set_rf_reg(t, RF_PATH_A, 0xEF, RFREGOFFSETMASK, 0x80000)
        set_rf_reg(t, RF_PATH_A, 0x30, RFREGOFFSETMASK, 0x30000)
        set_rf_reg(t, RF_PATH_A, 0x31, RFREGOFFSETMASK, 0x3F7FF)
        set_rf_reg(t, RF_PATH_A, 0x32, RFREGOFFSETMASK, 0xFE7BF)
        set_rf_reg(t, RF_PATH_A, 0x8F, RFREGOFFSETMASK, 0x88001)
        set_rf_reg(t, RF_PATH_A, 0x65, RFREGOFFSETMASK, 0x931D1)
        set_rf_reg(t, RF_PATH_A, 0xEF, RFREGOFFSETMASK, 0x00000)
    if tx1_fin:
        set_rf_reg(t, RF_PATH_B, 0xEF, RFREGOFFSETMASK, 0x80000)
        set_rf_reg(t, RF_PATH_B, 0x30, RFREGOFFSETMASK, 0x30000)
        set_rf_reg(t, RF_PATH_B, 0x31, RFREGOFFSETMASK, 0x3F7FF)
        set_rf_reg(t, RF_PATH_B, 0x32, RFREGOFFSETMASK, 0xFE7BF)
        set_rf_reg(t, RF_PATH_B, 0x8F, RFREGOFFSETMASK, 0x88001)
        set_rf_reg(t, RF_PATH_B, 0x65, RFREGOFFSETMASK, 0x931D1)
        set_rf_reg(t, RF_PATH_B, 0xEF, RFREGOFFSETMASK, 0x00000)
    set_bb(t, 0x978, 0x80000000, 0x1)
    set_bb(t, 0x97C, 0x80000000, 0x0)
    t.write32(0x90C, 0x00008000)
    # USB interface (not PCIE) -> 0x0046a890.
    t.write32(0x984, 0x0046A890)
    # rfe_type 0 -> the else arm (cb4/eb4 = 0x02000077).
    t.write32(0xCB0, 0x77777717)
    t.write32(0xCB4, 0x02000077)
    t.write32(0xEB0, 0x77777717)
    t.write32(0xEB4, 0x02000077)

    set_bb(t, _PAGE, 0x80000000, 0x1)                      # Page C1
    if tx0_fin:
        t.write32(0xC80, 0x38008C10)                       # TX_Tone_idx[9:0]
        t.write32(0xC84, 0x18008C10)                       # RX_Tone_idx[9:0]
        t.write32(0xC88, 0x82140119)
    if tx1_fin:
        t.write32(0xE80, 0x38008C10)
        t.write32(0xE84, 0x18008C10)
        t.write32(0xE88, 0x82140119)

    # --- RX IQK measurement loop ---
    rx_temp = [[0, 0, 0, 0] for _ in range(_CAL_NUM)]
    rx0_avg = rx1_avg = cal0_retry = cal1_retry = 0
    # iqk0_ready / iqk1_ready are intentionally NOT reset here: in the vendor they are
    # function-scoped and carry their final TX-loop value into the RX wait, where a path
    # whose TX IQK never finished (tx*_fin False) is never re-polled.
    while True:
        set_bb(t, _PAGE, 0x80000000, 0x0)                  # Page C
        if tx0_fin:
            set_bb(t, 0x978, 0x03FF8000, tx_iqc[0] & 0x07FF)
            set_bb(t, 0x978, 0x000007FF, tx_iqc[1] & 0x07FF)
            set_bb(t, _PAGE, 0x80000000, 0x1)              # Page C1
            t.write32(0xC8C, 0x28160CC0)                   # rfe_type 0 (1 -> 0x28161500)
            t.write32(0xCB8, 0x00300000)
            t.write32(0xCB8, 0x00100000)
            _ms(5)
            t.write32(0xC8C, 0x3C000000)
            t.write32(0xCB8, 0x00000000)
        if tx1_fin:
            set_bb(t, _PAGE, 0x80000000, 0x0)              # Page C
            set_bb(t, 0x978, 0x03FF8000, tx_iqc[2] & 0x07FF)
            set_bb(t, 0x978, 0x000007FF, tx_iqc[3] & 0x07FF)
            set_bb(t, _PAGE, 0x80000000, 0x1)              # Page C1
            t.write32(0xE8C, 0x28160CA0)                   # rfe_type 0 (1 -> 0x28161500)
            t.write32(0xEB8, 0x00300000)
            t.write32(0xEB8, 0x00100000)
            _ms(5)
            t.write32(0xE8C, 0x3C000000)
            t.write32(0xEB8, 0x00000000)
        delay_count = 0
        while True:
            if not rx0_fin and tx0_fin:
                iqk0_ready = bool(t.read32(0xD00) & (1 << 10))
            if not rx1_fin and tx1_fin:
                iqk1_ready = bool(t.read32(0xD40) & (1 << 10))
            if (iqk0_ready and iqk1_ready) or delay_count > 20:
                break
            _ms(1)
            delay_count += 1
        if delay_count < 20:
            rx0_fail = bool(t.read32(0xD00) & (1 << 11))
            rx1_fail = bool(t.read32(0xD40) & (1 << 11))
            if not (rx0_fail or rx0_fin) and tx0_fin:
                t.write32(0xCB8, 0x06000000)
                rx_temp[rx0_avg][0] = _coeff(t.read32(0xD00))
                t.write32(0xCB8, 0x08000000)
                rx_temp[rx0_avg][1] = _coeff(t.read32(0xD00))
                rx0_avg += 1
            else:
                cal0_retry += 1
                if cal0_retry == 10:
                    break
            if not (rx1_fail or rx1_fin) and tx1_fin:
                t.write32(0xEB8, 0x06000000)
                rx_temp[rx1_avg][2] = _coeff(t.read32(0xD40))
                t.write32(0xEB8, 0x08000000)
                rx_temp[rx1_avg][3] = _coeff(t.read32(0xD40))
                rx1_avg += 1
            else:
                cal1_retry += 1
                if cal1_retry == 10:
                    break
        else:
            cal0_retry += 1
            cal1_retry += 1
            if cal0_retry == 10:
                break
        if rx0_avg >= 2:
            ok, x, y = _vote(rx_temp, rx0_avg, 0, 1)
            if ok:
                rx_iqc[0], rx_iqc[1], rx0_fin = x, y, True
        if rx1_avg >= 2:
            ok, x, y = _vote(rx_temp, rx1_avg, 2, 3)
            if ok:
                rx_iqc[2], rx_iqc[3], rx1_fin = x, y, True
        if (rx0_fin or not tx0_fin) and (rx1_fin or not tx1_fin):
            break
        if ((cal0_retry + rx0_avg) >= 10 or (cal1_retry + rx1_avg) >= 10
                or rx0_avg == 3 or rx1_avg == 3):
            break

    return (tx0_fin, tx_iqc[0], tx_iqc[1], tx1_fin, tx_iqc[2], tx_iqc[3],
            rx0_fin, rx_iqc[0], rx_iqc[1], rx1_fin, rx_iqc[2], rx_iqc[3])


def _fill_results(t, res: tuple) -> None:
    # [SRC] _iqk_tx_8812a:1096 — fill final IQC, default (0x200,0x0) where cal failed.
    (tx0, tx0x, tx0y, tx1, tx1x, tx1y,
     rx0, rx0x, rx0y, rx1, rx1x, rx1y) = res
    _tx_fill_iqc(t, RF_PATH_A, tx0x, tx0y) if tx0 else _tx_fill_iqc(t, RF_PATH_A, 0x200, 0x0)
    _rx_fill_iqc(t, RF_PATH_A, rx0x, rx0y) if rx0 else _rx_fill_iqc(t, RF_PATH_A, 0x200, 0x0)
    _tx_fill_iqc(t, RF_PATH_B, tx1x, tx1y) if tx1 else _tx_fill_iqc(t, RF_PATH_B, 0x200, 0x0)
    _rx_fill_iqc(t, RF_PATH_B, rx1x, rx1y) if rx1 else _rx_fill_iqc(t, RF_PATH_B, 0x200, 0x0)


# --- public entry -----------------------------------------------------------

def iq_calibrate(t, *, is_2g: bool = True, recovery: bool = False) -> None:
    """[SRC] _phy_iq_calibrate_8812a / phy_iq_calibrate_8812a (halrf_8812a_ce.c:1127,1299).

    Full IQK: backup -> configure MAC -> TX/RX IQK measure+vote (both paths) ->
    fill coefficients -> restore. Invoke once after ``chan.set_chnl_bw`` (channel-set
    is a precondition: the IQK tone is measured at the tuned channel) and before RX.

    ``recovery`` mirrors ``phy_iq_calibrate_8812a(Adapter, bReCovery)`` for signature
    parity. The CE vendor source ignores the flag for 8812au (no FW IQK-offload), always
    running the full ``_phy_iq_calibrate_8812a`` re-measurement — so do we.
    """
    macbb = _backup_mac_bb(t)
    set_bb(t, _PAGE, 0x80000000, 0x1)                      # Page C1
    reg_cb8 = t.read32(0xCB8)
    reg_eb8 = t.read32(0xEB8)
    set_bb(t, _PAGE, 0x80000000, 0x0)                      # Page C
    afe = _backup_afe(t)
    rfa, rfb = _backup_rf(t)

    _configure_mac(t)
    res = _iqk_measure(t, is_2g)
    _fill_results(t, res)

    _restore_rf(t, RF_PATH_A, rfa)
    _restore_rf(t, RF_PATH_B, rfb)
    _restore_afe(t, afe)
    set_bb(t, _PAGE, 0x80000000, 0x1)                      # Page C1
    t.write32(0xCB8, reg_cb8)
    t.write32(0xEB8, reg_eb8)
    set_bb(t, _PAGE, 0x80000000, 0x0)                      # Page C
    _restore_mac_bb(t, macbb)
