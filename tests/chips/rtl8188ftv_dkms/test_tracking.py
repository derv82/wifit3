"""rtl8188ftv_dkms M5h: MIX thermal callback + RA retry count."""


class FakeT:
    def __init__(self, reads):
        self._reads = list(reads)
        self.writes = []

    def read8(self, addr):
        return self._reads.pop(0)

    def read16(self, addr):
        return self._reads.pop(0)

    def read32(self, addr):
        return self._reads.pop(0)

    def write8(self, addr, value):
        self.writes.append((addr, 1, value & 0xFF))

    def write16(self, addr, value):
        self.writes.append((addr, 2, value & 0xFFFF))

    def write32(self, addr, value):
        self.writes.append((addr, 4, value & 0xFFFFFFFF))


def test_delta_tables_match_recorded_pairs():
    from wifit3.chips.rtl8188ftv_dkms import track
    from wifit3.chips.rtl8188ftv_dkms import txpower as T
    tup, tdown = track.delta_tables(T.MGN_1M, 7)
    assert (tup[2], tup[3], tup[4], tup[5]) == (1, 2, 2, 3)
    assert (tdown[2], tdown[4]) == (2, 4)
    otup, _ = track.delta_tables(T.MGN_6M, 7)
    assert otup[2] == 2
    assert otup[2:] != tup[2:]


def test_set_iqk_matrix_captures():
    from wifit3.chips.rtl8188ftv_dkms import track
    t = FakeT([0x00000000, 0xA03E0346])
    track.set_iqk_matrix(t, 29, 0x103, 0x2)
    assert t.writes == [(0xC80, 4, 0x3C8100F4), (0xC94, 4, 0x00000000),
                        (0xC4C, 4, 0xA13E0346)]
    t = FakeT([0x00000000, 0xA13E0346])
    track.set_iqk_matrix(t, 30, 0x103, 0x2)
    assert t.writes == [(0xC80, 4, 0x40020103), (0xC94, 4, 0x00000000),
                        (0xC4C, 4, 0xA03E0346)]
    t = FakeT([0x00000000, 0xA03E0346])
    track.set_iqk_matrix(t, 31, 0x103, 0x2)
    assert t.writes[0] == (0xC80, 4, 0x43C20112)


def test_set_iqk_matrix_x_zero_writes_table():
    from wifit3.chips.rtl8188ftv_dkms import track
    t = FakeT([0x00000000, 0x00000000])
    track.set_iqk_matrix(t, 28, 0, 0)
    assert t.writes == [(0xC80, 4, 0x390000E4), (0xC94, 4, 0x00000000),
                        (0xC4C, 4, 0x00000000)]


