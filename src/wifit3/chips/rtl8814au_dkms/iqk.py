"""RTL8814A IQK (IQ / LO calibration, M3d halrf) — 1:1 port of the vendor phydm.

``do_iqk_8814a`` [SRC halrf_iqk_8814a.c:33] is the thermal-delta IQK re-cal that tails the
TX-power-tracking callback: on a >= threshold thermal swing vs the last IQK it re-runs the
whole calibrate. The orchestrator ``phy_iq_calibrate_8814a`` [SRC halrf_iqk_8814a.c:483] does
backup(MAC/BB) -> AFE(iqk) -> backup(RF) -> configure(MAC) -> per-path LOK + TX/RX IQK one-shots
-> reset-NCTL -> AFE(normal) -> restore(MAC/BB) -> restore(RF).

Every result is COMPUTED from the NCTL status/result registers the chip feeds back, not
replayed from a fixed transcript: the LOK poll (``R_0x1b00`` bit0), its DAC fill from
``R_0x1bfc``, the IQK poll + fail bit (``R_0x1b08`` bit26), the bounded ``while (fail)`` retry,
and the TX/RX IQC matrix read-back (``0x1b38`` / ``0x1b3c``) -> apply. So the iteration count,
retry count, and applied values track the silicon (in replay, the recorded read-backs), which
is why the offline pcap gate reproduces the block byte-for-byte across both bands.

Register access: BB regs are direct (``t.read32``/``write32``/``write8`` = odm_read/write_Nbyte,
``_bb32`` = odm_set_bb_reg masked RMW); RF regs ride the per-path SPI (``_rf_read`` /
``set_rf_masked`` in rf.py). ``band_type`` / ``band_width`` are the vendor's runtime
``*dm->band_type`` (lagging ``current_band_type``, carried in WatchdogState) and ``*dm->band_width``
(20 MHz only here). CE build: ``support_ic_type == ODM_RTL8814A``, ``mp_mode`` off, no link.
"""
from __future__ import annotations

import time

from .bb import _set_reg_masked as _bb32
from .rf import _rf_read, set_rf_masked
from . import constants as C

# Register-count constants [SRC halrf_iqk_8814a.h:21-23].
MAC_REG_NUM_8814 = 2
BB_REG_NUM_8814 = 14
RF_REG_NUM_8814 = 1
# IQK timing / index constants [SRC halrf_iqk.h:30-42]. Delays are honoured on hardware; the
# offline gate stubs time.sleep, so the poll loops there cost nothing.
LOK_delay = 1
WBIQK_delay = 10
TX_IQK = 0
RX_IQK = 1
NUM = 4
# odm_band_type (CE) [SRC phydm_pre_define.h:763]; *dm->band_width 0=20/1=40/2=80M.
ODM_BAND_2_4G = 0
ODM_BAND_5G = 1

# RF register symbols the IQK touches (vendor RF_0xNN names) [SRC halrf_iqk_8814a.c].
RF_0x8 = 0x08
RF_0x56 = 0x56
RF_0x58 = 0x58
RF_0x8f = 0x8f
RF_0xdf = 0xdf
RF_0xef = 0xef

# Backup register lists (locals in _phy_iq_calibrate_8814a) [SRC halrf_iqk_8814a.c:196-199].
_BACKUP_MAC_REG = (0x520, 0x550)
_BACKUP_BB_REG = (0xa14, 0x808, 0x838, 0x90c, 0x810, 0xcb0, 0xeb0, 0x18b4, 0x1ab4, 0x1abc,
                  0x9a4, 0x764, 0xcbc, 0x910)
_BACKUP_RF_REG = (0x0,)
# Per-path IQK apply regs [SRC halrf_iqk_8814a.c:290].
_IQK_APPLY = (0xc94, 0xe94, 0x1894, 0x1a94)
_PATHS = ("a", "b", "c", "d")


def _get_bb_field(t, addr: int, mask: int) -> int:
    """odm_get_bb_reg — read ``addr`` and return the ``mask`` field shifted to bit 0."""
    shift = (mask & -mask).bit_length() - 1
    return (t.read32(addr) & mask) >> shift


def _delay_ms(ms: int) -> None:
    """ODM_delay_ms — real settle on hardware; the offline gate stubs time.sleep to a no-op."""
    time.sleep(ms * 1e-3)


