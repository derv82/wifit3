"""Hardware-free regression for the RTL8188EUS (DKMS) no-link phydm DM watchdog.

Locks the decision logic the byte-for-byte ``verify_pcap.py verify_dm_tick`` gate can't
isolate on its own: the 11N FA-counter sum, the no-link IGI step (+2/+1/-2 by fa_th
2000/4000/5000) + [0x1c, 0x2a] clamp, and the CCK-PD threshold selection (no-link
FA>1000 -> 0x83, FA<500 -> 0x40) with write-on-change + moving-average reset. The carried
DM state is threaded explicitly. Runtime RX benefit is validated by the beacon-watch A/B.
"""
from wifit3.chips.rtl8188eus_dkms import dig, powertrack


class RegTx:
    """Stateful register fake; FA-counter regs served from a dict."""
    def __init__(self, regs):
        self.regs = dict(regs)
        self.w = []

    def read32(self, a):
        return self.regs.get(a, 0)

    def write32(self, a, v):
        v &= 0xFFFFFFFF
        self.regs[a] = v
        self.w.append((a, v))

    def write8(self, a, v):
        v &= 0xFF
        self.regs[a] = v
        self.w.append((a, v))


def _pt_state(channel=1):
    pt = powertrack.seed_state(0, 0, 0x18, None, False)
    pt.channel = channel
    return pt


def test_read_fa_counters_sums_ofdm_and_cck():
    regs = {
        0x0CF0: (5 << 16) | 3,      # sb_search=5, fast_fsync=3
        0x0DA0: (7 << 16) | 999,    # parity=7 (low half ofdm_cca ignored)
        0x0DA4: (11 << 16) | 2,     # crc8=11, rate_illegal=2
        0x0DA8: 4,                  # mcs=4
        0x0A5C: 0x10,               # cck low byte = 0x10
        0x0A58: 0x02 << 24,         # cck high byte = 0x02 -> +0x200
    }
    ofdm, cck = dig._read_fa_counters(RegTx(regs))
    assert ofdm == 3 + 5 + 7 + 2 + 11 + 4         # 32
    assert cck == 0x10 | (0x02 << 8)              # 0x210 = 528


def test_new_igi_by_fa_steps():
    assert dig._new_igi_by_fa(0x20, 6000) == 0x22   # > fa_th[2] -> +2
    assert dig._new_igi_by_fa(0x20, 4500) == 0x21   # > fa_th[1] -> +1
    assert dig._new_igi_by_fa(0x20, 1000) == 0x1E   # < fa_th[0] -> -2
    assert dig._new_igi_by_fa(0x20, 3000) == 0x20   # in band -> hold


def test_seed_state_from_carried_values():
    # The DM seed is carried from InitHalDm (dm.DmSeed): IGI (0xc50) + CCK CCA default
    # (0xa08[23:16]). seed_state builds the WatchdogState from those values — no chip read.
    st = dig.seed_state(0x20, 0xCD)
    assert st.cur_ig_value == 0x20
    assert st.cur_cck_cca_thres == 0xCD
    assert st.cck_fa_ma == dig.CCK_FA_MA_RESET


def test_watchdog_tick_clamps_and_writes_igi(mocker):
    mocker.patch("wifit3.chips.rtl8188eus_dkms.powertrack.thermal_tick")  # DIG test, not TX
    # Low FA -> IGI steps down from the carried value; clamps at 0x1c (sensitive floor).
    st = dig.WatchdogState(cur_ig_value=0x1D, cur_cck_cca_thres=0x40)
    t = RegTx({0x0C50: 0x1D})                      # all FA regs 0 -> cnt_all=0 < fa_th[0]
    tick = dig.watchdog_tick(t, st, _pt_state())
    assert tick.igi == 0x1C                         # 0x1d - 2 = 0x1b -> clamped to 0x1c
    assert (0x0C50, 0x1C) in t.w
    assert st.cur_ig_value == 0x1C                  # state carried forward
    # High FA -> IGI steps up; clamps at 0x2a (least sensitive).
    st = dig.WatchdogState(cur_ig_value=0x29, cur_cck_cca_thres=0x40)
    tick = dig.watchdog_tick(RegTx({0x0CF0: 6000}), st, _pt_state())  # cnt_all 6000 > fa_th[2]
    assert tick.igi == 0x2A                         # 0x29 + 2 = 0x2b -> clamped to 0x2a


