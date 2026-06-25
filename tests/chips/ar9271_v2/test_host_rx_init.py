"""M6: host_rx_init — RX DMA enable, STA rx/mcast filters, startpcureceive, cap-target.

The filter read-backs (getrxfilter) return 0 from the mock, so calcrxfilter preserves no
phy-error bits and resolves to the STA default 0x207.
"""
import struct

from wifit3.chips.ar9271_v2 import hw, reg as R, rx
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import WMI


class FakeDev:
    def __init__(self):
        self.cmds = []

    def write(self, ep, data, timeout=None):
        cmd_id, seq = struct.unpack_from(">HH", data, 8)
        payload = data[12:]
        self.cmds.append((cmd_id, payload))
        if cmd_id == 0x14:
            body = b"\x00\x00\x00\x00" * max(1, len(payload) // 4)
        else:
            body = struct.pack(">I", 0)
        self._resp = struct.pack(">BBH", 1, 0, len(body) + 4) + b"\x00\x00\x00\x00" + \
            struct.pack(">HH", cmd_id, seq) + body
        return len(data)

    def read(self, ep, length, timeout=None):
        return bytearray(self._resp)


def _run():
    dev = FakeDev()
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    rx.host_rx_init(h)
    return dev


def _writes(dev):
    out = {}
    for c, b in dev.cmds:
        if c == 0x15:
            for k in range(0, len(b), 8):
                reg, val = struct.unpack_from(">II", b, k)
                out[reg] = val
    return out


def _rmws(dev):
    out = []
    for c, b in dev.cmds:
        if c == 0x20:
            out += [struct.unpack_from(">III", b, k) for k in range(0, len(b), 12)]
    return out


def test_rxena_and_filters():
    dev = _run()
    w = _writes(dev)
    assert w[R.AR_CR] == R.AR_CR_RXE
    # STA default filter: ucast|mcast|bcast|mybeacon, no phyerr -> AR_PHY_ERR cleared.
    assert w[R.AR_RX_FILTER] == 0x207
    assert w[R.AR_PHY_ERR] == 0
    assert w[R.AR_MCAST_FIL0] == 0xFFFFFFFF
    assert w[R.AR_MCAST_FIL1] == 0xFFFFFFFF


def test_zlfdma_cleared_and_pcu_unblocked():
    rmw = _rmws(_run())
    # no phyerr bits -> ZLFDMA cleared; startpcureceive clears RX block + abort.
    assert (R.AR_RXCFG, 0, R.AR_RXCFG_ZLFDMA) in rmw
    assert (R.AR_DIAG_SW, 0, R.AR_DIAG_RX_DIS | R.AR_DIAG_RX_ABORT) in rmw


def test_cap_target_payload():
    dev = FakeDev()
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    h.wmi.update_cap_target(h.txchainmask)
    payload = next(b for c, b in dev.cmds if c == 0x18)
    assert payload == bytes.fromhex("0000ffffff000100")