# ---- backup / restore -------------------------------------------------------

def _iqk_backup_mac_bb_8814a(t, mac_backup: list, bb_backup: list) -> None:
    """[SRC] _iqk_backup_mac_bb_8814a, halrf_iqk_8814a.c:75 — save the MAC + BB defaults."""
    for i in range(MAC_REG_NUM_8814):
        mac_backup[i] = t.read32(_BACKUP_MAC_REG[i])
    for i in range(BB_REG_NUM_8814):
        bb_backup[i] = t.read32(_BACKUP_BB_REG[i])


def _iqk_backup_rf_8814a(t, rf_backup: list) -> None:
    """[SRC] _iqk_backup_rf_8814a, halrf_iqk_8814a.c:89 — save RF reg 0x0 on all four paths."""
    for i in range(RF_REG_NUM_8814):
        for pi, path in enumerate(_PATHS):
            rf_backup[i][pi] = _rf_read(t, path, _BACKUP_RF_REG[i])


def _iqk_restore_mac_bb_8814a(t, mac_backup: list, bb_backup: list) -> None:
    """[SRC] _iqk_restore_mac_bb_8814a, halrf_iqk_8814a.c:138 — reload the MAC + BB defaults."""
    for i in range(MAC_REG_NUM_8814):
        t.write32(_BACKUP_MAC_REG[i], mac_backup[i])
    for i in range(BB_REG_NUM_8814):
        t.write32(_BACKUP_BB_REG[i], bb_backup[i])


def _iqk_restore_rf_8814a(t, rf_backup: list) -> None:
    """[SRC] _iqk_restore_rf_8814a, halrf_iqk_8814a.c:151 — fixed RF_0xef/0x8f then reload 0x0."""
    for path in _PATHS:
        set_rf_masked(t, path, RF_0xef, C.RFREG_MASK, 0x0)
    for path in _PATHS:
        set_rf_masked(t, path, RF_0x8f, C.RFREG_MASK, 0x88001)
    for i in range(RF_REG_NUM_8814):
        for pi, path in enumerate(_PATHS):
            set_rf_masked(t, path, _BACKUP_RF_REG[i], C.RFREG_MASK, rf_backup[i][pi])


# ---- AFE / MAC / NCTL setup -------------------------------------------------

def _iqk_afe_setting_8814a(t, do_iqk: bool) -> None:
    """[SRC] _iqk_afe_setting_8814a, halrf_iqk_8814a.c:104 — RX_WAIT_CCA AFE mode.

    ``do_iqk`` (before) vs Normal (after) differ only in the 0xc60/0xe60/0x1860/0x1a60 word.
    """
    val = 0x0e808003 if do_iqk else 0x07808003
    t.write32(0xc60, val)
    t.write32(0xe60, val)
    t.write32(0x1860, val)
    t.write32(0x1a60, val)
    _bb32(t, 0x90c, 1 << 13, 0x1)
    _bb32(t, 0x764, (1 << 10) | (1 << 9), 0x3)
    _bb32(t, 0x764, (1 << 10) | (1 << 9), 0x0)
    _bb32(t, 0x804, 1 << 2, 0x1)
    _bb32(t, 0x804, 1 << 2, 0x0)


def _iqk_configure_mac_8814a(t) -> None:
    """[SRC] _iqk_configure_mac_8814a, halrf_iqk_8814a.c:201 — MAC/BB register setup for IQK.

    0x522 and 0x808 are 1-byte writes (byte width matters on the wire); the rest are 4-byte /
    masked BB regs.
    """
    t.write8(0x522, 0x3f)
    _bb32(t, 0x550, (1 << 11) | (1 << 3), 0x0)
    t.write8(0x808, 0x00)                      # RX ante off
    _bb32(t, 0x838, 0xf, 0xe)                  # CCA off
    _bb32(t, 0xa14, (1 << 9) | (1 << 8), 0x3)  # CCK RX path off
    t.write32(0xcb0, 0x77777777)
    t.write32(0xeb0, 0x77777777)
    t.write32(0x18b4, 0x77777777)
    t.write32(0x1ab4, 0x77777777)
    _bb32(t, 0x1abc, 0x0ff00000, 0x77)
    _bb32(t, 0x910, (1 << 23) | (1 << 22), 0x0)
    _bb32(t, 0xcbc, 0xf, 0x0)