def test_watchdog_tick_no_igi_write_when_unchanged(mocker):
    mocker.patch("wifit3.chips.rtl8188eus_dkms.powertrack.thermal_tick")
    st = dig.WatchdogState(cur_ig_value=0x20, cur_cck_cca_thres=0x40)
    t = RegTx({0x0C50: 0x20, 0x0CF0: 3000})        # in-band FA -> IGI unchanged
    dig.watchdog_tick(t, st, _pt_state())
    assert not any(a == 0x0C50 for a, _ in t.w)    # no IGI write


def test_cck_pd_picks_sensitive_threshold_and_writes_on_change():
    # No-link low CCK FA -> threshold drops to the sensitive 0x40; written once (0xa0a)
    # because it differs from the seeded default, and the moving average is reset.
    st = dig.WatchdogState(cur_ig_value=0x20, cur_cck_cca_thres=0xCD)
    t = RegTx({})
    dig._cck_pd(t, st, cck_fa=10)                  # MA reset -> 10 < 500 -> 0x40
    assert (0x0A0A, 0x40) in t.w
    assert st.cur_cck_cca_thres == 0x40
    assert st.cck_fa_ma == dig.CCK_FA_MA_RESET      # write resets the MA


def test_cck_pd_high_fa_picks_conservative_threshold():
    st = dig.WatchdogState(cur_ig_value=0x20, cck_fa_ma=2000, cur_cck_cca_thres=0x40)
    t = RegTx({})
    dig._cck_pd(t, st, cck_fa=2000)                # MA stays > 1000 -> 0x83
    assert (0x0A0A, 0x83) in t.w
    assert st.cur_cck_cca_thres == 0x83


def test_adaptivity_drives_edcca_from_igi():
    # mode2 (no ADAPTIVITY support): th_l2h = 20 + (0x32 - igi); th_h2l = th_l2h - 8.
    # igi=0x22 -> th_l2h=0x24, th_h2l=0x1c; written into 0xc4c byte0/byte2 over the seed.
    st = dig.WatchdogState(cur_ig_value=0x22, cur_cck_cca_thres=0x40)
    t = RegTx({0x0C4C: 0x007F037F})               # no-link seed (0x7f/0x7f)
    dig._adaptivity(t, st)
    assert (0x0C4C, 0x001C0324) in t.w            # L2H=0x24 (byte0), H2L=0x1c (byte2)


def test_nhm_thresholds_track_igi():
    # NHM noise thresholds: th[0]=(igi-14)<<1, th[i]=th[0]+4i; packed little-end into
    # 0x898 (th0-3) / 0x89c (th4-7) / 0xe28 (th8). igi=0x22 -> 0x34302c28 / 0x44403c38 / 0x48.
    st = dig.WatchdogState(cur_ig_value=0x22, cur_cck_cca_thres=0x40, nhm_igi=0x20)
    t = RegTx({0x0890: 0x4C480900, 0x0894: 0xFFFFFFFF, 0x0E28: 0x44})
    dig._nhm(t, st)
    assert (0x0898, 0x34302C28) in t.w
    assert (0x089C, 0x44403C38) in t.w
    assert (0x0E28, 0x48) in t.w
    assert st.nhm_igi == 0x22                      # threshold cache tracks the new IGI


def test_nhm_skips_unchanged_writes():
    # IGI unchanged + period already set -> thresholds and the period write are skipped.
    st = dig.WatchdogState(cur_ig_value=0x20, cur_cck_cca_thres=0x40, nhm_igi=0x20,
                           nhm_configured=True, nhm_period=65534)
    t = RegTx({0x0890: 0x504C0100, 0x0894: 0xFFFEFFFF})
    dig._nhm(t, st)
    assert not any(a == 0x0898 for a, _ in t.w)    # IGI unchanged -> no threshold write
    assert not any(a == 0x0894 for a, _ in t.w)    # period already max -> no rewrite


# --- phydm_receiver_blocking ----------------------------------------------
_T_CUT = 0x0000          # SYS_CFG[15:12] < 8 -> 8188E_T
_S_CUT = 0x8000          # SYS_CFG[15:12] >= 8 -> 8188E_S


def _armable_state(**kw):
    return dig.WatchdogState(cur_ig_value=0x20, consecutive_idlel_time=12,
                             adaptivity_enable=True, **kw)


