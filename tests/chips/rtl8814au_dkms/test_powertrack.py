"""Hardware-free regression for the M3c thermal TX-power tracking (halrf MIX_MODE).

The full byte-for-byte check vs the cold-boot capture is
``scripts/rtl8814au_dkms/verify_pcap.py``; these pin the swing math so the pcap value is
provably COMPUTED (from the delta-swing tables + the EFUSE thermal base), not hardcoded:
the negative-delta chain, the per-path register writes, and the per-hop clear + band-switch
``default_ofdm_index`` reload.
"""
from wifit3.chips.rtl8814au_dkms import powertrack, watchdog
from wifit3.chips.rtl8814au_dkms import powertrack_tbl as T


class Rec:
    """Recording transport: serves canned reads, logs writes (last-write-wins per addr)."""

    def __init__(self, reads):
        self.reads = reads
        self.writes = []

    def read32(self, a):
        return self.reads.get(a, 0)

    def write32(self, a, v):
        self.writes.append((a, v))


def test_negative_delta_computes_bb_swing_idx_20_and_0x197():
    """eeprom_thermal=35, thermal=25 -> delta 10 -> 2G down-table 4 -> bb_swing_idx 20 -> 0x197.

    Derived end-to-end from the tables + EFUSE base; this is capture-1's tick #2 value.
    """
    st = watchdog.WatchdogState(eeprom_thermal=35)          # default_ofdm_index=24, thermal_value=35
    powertrack.odm_get_tracking_table(25, 10, 35, 4, powertrack.NO_LINK_TX_RATE, st)
    assert st.absolute_ofdm_swing_idx == [-4, -4, -4, -4]   # -1 * 2G-CCK down[10] (4 on every path)
    assert st.delta_power_index == [-4, -4, -4, -4]
    for p in range(4):
        st.power_index_offset[p] = st.delta_power_index[p] - st.delta_power_index_last[p]
    powertrack.get_mix_mode_tx_agc_bb_swing_offset(st, 0, 0xF)
    assert st.bb_swing_idx_ofdm[0] == 20                    # default_ofdm_index(24) + (-4)
    assert st.absolute_ofdm_swing_idx[0] == 0               # index fits the TXAGC field (no spill)
    assert T.TX_SCALING_TABLE_JAGUAR[20] == 0x197           # -2.0 dB


def test_check_ce_arms_then_corrects_all_paths_to_0x197():
    """Two-phase gate: tick 1 arms RF 0x42 (no BB-swing write); tick 2 reads the thermal meter
    and writes 0x197 (bits[31:21]) to every path's BB-swing register."""
    reads = {0x2908: 0x64C8}                                # RF 0x42[15:10] = 25
    for r in (0x0C94, 0x0E94, 0x1894, 0x1A94):
        reads[r] = 0x01000401                              # TXAGC field already 0
    for r in (0x0C1C, 0x0E1C, 0x181C, 0x1A1C):
        reads[r] = 0x40000053                              # bits[31:21] = 0x200 (0 dB base)
    st = watchdog.WatchdogState(eeprom_thermal=35)
    st.thermal_value_iqk = 25       # == the read thermal -> delta_iqk 0 -> the IQK re-cal does NOT
    #                                 fire, isolating the pwr correction here (the IQK trigger +
    #                                 one-shots are exercised in test_iqk.py).
    t = Rec(reads)

    powertrack.txpowertracking_check_ce(t, st, 4)          # tick 1: arm
    assert st.tm_trigger == 1
    assert all(a != 0x0C1C for a, _ in t.writes)           # no correction yet

    powertrack.txpowertracking_check_ce(t, st, 4)          # tick 2: callback + correction
    assert st.tm_trigger == 0
    w = dict(t.writes)
    for r in (0x0C1C, 0x0E1C, 0x181C, 0x1A1C):
        assert w[r] == 0x32E00053                          # bits[31:21] 0x200 -> 0x197
    for r in (0x0C94, 0x0E94, 0x1894, 0x1A94):
        assert w[r] == 0x01000401                          # TXAGC field unchanged (index 0)


def test_band_switch_reloads_default_ofdm_index():
    """A 2.4 GHz<->5 GHz hop moves default_ofdm_index by BBDiffBetweenBand*2 (0 dB vs -3 dB
    swing => +-6 indices) and the clear rebases the thermal baseline to the EFUSE value."""
    st = watchdog.WatchdogState(eeprom_thermal=35, bb_swing_diff_2g=0, bb_swing_diff_5g=-3)
    assert st.default_ofdm_index == 24
    powertrack.on_channel_switch(st, 4, 52)                # 2.4 GHz -> 5 GHz
    assert st.default_ofdm_index == 18                     # 24 - (0 - (-3)) * 2
    assert st.thermal_value == 35                          # clear rebases thermal to eeprom base
    powertrack.on_channel_switch(st, 52, 6)                # 5 GHz -> 2.4 GHz
    assert st.default_ofdm_index == 24


def test_same_band_hop_clears_without_reindex():
    """A same-band hop clears the power indices but leaves default_ofdm_index unchanged."""
    st = watchdog.WatchdogState(eeprom_thermal=35)
    st.delta_power_index_last = [3, 3, 3, 3]
    powertrack.on_channel_switch(st, 1, 6)                 # 2.4 GHz -> 2.4 GHz
    assert st.default_ofdm_index == 24
    assert st.delta_power_index_last == [0, 0, 0, 0]