def _iqk_reset_nctl_8814a(t) -> None:
    """[SRC] _iqk_reset_nctl_8814a, halrf_iqk_8814a.c:192 — reset the NCTL (3-wire -> BB)."""
    t.write32(0x1b00, 0xf8000000)
    t.write32(0x1b80, 0x00000006)
    t.write32(0x1b00, 0xf8000000)
    t.write32(0x1b80, 0x00000002)


# ---- the core: LOK + IQK one-shots -----------------------------------------

def _lok_one_shot(t, st) -> None:
    """[SRC] _lok_one_shot, halrf_iqk_8814a.c:219 — per-path LO calibration.

    Trigger to 0x1b00, poll ``R_0x1b00`` bit0 (done == 0, bound 10x 1 ms; timeout -> reset NCTL).
    On ready the two 5-bit DAC codes are read from ``R_0x1bfc`` (fields 0x003e0000 / 0x0000003e),
    spread by the vendor's bit-replicate loop, and applied to RF_0x8; on fail RF_0x8 = 0x08400.
    """
    for pi, path in enumerate(_PATHS):
        _bb32(t, 0x9a4, (1 << 21) | (1 << 20), pi)                # ADC clock source
        t.write32(0x1b00, 0xf8000001 | (1 << (4 + pi)))          # LOK: CMD ID 0
        _delay_ms(LOK_delay)
        delay_count = 0
        lok_notready = True
        while lok_notready:
            lok_notready = bool(_get_bb_field(t, 0x1b00, 1 << 0))
            _delay_ms(1)
            delay_count += 1
            if delay_count >= 10:
                _iqk_reset_nctl_8814a(t)
                break

        if not lok_notready:
            t.write32(0x1b00, 0xf8000000 | (pi << 1))
            t.write32(0x1bd4, 0x003f0001)
            lok_temp2 = (_get_bb_field(t, 0x1bfc, 0x003e0000) + 0x10) & 0x1f
            lok_temp1 = (_get_bb_field(t, 0x1bfc, 0x0000003e) + 0x10) & 0x1f
            for ii in range(1, 5):
                lok_temp1 = lok_temp1 + ((lok_temp1 & (1 << (4 - ii))) << (ii * 2))
                lok_temp2 = lok_temp2 + ((lok_temp2 & (1 << (4 - ii))) << (ii * 2))
            set_rf_masked(t, path, RF_0x8, 0x07c00, lok_temp1 >> 4)
            set_rf_masked(t, path, RF_0x8, 0xf8000, lok_temp2 >> 4)
        else:
            set_rf_masked(t, path, RF_0x8, C.RFREG_MASK, 0x08400)
        st.iqk_lok_fail[pi] = lok_notready


