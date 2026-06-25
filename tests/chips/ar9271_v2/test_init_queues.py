"""M2e-5: init_queues — DQCUMASK seed + per-queue DCU/QCU config (data, CAB, beacon)."""
import struct

from wifit3.chips.ar9271_v2 import hw, mac_queue, reg as R
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import WMI


class FakeDev:
    def __init__(self):
        self.cmds = []

    def write(self, ep, data, timeout=None):
        cmd_id, seq = struct.unpack_from(">HH", data, 8)
        self.cmds.append((cmd_id, data[12:]))
        self._resp = struct.pack(">BBH", 1, 0, 8) + b"\x00\x00\x00\x00" + \
            struct.pack(">HH", cmd_id, seq) + struct.pack(">I", 0)
        return len(data)

    def read(self, ep, length, timeout=None):
        return bytearray(self._resp)


def _run():
    dev = FakeDev()
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    mac_queue.init_tx_queues(h)
    mac_queue.init_queues(h)
    return dev


def _writes(dev):
    out = {}
    for c, b in dev.cmds:
        if c == 0x15:
            for k in range(0, len(b) - 4, 8):
                reg, val = struct.unpack_from(">II", b, k)
                out[reg] = val
    return out


def _rmws(dev):
    out = []
    for c, b in dev.cmds:
        if c == 0x20:
            out += [struct.unpack_from(">III", b, k) for k in range(0, len(b) - 4, 12)]
    return out


def test_queue_allocation():
    h = hw.AthHw(WMI(AR9271Transport(FakeDev()), ctrl_epid=1))
    mac_queue.init_tx_queues(h)
    # 4 data (0-3), CAB (8), beacon (9) active; 4-7 inactive.
    assert [q.tqi_type for q in h.txq] == [R.TXQ_DATA] * 4 + [R.TXQ_INACTIVE] * 4 \
        + [R.TXQ_CAB, R.TXQ_BEACON]


def test_dqcumask_seed():
    w = _writes(_run())
    for i in range(R.AR_NUM_DCU):
        assert w[R.AR_DQCUMASK(i)] == (1 << i)


def test_data_queue_config():
    w = _writes(_run())
    assert w[R.AR_DLCL_IFS(0)] == 0x2FFC0F        # cwmin15/cwmax1023/aifs2
    assert w[R.AR_DRETRY_LIMIT(0)] == 0x8200A
    assert w[R.AR_QMISC(0)] == R.AR_Q_MISC_DCU_EARLY_TERM_REQ
    assert w[R.AR_DMISC(0)] == 0x1102


def test_cab_and_beacon():
    dev = _run()
    w, rmw = _writes(dev), _rmws(dev)
    # CAB (q8): readyTime cfg = (0 - (6-1))*1024 | EN  -> 0xFFFFEC00.
    assert w[R.AR_QRDYTIMECFG(8)] == 0xFFFFEC00
    assert (R.AR_QMISC(8), 0x62, 0) in rmw         # FSP_DBA_GATED|CBR_INCR_DIS1|DIS0
    # Beacon (q9): DLCL cwmin1/cwmax1/aifs1 and the beacon SET_BITs.
    assert w[R.AR_DLCL_IFS(9)] == 0x100401
    assert (R.AR_QMISC(9), 0xA2, 0) in rmw         # FSP_DBA_GATED|BEACON_USE|CBR_INCR_DIS1
    assert (R.AR_DMISC(9), 0x250000, 0) in rmw


def test_interrupt_masks():
    dev = _run()
    w = _writes(dev)
    # After all queues: txdesc/txeol masks = bits 0-3,8 = 0x10f (beacon q9 has no int flags).
    assert w[R.AR_IMR_S0] == R.SM(0x10F, R.AR_IMR_S0_QCU_TXDESC)
    assert w[R.AR_IMR_S1] == R.SM(0x10F, R.AR_IMR_S1_QCU_TXEOL)
