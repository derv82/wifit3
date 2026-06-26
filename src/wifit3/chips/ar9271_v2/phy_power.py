"""TX-power programming for the AR9271 (4k EEPROM), ported from eeprom_4k.c / eeprom.c.

``apply_txpower`` mirrors ath9k_hw_apply_txpower -> ath9k_hw_4k_set_txpower: it computes the
per-rate target powers (clamped by the CTL regulatory edges) and the PDADC calibration table
from the 4k EEPROM, then programs AR_PHY_TPCRG1/TPCRG5, the 32 PDADC words, and the per-rate
power registers. The AR9271 is 1T1R / 2.4 GHz / 20 MHz here, so the HT40 and multi-chain
branches are ported behind their guards but never taken.

For the cold-boot reset the regulatory inputs are the stack defaults: channel max_power = 20
(common-init.c) so powerLimit = min(20*2, MAX_COMBINED_POWER) = 40; reg->power_limit starts at
MAX_COMBINED_POWER; ctl = NO_CTL on the first (pre-regd) reset. [SRC] hw.c:2944 / common-init.c.
"""
from __future__ import annotations

from . import reg as R
from .chan import Channel
from .eeprom_4k import Map4k
from .hw import AthHw

NO_CTL = 0xFF                              # [SRC] reg.h: regulatory "no CTL" sentinel
_DEFAULT_CHAN_MAX_POWER = 20              # ieee80211_channel.max_power [SRC] common-init.c:26
_MAX_COMBINED_POWER = 254                 # [SRC] hw.h:176

# enum ar5416_rates [SRC] eeprom.h:264-276
(rate6mb, rate9mb, rate12mb, rate18mb, rate24mb, rate36mb, rate48mb, rate54mb,
 rate1l, rate2l, rate2s, rate5_5l, rate5_5s, rate11l, rate11s, rateXr,
 rateHt20_0, rateHt20_1, rateHt20_2, rateHt20_3, rateHt20_4, rateHt20_5, rateHt20_6, rateHt20_7,
 rateHt40_0, rateHt40_1, rateHt40_2, rateHt40_3, rateHt40_4, rateHt40_5, rateHt40_6, rateHt40_7,
 rateDupCck, rateDupOfdm, rateExtCck, rateExtOfdm) = range(36)
Ar5416RateSize = 36


def _freq2fbin(freq: int) -> int:           # FREQ2FBIN(x, is2g) [SRC] eeprom.h:109
    return (freq - 2300) & 0xff


def _fbin2freq(fbin: int) -> int:           # ath9k_hw_fbin2freq(is2g) [SRC] eeprom.h:715
    if fbin == R.AR5416_BCHAN_UNUSED:
        return fbin
    return 2300 + fbin


def _interpolate(target: int, sl: int, sr: int, tl: int, tr: int) -> int:
    """ath9k_hw_interpolate [SRC] eeprom.c:35 (C integer division)."""
    if sr == sl:
        return tl
    return ((target - sl) * tr + (sr - target) * tl) // (sr - sl)


def _lower_upper_index(target: int, plist: list[int], n: int) -> tuple[bool, int, int]:
    """ath9k_hw_get_lower_upper_index [SRC] eeprom.c:51."""
    if target <= plist[0]:
        return True, 0, 0
    if target >= plist[n - 1]:
        return True, n - 1, n - 1
    for i in range(n - 1):
        if plist[i] == target:
            return True, i, i
        if target < plist[i + 1]:
            return False, i, i + 1
    return False, 0, 0