def _iqk_one_shot(t, st) -> None:
    """[SRC] _iqk_one_shot, halrf_iqk_8814a.c:283 — per-path wideband TX-IQK then RX-IQK.

    Outer ``idx`` 0=TXK / 1=RXK; inner per path A..D with a bounded ``while (fail)`` retry
    (``cal_retry > 3`` breaks). IQK_CMD is bandwidth-derived (CMD id 3/4/5 TXK, 9/8/7 RXK for
    20/40/80 M). Poll ``R_0x1b00`` bit0 for not-ready then ``R_0x1b08`` bit26 for fail (bound
    20x 1 ms; timeout -> reset NCTL). On success read the IQC matrix (0x1b38 TXK / 0x1b3c RXK);
    the RXK pass then either re-writes the TXK IQC or clears the per-path apply bit, from the
    TXK pass/fail captured in this same call. The 2.4 GHz RXK tone block (RF_0xdf/0x56 + BB gain)
    runs only when ``band_type == ODM_BAND_2_4G``.
    """
    for idx in (TX_IQK, RX_IQK):
        for pi, path in enumerate(_PATHS):
            cal_retry = 0
            fail = True
            while fail:
                _bb32(t, 0x9a4, (1 << 21) | (1 << 20), pi)
                if idx == TX_IQK:
                    iqk_cmd = 0xf8000001 | (st.band_width + 3) << 8 | (1 << (4 + pi))
                else:  # RX_IQK
                    if st.current_band_type == ODM_BAND_2_4G:
                        set_rf_masked(t, path, RF_0xdf, 1 << 11, 0x1)
                        set_rf_masked(t, path, RF_0x56, 0xfffff, 0x51ce1)
                        if pi in (0, 1):
                            t.write32(0xeb0, 0x54775477)
                        elif pi == 2:
                            t.write32(0x18b4, 0x54775477)
                        elif pi == 3:
                            t.write32(0x1abc, 0x75400000)
                            t.write32(0x1ab4, 0x77777777)
                    iqk_cmd = 0xf8000001 | (9 - st.band_width) << 8 | (1 << (4 + pi))

                t.write32(0x1b00, iqk_cmd)
                _delay_ms(WBIQK_delay)
                delay_count = 0
                notready = True
                while notready:
                    notready = bool(_get_bb_field(t, 0x1b00, 1 << 0))
                    if not notready:
                        fail = bool(_get_bb_field(t, 0x1b08, 1 << 26))
                        break
                    _delay_ms(1)
                    delay_count += 1
                    if delay_count >= 20:
                        _iqk_reset_nctl_8814a(t)
                        break
                if fail:
                    cal_retry += 1
                if cal_retry > 3:
                    break

            t.write32(0x1b00, 0xf8000000 | (pi << 1))
            if not fail:
                if idx == TX_IQK:
                    st.iqc_matrix[idx][pi] = t.read32(0x1b38)
                else:  # RX_IQK
                    t.write32(0x1b3c, 0x20000000)
                    st.iqc_matrix[idx][pi] = t.read32(0x1b3c)

            if idx == RX_IQK:
                if not st.iqk_fail[TX_IQK][pi]:              # TXIQK succeeded in RXIQK
                    t.write32(0x1b38, st.iqc_matrix[TX_IQK][pi])
                else:
                    _bb32(t, _IQK_APPLY[pi], 1 << 0, 0x0)
                if fail:
                    _bb32(t, _IQK_APPLY[pi], (1 << 11) | (1 << 10), 0x0)
                if st.current_band_type == ODM_BAND_2_4G:
                    set_rf_masked(t, path, RF_0xdf, 1 << 11, 0x0)

            st.iqk_fail[idx][pi] = fail


def _iqk_tx_8814a(t, st, chnl_idx: int) -> None:
    """[SRC] _iqk_tx_8814a, halrf_iqk_8814a.c:407 — RF/BB TX-IQK setup then LOK + IQK.

    ``chnl_idx`` is computed by the caller but unused by the 8814A TX path (kept for parity,
    spec 2c) — only ``band_type`` selects the 0x1b00 seed. Applies RF_0x58 bit19 and the
    0xc94/.../0x1a94 tone defaults on all paths, seeds NCTL, then runs the one-shots.
    """
    for path in _PATHS:
        set_rf_masked(t, path, RF_0x58, 1 << 19, 0x1)
    for reg in _IQK_APPLY:
        _bb32(t, reg, (1 << 11) | (1 << 10) | (1 << 0), 0x401)

    if st.current_band_type == ODM_BAND_5G:
        t.write32(0x1b00, 0xf8000ff1)
    else:
        t.write32(0x1b00, 0xf8000ef1)
    _delay_ms(1)

    t.write32(0x810, 0x20101063)
    t.write32(0x90c, 0x0B00C000)
    _lok_one_shot(t, st)
    _iqk_one_shot(t, st)


# ---- orchestrator -----------------------------------------------------------

def odm_get_right_chnl_place_for_iqk(chnl: int) -> int:
    """[SRC] odm_get_right_chnl_place_for_iqk, halphyrf_ce.c:1082 — channel -> IQK table index.

    2.4 GHz returns the channel; 5 GHz returns its place in channel_all minus 13. Unused by the
    8814A TX path (see _iqk_tx_8814a) but kept for parity."""
    channel_all = (
        1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14,
        36, 38, 40, 42, 44, 46, 48, 50, 52, 54, 56, 58, 60, 62, 64,
        100, 102, 104, 106, 108, 110, 112, 114, 116, 118, 120, 122,
        124, 126, 128, 130, 132, 134, 136, 138, 140,
        149, 151, 153, 155, 157, 159, 161, 163, 165)
    if chnl > 14:
        for place in range(14, len(channel_all)):
            if channel_all[place] == chnl:
                return place - 13
    return 0


