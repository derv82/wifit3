"""Hardware-free regression for the RTL8188EUS (DKMS) no-link phydm DM watchdog.

Locks the decision logic the byte-for-byte ``verify_pcap.py verify_dm_tick`` gate can't
isolate on its own: the 11N FA-counter sum, the no-link IGI step (+2/+1/-2 by fa_th
2000/4000/5000) + [0x1c, 0x2a] clamp, and the CCK-PD threshold selection (no-link
FA>1000 -> 0x83, FA<500 -> 0x40) with write-on-change + moving-average reset. The carried
DM state is threaded explicitly. Runtime RX benefit is validated by the beacon-watch A/B.
"""
from wifit3.chips.rtl8188eus_dkms import dig


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


def test_init_state_seeds_from_chip():
    # phydm_cck_pd_init reads the CCK CCA default from 0xa08[23:16]; IGI seed is 0x20.
    st = dig.init_state(RegTx({0x0A08: 0x00CD0000}))
    assert st.cur_ig_value == 0x20
    assert st.cur_cck_cca_thres == 0xCD
    assert st.cck_fa_ma == dig.CCK_FA_MA_RESET


def test_watchdog_tick_clamps_and_writes_igi(mocker):
    mocker.patch("wifit3.chips.rtl8188eus_dkms.powertrack.thermal_tick")  # DIG test, not TX
    # Low FA -> IGI steps down from the carried value; clamps at 0x1c (sensitive floor).
    st = dig.WatchdogState(cur_ig_value=0x1D, cur_cck_cca_thres=0x40)
    t = RegTx({0x0C50: 0x1D})                      # all FA regs 0 -> cnt_all=0 < fa_th[0]
    tick = dig.watchdog_tick(t, st, None)
    assert tick.igi == 0x1C                         # 0x1d - 2 = 0x1b -> clamped to 0x1c
    assert (0x0C50, 0x1C) in t.w
    assert st.cur_ig_value == 0x1C                  # state carried forward
    # High FA -> IGI steps up; clamps at 0x2a (least sensitive).
    st = dig.WatchdogState(cur_ig_value=0x29, cur_cck_cca_thres=0x40)
    tick = dig.watchdog_tick(RegTx({0x0CF0: 6000}), st, None)  # cnt_all 6000 > fa_th[2]
    assert tick.igi == 0x2A                         # 0x29 + 2 = 0x2b -> clamped to 0x2a


def test_watchdog_tick_no_igi_write_when_unchanged(mocker):
    mocker.patch("wifit3.chips.rtl8188eus_dkms.powertrack.thermal_tick")
    st = dig.WatchdogState(cur_ig_value=0x20, cur_cck_cca_thres=0x40)
    t = RegTx({0x0C50: 0x20, 0x0CF0: 3000})        # in-band FA -> IGI unchanged
    dig.watchdog_tick(t, st, None)
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