def _fill_vpd_table(pwr_min: int, pwr_max: int, p_pwr: list[int], p_vpd: list[int],
                    num_i: int) -> list[int]:
    """ath9k_hw_fill_vpd_table [SRC] eeprom.c:240."""
    out = [0] * ((pwr_max - pwr_min) // 2 + 1)
    curr = pwr_min
    for i in range(len(out)):
        _, idxL, idxR = _lower_upper_index(curr, p_pwr, num_i)
        if idxR < 1:
            idxR = 1
        if idxL == num_i - 1:
            idxL = num_i - 2
        if p_pwr[idxL] == p_pwr[idxR]:
            k = p_vpd[idxL]
        else:
            k = ((curr - p_pwr[idxL]) * p_vpd[idxR]
                 + (p_pwr[idxR] - curr) * p_vpd[idxL]) // (p_pwr[idxR] - p_pwr[idxL])
        out[i] = k & 0xff
        curr += 2
    return out


def _legacy_target_powers(pow_info, num_channels: int, num_rates: int, freq: int) -> list[int]:
    """ath9k_hw_get_legacy_target_powers / ath9k_hw_get_target_powers [SRC] eeprom.c:266,322
    (HT shares the same pier-matching shape). ``freq`` is centers.ctl_center (20 MHz)."""
    match_index = -1
    low_index = -1
    i = 0
    if freq <= _fbin2freq(pow_info[0].bChannel):
        match_index = 0
    else:
        for i in range(num_channels):
            if pow_info[i].bChannel == R.AR5416_BCHAN_UNUSED:
                break
            f = _fbin2freq(pow_info[i].bChannel)
            if freq == f:
                match_index = i
                break
            if freq < f and i > 0 and freq > _fbin2freq(pow_info[i - 1].bChannel):
                low_index = i - 1
                break
        else:
            i = num_channels
        if match_index == -1 and low_index == -1:
            match_index = i - 1
    if match_index != -1:
        return list(pow_info[match_index].tPow2x[:num_rates])
    clo = _fbin2freq(pow_info[low_index].bChannel)
    chi = _fbin2freq(pow_info[low_index + 1].bChannel)
    return [_interpolate(freq, clo, chi, pow_info[low_index].tPow2x[k],
                         pow_info[low_index + 1].tPow2x[k]) & 0xff for k in range(num_rates)]


def _max_edge_power(freq: int, edges: list[tuple[int, int]], n: int) -> int:
    """ath9k_hw_get_max_edge_power [SRC] eeprom.c:378."""
    twice_max = R.MAX_RATE_POWER
    for i in range(n):
        if edges[i][0] == R.AR5416_BCHAN_UNUSED:
            break
        f = _fbin2freq(edges[i][0])
        if freq == f:
            twice_max = R.CTL_EDGE_TPOWER(edges[i][1])
            break
        if i > 0 and freq < f:
            if _fbin2freq(edges[i - 1][0]) < freq and R.CTL_EDGE_FLAGS(edges[i - 1][1]):
                twice_max = R.CTL_EDGE_TPOWER(edges[i - 1][1])
            break
    return twice_max


def _gain_boundaries_pdadcs(eep: Map4k, freq: int, t_pd_overlap: int,
                            num_xpd_gains: int) -> tuple[list[int], list[int]]:
    """ath9k_hw_get_gain_boundaries_pdadcs [SRC] eeprom.c:452 — eeprom_4k path, single chain,
    20 MHz (centers.synth_center == channel freq). Returns (gainBoundaries[4], pdadc[128])."""
    intercepts = R.AR5416_PD_GAIN_ICEPTS
    piers = eep.calPierData2G()
    bchans = eep.calFreqPier2G
    num_piers = 0
    for num_piers in range(len(bchans)):
        if bchans[num_piers] == R.AR5416_BCHAN_UNUSED:
            break
    else:
        num_piers = len(bchans)

    synth_fbin = _freq2fbin(freq)
    match, idxL, idxR = _lower_upper_index(synth_fbin, bchans, num_piers)

    vpd_i = [[0] * R.AR5416_MAX_PWR_RANGE_IN_HALF_DB for _ in range(R.AR5416_NUM_PD_GAINS)]
    min_pwr = [0] * R.AR5416_NUM_PD_GAINS
    max_pwr = [0] * R.AR5416_NUM_PD_GAINS
    if match:
        for i in range(num_xpd_gains):
            pwr, vpd = piers[idxL].pwrPdg, piers[idxL].vpdPdg
            min_pwr[i] = pwr[i][0]
            max_pwr[i] = pwr[i][intercepts - 1]
            row = _fill_vpd_table(min_pwr[i], max_pwr[i], pwr[i], vpd[i], intercepts)
            vpd_i[i][:len(row)] = row
    else:
        for i in range(num_xpd_gains):
            pwrL, vpdL = piers[idxL].pwrPdg, piers[idxL].vpdPdg
            pwrR, vpdR = piers[idxR].pwrPdg, piers[idxR].vpdPdg
            min_pwr[i] = max(pwrL[i][0], pwrR[i][0])
            max_pwr[i] = min(pwrL[i][intercepts - 1], pwrR[i][intercepts - 1])
            vL = _fill_vpd_table(min_pwr[i], max_pwr[i], pwrL[i], vpdL[i], intercepts)
            vR = _fill_vpd_table(min_pwr[i], max_pwr[i], pwrR[i], vpdR[i], intercepts)
            for j in range((max_pwr[i] - min_pwr[i]) // 2 + 1):
                vpd_i[i][j] = _interpolate(synth_fbin, bchans[idxL], bchans[idxR],
                                           vL[j], vR[j]) & 0xff

    pdadc = [0] * R.AR5416_NUM_PDADC_VALUES
    boundaries = [0] * R.AR5416_PD_GAINS_IN_MASK
    k = 0
    i = 0
    for i in range(num_xpd_gains):
        if i == num_xpd_gains - 1:
            boundaries[i] = max_pwr[i] // 2
        else:
            boundaries[i] = (max_pwr[i] + min_pwr[i + 1]) // 4
        boundaries[i] = min(R.MAX_RATE_POWER, boundaries[i])

        if i == 0:                          # AR_SREV_9280_20_OR_LATER
            ss = 0 - (min_pwr[i] // 2)
        else:
            ss = (boundaries[i - 1] - (min_pwr[i] // 2)) - t_pd_overlap + 1
        vpd_step = vpd_i[i][1] - vpd_i[i][0]
        vpd_step = 1 if vpd_step < 1 else vpd_step
        while ss < 0 and k < R.AR5416_NUM_PDADC_VALUES - 1:
            tmp = vpd_i[i][0] + ss * vpd_step
            pdadc[k] = 0 if tmp < 0 else tmp
            k += 1
            ss += 1

        size_curr = (max_pwr[i] - min_pwr[i]) // 2 + 1
        tgt = boundaries[i] + t_pd_overlap - (min_pwr[i] // 2)
        max_index = tgt if tgt < size_curr else size_curr
        while ss < max_index and k < R.AR5416_NUM_PDADC_VALUES - 1:
            pdadc[k] = vpd_i[i][ss]
            k += 1
            ss += 1

        vpd_step = vpd_i[i][size_curr - 1] - vpd_i[i][size_curr - 2]
        vpd_step = 1 if vpd_step < 1 else vpd_step
        if tgt >= max_index:
            while ss <= tgt and k < R.AR5416_NUM_PDADC_VALUES - 1:
                tmp = vpd_i[i][size_curr - 1] + (ss - max_index + 1) * vpd_step
                pdadc[k] = 255 if tmp > 255 else tmp
                k += 1
                ss += 1

    pdgain_default = 58                     # eeprom_4k path
    while i < R.AR5416_PD_GAINS_IN_MASK:
        boundaries[i] = pdgain_default
        i += 1
    while k < R.AR5416_NUM_PDADC_VALUES:
        pdadc[k] = pdadc[k - 1]
        k += 1
    return boundaries, pdadc


def _set_power_cal_table(hw: AthHw, eep: Map4k, chan: Channel) -> None:
    """ath9k_hw_set_4k_power_cal_table [SRC] eeprom_4k.c:283 — TPCRG1 gain config (RMW batch),
    then per active TX chain the TPCRG5 boundaries + 32 PDADC words (write batch)."""
    xpd_mask = eep.xpdGain
    if eep.eeprom_rev >= R.AR5416_EEP_MINOR_VER_2:
        pd_gain_overlap = eep.pdGainOverlap
    else:
        pd_gain_overlap = R.MS(hw.read(R.AR_PHY_TPCRG5), R.AR_PHY_TPCRG5_PD_GAIN_OVERLAP)

    num_xpd_gain = 0
    xpd_gain_values = [0, 0]
    for i in range(1, R.AR5416_PD_GAINS_IN_MASK + 1):
        if (xpd_mask >> (R.AR5416_PD_GAINS_IN_MASK - i)) & 1:
            if num_xpd_gain >= R.AR5416_EEP4K_NUM_PD_GAINS:
                break
            xpd_gain_values[num_xpd_gain] = R.AR5416_PD_GAINS_IN_MASK - i
            num_xpd_gain += 1

    hw.enable_rmw_buffer()
    hw.rmw_field(R.AR_PHY_TPCRG1, R.AR_PHY_TPCRG1_NUM_PD_GAIN, (num_xpd_gain - 1) & 0x3)
    hw.rmw_field(R.AR_PHY_TPCRG1, R.AR_PHY_TPCRG1_PD_GAIN_1, xpd_gain_values[0])
    hw.rmw_field(R.AR_PHY_TPCRG1, R.AR_PHY_TPCRG1_PD_GAIN_2, xpd_gain_values[1])
    hw.rmw_field(R.AR_PHY_TPCRG1, R.AR_PHY_TPCRG1_PD_GAIN_3, 0)
    hw.rmw_buffer_flush()

    for i in range(R.AR5416_EEP4K_MAX_CHAINS):
        regChainOffset = i * 0x1000
        if not (eep.txMask & (1 << i)):
            continue
        boundaries, pdadc = _gain_boundaries_pdadcs(eep, chan.center_freq, pd_gain_overlap,
                                                    num_xpd_gain)
        hw.enable_write_buffer()
        hw.write(R.AR_PHY_TPCRG5 + regChainOffset,
                 R.SM(pd_gain_overlap, R.AR_PHY_TPCRG5_PD_GAIN_OVERLAP)
                 | R.SM(boundaries[0], R.AR_PHY_TPCRG5_PD_GAIN_BOUNDARY_1)
                 | R.SM(boundaries[1], R.AR_PHY_TPCRG5_PD_GAIN_BOUNDARY_2)
                 | R.SM(boundaries[2], R.AR_PHY_TPCRG5_PD_GAIN_BOUNDARY_3)
                 | R.SM(boundaries[3], R.AR_PHY_TPCRG5_PD_GAIN_BOUNDARY_4))
        regOffset = R.AR_PHY_BASE + (672 << 2) + regChainOffset
        for j in range(32):
            reg32 = int.from_bytes(bytes(pdadc[4 * j:4 * j + 4]), "little")
            hw.write(regOffset, reg32)
            regOffset += 4
        hw.write_flush()


def _set_power_per_rate_table(hw: AthHw, eep: Map4k, chan: Channel, rates: list[int],
                              cfg_ctl: int, antenna_reduction: int, power_limit: int) -> None:
    """ath9k_hw_set_4k_power_per_rate_table [SRC] eeprom_4k.c:386 — fill ``rates`` (no wire
    ops). 20 MHz: only the CTL_11B/11G/2GHT20 modes; HT40/ext modes are guarded out."""
    freq = chan.center_freq                 # centers.ctl_center (20 MHz)
    scaled_power = min(power_limit - antenna_reduction, R.MAX_RATE_POWER)
    ctl_modes = [R.CTL_11B, R.CTL_11G, R.CTL_2GHT20,
                 R.CTL_11B_EXT, R.CTL_11G_EXT, R.CTL_2GHT40]
    num_ctl_modes = len(ctl_modes) - R.SUB_NUM_CTL_MODES_AT_2G_40   # 20 MHz -> 3

    tp_cck = _legacy_target_powers(eep.calTargetPowerCck, 3, 4, freq)
    tp_ofdm = _legacy_target_powers(eep.calTargetPower2G, 3, 4, freq)
    tp_ht20 = _legacy_target_powers(eep.calTargetPower2GHT20, 3, 8, freq)

    ctl_index = eep.ctlIndex
    for mode in ctl_modes[:num_ctl_modes]:
        twice_max_edge = R.MAX_RATE_POWER
        for i in range(R.AR5416_EEP4K_NUM_CTLS):
            if ctl_index[i] == 0:
                break
            base = (cfg_ctl & ~R.CTL_MODE_M) | (mode & R.CTL_MODE_M)
            if base == ctl_index[i] or base == ((ctl_index[i] & R.CTL_MODE_M) | R.SD_NO_CTL):
                twice_min_edge = _max_edge_power(freq, eep.ctlEdges(i),
                                                 R.AR5416_EEP4K_NUM_BAND_EDGES)
                if (cfg_ctl & ~R.CTL_MODE_M) == R.SD_NO_CTL:
                    twice_max_edge = min(twice_max_edge, twice_min_edge)
                else:
                    twice_max_edge = twice_min_edge
                    break
        min_ctl_power = min(twice_max_edge, scaled_power)
        if mode == R.CTL_11B:
            tp_cck = [min(v, min_ctl_power) for v in tp_cck]
        elif mode == R.CTL_11G:
            tp_ofdm = [min(v, min_ctl_power) for v in tp_ofdm]
        elif mode == R.CTL_2GHT20:
            tp_ht20 = [min(v, min_ctl_power) for v in tp_ht20]

    for r in (rate6mb, rate9mb, rate12mb, rate18mb, rate24mb):
        rates[r] = tp_ofdm[0]
    rates[rate36mb] = tp_ofdm[1]
    rates[rate48mb] = tp_ofdm[2]
    rates[rate54mb] = tp_ofdm[3]
    rates[rateXr] = tp_ofdm[0]
    for i in range(8):
        rates[rateHt20_0 + i] = tp_ht20[i]
    rates[rate1l] = tp_cck[0]
    rates[rate2s] = rates[rate2l] = tp_cck[1]
    rates[rate5_5s] = rates[rate5_5l] = tp_cck[2]
    rates[rate11s] = rates[rate11l] = tp_cck[3]


def _set_txpower(hw: AthHw, chan: Channel, cfg_ctl: int, antenna_reduction: int,
                 power_limit: int) -> None:
    """ath9k_hw_4k_set_txpower [SRC] eeprom_4k.c:586 (test=false, tpc disabled)."""
    eep = Map4k(hw.eeprom)
    rates = [0] * Ar5416RateSize

    _set_power_per_rate_table(hw, eep, chan, rates, cfg_ctl, antenna_reduction, power_limit)
    _set_power_cal_table(hw, eep, chan)

    hw.max_power_level = 0                             # reg->max_power_level
    for i in range(Ar5416RateSize):
        if rates[i] > R.MAX_RATE_POWER:
            rates[i] = R.MAX_RATE_POWER
        if rates[i] > hw.max_power_level:
            hw.max_power_level = rates[i]
    for i in range(Ar5416RateSize):
        rates[i] -= R.AR5416_PWR_TABLE_OFFSET_DB * 2   # += 10

    pw = R.ATH9K_POW_SM
    hw.enable_write_buffer()
    hw.write(R.AR_PHY_POWER_TX_RATE1,
             pw(rates[rate18mb], 24) | pw(rates[rate12mb], 16)
             | pw(rates[rate9mb], 8) | pw(rates[rate6mb], 0))
    hw.write(R.AR_PHY_POWER_TX_RATE2,
             pw(rates[rate54mb], 24) | pw(rates[rate48mb], 16)
             | pw(rates[rate36mb], 8) | pw(rates[rate24mb], 0))
    hw.write(R.AR_PHY_POWER_TX_RATE3,
             pw(rates[rate2s], 24) | pw(rates[rate2l], 16)
             | pw(rates[rateXr], 8) | pw(rates[rate1l], 0))
    hw.write(R.AR_PHY_POWER_TX_RATE4,
             pw(rates[rate11s], 24) | pw(rates[rate11l], 16)
             | pw(rates[rate5_5s], 8) | pw(rates[rate5_5l], 0))
    hw.write(R.AR_PHY_POWER_TX_RATE5,
             pw(rates[rateHt20_3], 24) | pw(rates[rateHt20_2], 16)
             | pw(rates[rateHt20_1], 8) | pw(rates[rateHt20_0], 0))
    hw.write(R.AR_PHY_POWER_TX_RATE6,
             pw(rates[rateHt20_7], 24) | pw(rates[rateHt20_6], 16)
             | pw(rates[rateHt20_5], 8) | pw(rates[rateHt20_4], 0))
    hw.write(R.AR_PHY_POWER_TX_RATE_MAX, R.MAX_RATE_POWER)   # TPC disabled
    hw.write_flush()


def apply_txpower(hw: AthHw, chan: Channel) -> None:
    """ath9k_hw_apply_txpower [SRC] hw.c:2944 (test=false). The per-rate target powers are
    clamped to the current regulatory state: chan_pwr = min(channel->max_power*2,
    MAX_COMBINED_POWER), new_pwr = min(chan_pwr, reg->power_limit). On the cold reset that state
    is the stack default (max_power 20, power_limit MAX_COMBINED_POWER -> new_pwr 40); after the
    first update_txpow(0) it drops to 0 (every rate -> 0x0a)."""
    eep = Map4k(hw.eeprom)
    chan_pwr = min(hw.chan_max_power * 2, _MAX_COMBINED_POWER)
    new_pwr = min(chan_pwr, hw.reg_power_limit)
    _set_txpower(hw, chan, NO_CTL, eep.antennaGainCh0, new_pwr)


def update_txpow(hw: AthHw, chan: Channel, new_txpow: int) -> None:
    """ath9k_cmn_update_txpow -> ath9k_hw_set_txpowerlimit [SRC] common.c:74 / hw.c:2966 — set
    reg->power_limit and re-apply. channel->max_power is only touched in test mode (test=false
    here), so it stays the mac80211 value. The first start passes priv->txpowlimit=0 (per-rate
    targets -> 0x0a); a later CONF_CHANGE_POWER raises it back (-> 0x28)."""
    hw.reg_power_limit = min(new_txpow, _MAX_COMBINED_POWER)
    apply_txpower(hw, chan)
