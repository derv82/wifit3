"""M2e-8: reset DMA tail — STA_ID1 seqnum, set_dma, AR_OBS, RX interrupt mitigation."""
import struct

from wifit3.chips.ar9271_v2 import hw, reg as R
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
    h.reset_dma_and_intr()
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


def test_seqnum_and_dma():
    dev = _run()
    w, rmw = _writes(dev), _rmws(dev)
    assert (R.AR_STA_ID1, R.AR_STA_ID1_PRESERVE_SEQNUM, 0) in rmw
    assert (R.AR_AHB_MODE, R.AR_AHB_PREFETCH_RD_EN, 0) in rmw
    assert (R.AR_TXCFG, R.AR_TXCFG_DMASZ_128B, R.AR_TXCFG_DMASZ_MASK) in rmw
    assert (R.AR_TXCFG, R.SM(4, R.AR_FTRIG), R.AR_FTRIG) in rmw        # tx_trig_level 256B
    assert (R.AR_RXCFG, R.AR_RXCFG_DMASZ_128B, R.AR_RXCFG_DMASZ_MASK) in rmw
    assert w[R.AR_RXFIFO_CFG] == 0x200


def test_obs_and_rimt():
    dev = _run()
    w, rmw = _writes(dev), _rmws(dev)
    assert w[R.AR_OBS] == 8
    assert (R.AR_RIMT, R.SM(250, R.AR_RIMT_LAST), R.AR_RIMT_LAST) in rmw
    assert (R.AR_RIMT, R.SM(700, R.AR_RIMT_FIRST), R.AR_RIMT_FIRST) in rmw
