"""rtl8188ftv_dkms M5h: CAM invalidate."""


class FakeT:
    def __init__(self, reads):
        self._reads = list(reads)
        self.writes = []

    def read32(self, addr):
        return self._reads.pop(0)

    def write32(self, addr, value):
        self.writes.append((addr, 4, value & 0xFFFFFFFF))


def test_invalidate_cam_all():
    from wifit3.chips.rtl8188ftv_dkms import sec
    t = FakeT([])
    sec.invalidate_cam_all(t)
    assert t.writes == [(0x670, 4, 0xC0000000)]
