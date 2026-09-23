"""rtl8188ftv_dkms M5g: TX power by-rate tables + index computation."""


class FakeT:
    def __init__(self, reads):
        self._reads = list(reads)
        self.writes = []

    def read32(self, addr):
        return self._reads.pop(0)

    def write32(self, addr, value):
        self.writes.append((addr, value & 0xFFFFFFFF))


def _params(cck=0x1D, bw40=0x20, ofdm=3, bw20=1):
    from wifit3.chips.rtl8188ftv_dkms import prom
    params = prom.EfuseParams()
    params.cck_base_ch = [[cck] * 14]
    params.bw40_base_ch = [[bw40] * 14]
    params.txpower.ofdm_diff = [[ofdm, 0, 0, 0]]
    params.txpower.bw20_diff = [[bw20, 0, 0, 0]]
    return params


def test_rate_values_bcd():
    from wifit3.chips.rtl8188ftv_dkms import txpower as T
    assert T.rate_values(0xE00, 0xFFFFFFFF, 0x34363636) == [
        (T.MGN_6M, 36), (T.MGN_9M, 36), (T.MGN_12M, 36), (T.MGN_18M, 34)]
    assert T.rate_values(0xE08, 0xFF00, 0x3200) == [(T.MGN_1M, 32)]
    assert T.rate_values(0x86C, 0xFFFFFF00, 0x32323200) == [
        (T.MGN_2M, 32), (T.MGN_5_5M, 32), (T.MGN_11M, 32)]
    assert T.rate_values(0x86C, 0x000000FF, 0x29) == [(T.MGN_11M, 29)]
    assert T.rate_values(0xE14, 0xFFFFFFFF, 0x26262830) == [
        (T.MGN_MCS0 + 4, 30), (T.MGN_MCS0 + 5, 28),
        (T.MGN_MCS0 + 6, 26), (T.MGN_MCS0 + 7, 26)]
    assert T.rate_values(0x999, 0xFFFFFFFF, 0x0) == []


def test_load_convert_get_round_trip():
    from wifit3.chips.rtl8188ftv_dkms import txpower as T
    tables = T.load_pg_tables([
        (0, 0, 0, 0xE04, 0xFFFFFFFF, 0x28303234),
        (0, 0, 0, 0xE10, 0xFFFFFFFF, 0x30343434),
        (0, 0, 0, 0xE14, 0xFFFFFFFF, 0x26262830),
    ])
    assert tables.get(0, 0, 0, T.MGN_24M) == 6
    assert tables.get(0, 0, 0, T.MGN_54M) == 0
    assert tables.get(0, 0, 0, T.MGN_MCS0) == 8
    assert tables.get(0, 0, 0, T.MGN_1M) == 0
    assert tables.get(0, 0, 0, T.MGN_MCS0) == 8


def test_default_pg_tables_match_recorded():
    from wifit3.chips.rtl8188ftv_dkms import txpower as T
    tables = T.load_default_pg_tables()
    assert tables.get(0, 0, 0, T.MGN_6M) == 8
    assert tables.get(0, 0, 0, T.MGN_18M) == 6
    assert tables.get(0, 0, 0, T.MGN_MCS7) == 0


def test_index_base_and_get_index():
    from wifit3.chips.rtl8188ftv_dkms import txpower as T
    params = _params()
    tables = T.load_default_pg_tables()
    assert T.get_index(params, tables, 0, T.MGN_1M, 1) == 0x1D
    assert T.get_index(params, tables, 0, T.MGN_6M, 1) == 0x2B
    assert T.get_index(params, tables, 0, T.MGN_24M, 1) == 0x29
    assert T.get_index(params, tables, 0, T.MGN_MCS0, 1) == 0x29
    try:
        T.get_index(params, tables, 0, T.MGN_1M, 36)
    except ValueError:
        pass
    else:
        raise AssertionError("5 GHz accepted")
    try:
        T.get_index(params, tables, 0, T.MGN_1M, 1, reg_pwr_tbl_sel=1)
    except ValueError:
        pass
    else:
        raise AssertionError("limit path accepted")


def test_remnant_offsets():
    from wifit3.chips.rtl8188ftv_dkms import txpower as T
    params = _params()
    tables = T.load_default_pg_tables()
    assert T.get_index(params, tables, 0, T.MGN_1M, 7, rem_cck=1) == 0x1E
    assert T.get_index(params, tables, 0, T.MGN_1M, 7) == 0x1D
    assert T.get_index(params, tables, 0, T.MGN_6M, 7, rem_ofdm=1) == 0x2C
    assert T.get_index(params, tables, 0, T.MGN_6M, 7) == 0x2B


def test_set_level_writes_twenty_rates():
    from wifit3.chips.rtl8188ftv_dkms import txpower as T
    params = _params()
    tables = T.load_default_pg_tables()
    t = FakeT([0x0] * 20)
    T.set_level(t, 1, 0, params, tables)
    assert len(t.writes) == 20
    assert t.writes[0][0] == 0xE08
    assert t.writes[0] == (0xE08, (0x0 & ~0xFF00) | (0x1D << 8))
