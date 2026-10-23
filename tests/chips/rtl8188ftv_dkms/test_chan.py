"""rtl8188ftv_dkms M5f: ch1 tune order (TX power ported next)."""


class FakeT:
    def __init__(self, reads):
        self._reads = list(reads)
        self.writes = []

    def read32(self, addr):
        return self._reads.pop(0)

    def write32(self, addr, value):
        self.writes.append((addr, 4, value & 0xFFFFFFFF))


def test_spur_cal_ch1_skips_psd():
    from wifit3.chips.rtl8188ftv_dkms import chan
    t = FakeT([0x0] * 4)
    chan.spur_calibration(t, 1, {"cur_ig": 0x20})
    assert t.writes == [(0xC40, 4, 0x1F000000), (0xC40, 4, 0x200),
                        (0xD2C, 4, 0x0)]


def test_spur_cal_psd_notch_ch7():
    from wifit3.chips.rtl8188ftv_dkms import chan
    t = FakeT([0x1F78423F, 0x1F78423F, 0x99000000, 0x69553424,
               0x83045700, 0x69553424, 0x76, 0x82045700, 0x69553430,
               0x0, 0x0C000000, 0x100, 0x100C07, 0xCB979975])
    hal = {"cur_ig": 0x24}
    chan.spur_calibration(t, 7, hal)
    assert hal["cur_ig"] == 0x24
    assert t.writes == [(0xC40, 4, 0x1F78423F), (0xC40, 4, 0x1F78423F),
                        (0x800, 4, 0x82045700), (0xC50, 4, 0x69553430),
                        (0x88C, 4, 0xCCF000C0), (0x808, 4, 0xFFCD),
                        (0x808, 4, 0x40FFCD), (0x808, 4, 0xFFCD),
                        (0x88C, 4, 0xCCC000C0), (0x800, 4, 0x83045700),
                        (0xC50, 4, 0x69553424), (0x824, 4, 0x0C000000),
                        (0x824, 4, 0x0C000000), (0x824, 4, 0x8C000000),
                        (0xD40, 4, 0x0),
                        (0xD44, 4, 0x0), (0xD48, 4, 0x0),
                        (0xD4C, 4, 0x06000000), (0xD2C, 4, 0xDB979975)]


def test_sw_chnl_threads_channel():
    from wifit3.chips.rtl8188ftv_dkms import chan
    t = FakeT([0x0] * 30)
    val = chan.sw_chnl(t, 1, 0x000C01, {"cur_ig": 0x20})
    assert val == 0x000C01
    assert (0x840, 4, (0x18 << 20) | 0x000C01) in t.writes


def test_rf_bandwidth_20_values():
    from wifit3.chips.rtl8188ftv_dkms import chan
    t = FakeT([])
    val = chan.rf_bandwidth_20(t, 0x000001)
    assert val == (0x000001 & 0xFFFFF3FF) | 0xC00
    assert t.writes == [(0x840, 4, (0x18 << 20) | val),
                        (0x840, 4, (0x87 << 20) | 0x65),
                        (0x840, 4, (0x1C << 20) | 0x00),
                        (0x840, 4, (0xDF << 20) | 0x140),
                        (0x840, 4, (0x1B << 20) | 0x1C6C)]