def phy_reset_iqk_result_8814a(t) -> None:
    """[SRC] phy_reset_iqk_result_8814a, halrf_iqk_8814a.c:176 — clear the NCTL IQC banks per
    path then the four apply regs. Not on the CE flow; kept for parity/debug."""
    for pi in range(NUM):
        t.write32(0x1b00, 0xf8000000 | (pi << 1))
        t.write32(0x1b38, 0x20000000)
    t.write32(0xc10, 0x100)
    t.write32(0xe10, 0x100)
    t.write32(0x1810, 0x100)
    t.write32(0x1a10, 0x100)


def phy_iq_calibrate_8814a_init(st) -> None:
    """[SRC] phy_iq_calibrate_8814a_init, halrf_iqk_8814a.c:439 — one-time (per driver lifetime)
    reset of the LOK/IQK fail flags + IQC matrix. ``iqk_firstrun`` mirrors the static firstrun."""
    if st.iqk_firstrun:
        st.iqk_firstrun = False
        for jj in range(2):
            for ii in range(NUM):
                st.iqk_lok_fail[ii] = True
                st.iqk_fail[jj][ii] = True
                st.iqc_matrix[jj][ii] = 0x20000000


def _phy_iq_calibrate_8814a(t, st, channel: int) -> None:
    """[SRC] _phy_iq_calibrate_8814a, halrf_iqk_8814a.c:458 — the backup -> calibrate -> restore
    backbone. MAC/BB/RF backups are per-call locals; the LOK/IQK results land in ``st``."""
    mac_backup = [0] * MAC_REG_NUM_8814
    bb_backup = [0] * BB_REG_NUM_8814
    rf_backup = [[0, 0, 0, 0] for _ in range(RF_REG_NUM_8814)]
    chnl_idx = odm_get_right_chnl_place_for_iqk(channel)

    st.iqk_times += 1

    _iqk_backup_mac_bb_8814a(t, mac_backup, bb_backup)
    _iqk_afe_setting_8814a(t, True)
    _iqk_backup_rf_8814a(t, rf_backup)
    _iqk_configure_mac_8814a(t)
    _iqk_tx_8814a(t, st, chnl_idx)
    _iqk_reset_nctl_8814a(t)                    # for 3-wire to BB use
    _iqk_afe_setting_8814a(t, False)
    _iqk_restore_mac_bb_8814a(t, mac_backup, bb_backup)
    _iqk_restore_rf_8814a(t, rf_backup)


def phy_iq_calibrate_8814a(t, st, channel: int, is_recovery: bool) -> None:
    """[SRC] phy_iq_calibrate_8814a, halrf_iqk_8814a.c:483 — IQK version 0xf. The ODM_AP
    fail-report call is dropped (CE)."""
    phy_iq_calibrate_8814a_init(st)
    _phy_iq_calibrate_8814a(t, st, channel)


def halrf_iqk_trigger(t, st, channel: int, is_recovery: bool) -> None:
    """[SRC] halrf_iqk_trigger, halrf.c:1694 — gate + re-entrancy bracket around the 8814A IQK.

    mp_mode is off and ``rf_supportability & HAL_RF_IQK`` is set in this build (both constant
    gates), so only ``rfk_forbidden`` and the ``is_iqk_in_progress`` re-entrancy guard remain.
    The BT-coex handshake (halrf_rfk_handshake) is a no-op in this no-BT monitor build.
    """
    if st.rfk_forbidden:
        return
    if not st.is_iqk_in_progress:
        st.is_iqk_in_progress = True
        phy_iq_calibrate_8814a(t, st, channel, is_recovery)   # switch: ODM_RTL8814A case
        st.is_iqk_in_progress = False


def do_iqk_8814a(t, st, channel: int) -> None:
    """[SRC] do_iqk_8814a (CE), halrf_iqk_8814a.c:33 — the thermal-track IQK entry.

    ``odm_reset_iqk_result`` is an empty no-op in CE. ``thermal_value_iqk`` is re-affirmed to the
    current (just-stored) thermal value, then the trigger runs (is_recovery = false).
    """
    st.thermal_value_iqk = st.thermal_value
    halrf_iqk_trigger(t, st, channel, False)
