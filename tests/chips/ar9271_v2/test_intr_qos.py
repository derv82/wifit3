"""M2e-6: init_interrupt_masks, ani_cache_ini_regs (reads), init_qos."""
import struct

from wifit3.chips.ar9271_v2 import hw, reg as R
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import WMI


class FakeDev:
    def __init__(self):
        self.cmds = []
        self.reads = []

    def write(self, ep, data, timeout=None):
        cmd_id, seq = struct.unpack_from(">HH", data, 8)
        body = data[12:]
        self.cmds.append((cmd_id, body))
        if cmd_id == 0x14:
            self.reads.append(struct.unpack_from(">I", body, 0)[0])
        self._resp = struct.pack(">BBH", 1, 0, 8) + b"\x00\x00\x00\x00" + \
            struct.pack(">HH", cmd_id, seq) + struct.pack(">I", 0)
        return len(data)

    def read(self, ep, length, timeout=None):
        return bytearray(self._resp)


def _hw():
    return hw.AthHw(WMI(AR9271Transport(FakeDev()), ctrl_epid=1))


def _writes(dev):
    out = {}
    for c, b in dev.cmds:
        if c == 0x15:
            for k in range(0, len(b) - 4, 8):
                reg, val = struct.unpack_from(">II", b, k)
                out[reg] = val
    return out


def test_interrupt_masks_rx_mitigation():
    h = _hw()
    h.init_interrupt_masks()
    w = _writes(h.wmi.t.dev)
    # rx-mitigation on, tx-mitigation off, not 9300: RXINTM|RXMINTR|TXOK|base.
    assert w[R.AR_IMR] == 0x81800964
    assert w[R.AR_IMR_S2] == R.AR_IMR_S2_GTT
    assert w[R.AR_INTR_SYNC_CAUSE] == 0xFFFFFFFF
    assert w[R.AR_INTR_SYNC_ENABLE] == R.AR_INTR_SYNC_DEFAULT == 0x23F60
    assert w[R.AR_INTR_SYNC_MASK] == 0


def test_ani_cache_reads_in_order():
    h = _hw()
    h.ani_cache_ini_regs()
    assert h.wmi.t.dev.reads == [R.AR_PHY_SFCORR, R.AR_PHY_SFCORR_LOW, R.AR_PHY_SFCORR_EXT,
                                 R.AR_PHY_FIND_SIG, R.AR_PHY_FIND_SIG_LOW, R.AR_PHY_TIMING5,
                                 R.AR_PHY_EXT_CCA]


def test_init_qos():
    h = _hw()
    h.init_qos()
    w = _writes(h.wmi.t.dev)
    assert w[R.AR_MIC_QOS_CONTROL] == 0x100AA
    assert w[R.AR_MIC_QOS_SELECT] == 0x3210
    assert w[R.AR_QOS_NO_ACK] == 0x52
    assert w[R.AR_TXOP_X] == R.AR_TXOP_X_VAL
    assert w[R.AR_TXOP_0_3] == 0xFFFFFFFF and w[R.AR_TXOP_12_15] == 0xFFFFFFFF