def test_write_cck_swing_limit_row():
    from wifit3.chips.rtl8188ftv_dkms import track
    t = FakeT([])
    track.write_cck_swing(t, 20)
    assert [v for _, _, v in t.writes] == [
        0xD8, 0xD1, 0xBD, 0xA0, 0x7D, 0x5A, 0x3B, 0x22,
        0x10, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
    assert [a for a, _, _ in t.writes] == [
        0xA22, 0xA23, 0xA24, 0xA25, 0xA26, 0xA27, 0xA28, 0xA29,
        0xA9A, 0xA9B, 0xA9C, 0xA9D, 0xAA0, 0xAA1, 0xAA2, 0xAA3]


def _st(eeprom=0x1A):
    from wifit3.chips.rtl8188ftv_dkms import track
    st = track.tracking_init_state(eeprom)
    st["iqk_x"], st["iqk_y"] = 0x103, 0x2
    return st


def _params():
    from wifit3.chips.rtl8188ftv_dkms import prom
    from wifit3.chips.rtl8188ftv_dkms import txpower as T
    params = prom.EfuseParams()
    params.cck_base_ch = [[0x20] * 14]
    params.bw40_base_ch = [[0x20] * 14]
    params.txpower.ofdm_diff = [[0, 0, 0, 0]]
    params.txpower.bw20_diff = [[0, 0, 0, 0]]
    return params, T.ByRateTables()


def test_tracking_callback_first_setpwr():
    from wifit3.chips.rtl8188ftv_dkms import track
    st = _st()
    params, tables = _params()
    reads = [0xA1390204, 0xA1390204, 0x01000100, 0x001070E0,
             0x00000000, 0xA03E0346] + [0x0] * 20
    t = FakeT(reads)
    assert track.tracking_callback(t, st, 10, params, tables) is True
    assert (st["abs_ofdm"], st["rem_cck"], st["rem_ofdm"]) == (1, 1, 0)
    assert st["th_val"] == 28
    assert st["mod_cck"] is True and st["mod_ofdm"] is False
    assert (0xC80, 4, 0x3C8100F4) in t.writes
    assert (0xE08, 4, 0x00002100) in t.writes
    assert (0xE00, 4, 0x00000020) in t.writes
    assert (0xA22, 1, 0xD8) in t.writes


def test_tracking_callback_no_setpwr_on_zero_offset():
    from wifit3.chips.rtl8188ftv_dkms import track
    st = _st()
    params, tables = _params()
    t = FakeT([0xA1390204, 0xA1390204, 0x01000100, 0x001068D0])
    assert track.tracking_callback(t, st, 10, params, tables) is False
    assert (st["abs_ofdm"], st["rem_cck"], st["rem_ofdm"]) == (0, 0, 0)
    assert st["th_val"] == 0x1A
    assert len(t.writes) == 3


def test_tracking_callback_second_fires_on_table_step():
    from wifit3.chips.rtl8188ftv_dkms import track
    st = _st()
    params, tables = _params()
    t = FakeT([0xA1390204, 0xA1390204, 0x01000100, 0x001070E0,
               0x00000000, 0xA03E0346] + [0x0] * 20)
    assert track.tracking_callback(t, st, 10, params, tables) is True
    t = FakeT([0xA1390204, 0xA1390204, 0x01000100, 0x001074E0])
    assert track.tracking_callback(t, st, 10, params, tables) is False
    assert (st["abs_ofdm"], st["rem_cck"]) == (1, 1)
    t = FakeT([0xA1390204, 0xA1390204, 0x01000100, 0x001078E0,
               0x00000000, 0xA13E0346] + [0x0] * 20)
    assert track.tracking_callback(t, st, 10, params, tables) is True
    assert (st["abs_ofdm"], st["rem_cck"]) == (2, 2)
    assert (0xC80, 4, 0x40020103) in t.writes


def test_ra_dynamic_retry_count_flips_once():
    from wifit3.chips.rtl8188ftv_dkms import track
    st = _st()
    fa = {"all": 2022, "cca": 2075, "cck_fail": 0}
    t = FakeT([])
    track.ra_dynamic_retry_count(t, st, fa)
    assert t.writes == [(0x430, 4, 0x00000000), (0x434, 4, 0x04030201)]
    assert st["pre_noisy"] is True
    track.ra_dynamic_retry_count(t, st, fa)
    assert len(t.writes) == 2
    st2 = _st()
    st2["pre_noisy"] = True
    t = FakeT([])
    track.ra_dynamic_retry_count(t, st2, {"all": 0, "cca": 5000,
                                          "cck_fail": 0})
    assert t.writes == [(0x430, 4, 0x02010000), (0x434, 4, 0x06050403)]


def test_watchdog_trigger_tick():
    from wifit3.chips.rtl8188ftv_dkms import dm
    from wifit3.chips.rtl8188ftv_dkms import track
    reads = [0xA0, 0x0000, 0x48071D40, 0x04030740, 0x350000, 0x2F60642,
             0x1702F7, 0x2E, 0xD3A000, 0xD3B000, 0x154, 0x2005881,
             0x80801B04, 0x6C6C6CEC, 0xEC6C6CEC, 0x84030740, 0x8C030740,
             0xC8071D40, 0x84030740, 0xD3F000, 0xD3C000, 0xD3E000,
             0xD32000, 0xA07F037F, 0x8C390204, 0x8C390204, 0x01000100,
             0x00106800]
    t = FakeT(reads)
    st = {"cur_cck": 0, "cur_ig": 0x20, "th_l2h_ini": 0xF5,
          "adaptivity_ability": False, "tm_trigger": False,
          "channel": 10, "params": None, "by_rate": None}
    st.update(track.tracking_init_state(0x1A))
    st["tm_trigger"] = False
    fa = dm.watchdog_tick(t, st)
    assert fa["all"] == 2235
    assert st["tm_trigger"] is True
    assert st["pre_noisy"] is True
    assert (0x430, 4, 0x00000000) in t.writes
    assert (0xA0A, 1, 0x40) in t.writes
    assert t.writes[-1] == (0x840, 4, 0x04236800)