def test_receiver_blocking_t_cut_ch1_arms_nbi():
    # fc 2412, f_intf 2410 -> tone_idx_tmp 64 -> nbi_table_128 reg_idx 3; then NBI enable BIT9.
    st = _armable_state()
    t = RegTx({0x00F0: _T_CUT})
    dig._receiver_blocking(t, st, 1)
    assert [w for w in t.w if w[0] == 0x0C40] == [(0x0C40, 0x03000000), (0x0C40, 0x03000200)]
    assert st.is_receiver_blocking_en is True


def test_receiver_blocking_t_cut_ch13_arms_nbi():
    # fc 2472, f_intf 2473 -> tone_idx_tmp 32 -> reg_idx 2.
    st = _armable_state()
    t = RegTx({0x00F0: _T_CUT})
    dig._receiver_blocking(t, st, 13)
    assert t.regs[0x0C40] == 0x02000200
    assert st.is_receiver_blocking_en is True


def test_receiver_blocking_s_cut_ch13_arms_nbi():
    st = _armable_state()
    t = RegTx({0x00F0: _S_CUT})
    dig._receiver_blocking(t, st, 13)
    assert t.regs[0x0C40] == 0x02000200
    assert st.is_receiver_blocking_en is True


def test_receiver_blocking_s_cut_ch1_does_not_arm():
    st = _armable_state()
    t = RegTx({0x00F0: _S_CUT})
    dig._receiver_blocking(t, st, 1)
    assert t.w == []
    assert st.is_receiver_blocking_en is False


def test_receiver_blocking_disarms_on_leaving_ch1_ch13():
    # Disarm: NBI enable BIT9 cleared, then 0xc40[28:24] = 0x1f.
    st = _armable_state(is_receiver_blocking_en=True)
    t = RegTx({0x00F0: _T_CUT, 0x0C40: 0x03000200})
    dig._receiver_blocking(t, st, 6)
    assert [w for w in t.w if w[0] == 0x0C40] == [(0x0C40, 0x03000000), (0x0C40, 0x1F000000)]
    assert st.is_receiver_blocking_en is False
    st = _armable_state(is_receiver_blocking_en=True)            # S-cut: leaving ch13 for ch1
    t = RegTx({0x00F0: _S_CUT, 0x0C40: 0x02000200})
    dig._receiver_blocking(t, st, 1)
    assert t.regs[0x0C40] == 0x1F000000
    assert st.is_receiver_blocking_en is False


def test_receiver_blocking_disarms_when_gate_closes():
    # Armed, but adaptivity_enable False -> the unconditional disarm at the tail.
    st = dig.WatchdogState(cur_ig_value=0x20, consecutive_idlel_time=12,
                           is_receiver_blocking_en=True)
    t = RegTx({0x00F0: _T_CUT, 0x0C40: 0x03000200})
    dig._receiver_blocking(t, st, 1)
    assert t.regs[0x0C40] == 0x1F000000
    assert st.is_receiver_blocking_en is False


def test_receiver_blocking_idle_time_gate():
    st = _armable_state()
    st.consecutive_idlel_time = 10                                # not > 10
    t = RegTx({0x00F0: _T_CUT})
    dig._receiver_blocking(t, st, 1)
    assert t.w == []
    assert st.is_receiver_blocking_en is False


def test_watchdog_tick_updates_idle_time_before_receiver_blocking(mocker):
    # phydm_common_info_self_update (idle += 2) runs before phydm_receiver_blocking, and
    # phydm_adaptivity's check (adaptivity_enable = False) runs after it.
    mocker.patch("wifit3.chips.rtl8188eus_dkms.powertrack.thermal_tick")
    st = dig.WatchdogState(cur_ig_value=0x20, cur_cck_cca_thres=0x40,
                           consecutive_idlel_time=10, adaptivity_enable=True)
    t = RegTx({0x00F0: _T_CUT, 0x0CF0: 3000})
    dig.watchdog_tick(t, st, _pt_state(1))
    assert st.consecutive_idlel_time == 12
    assert st.is_receiver_blocking_en is True
    assert st.adaptivity_enable is False


def test_watchdog_tick_never_arms_in_this_build(mocker):
    mocker.patch("wifit3.chips.rtl8188eus_dkms.powertrack.thermal_tick")
    st = dig.WatchdogState(cur_ig_value=0x20, cur_cck_cca_thres=0x40)
    for _ in range(10):
        t = RegTx({0x00F0: _T_CUT, 0x0CF0: 3000})
        dig.watchdog_tick(t, st, _pt_state(1))
        assert not any(a == 0x0C40 for a, _ in t.w)
    assert st.consecutive_idlel_time == 20
    assert st.is_receiver_blocking_en is False
