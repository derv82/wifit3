"""M2e-7: init_global_settings — SIFS/slot/ACK/CTS/EIFS MAC timing (2.4 GHz, 20 MHz)."""
import struct

from wifit3.chips.ar9271_v2 import chan as chanmod, hw, reg as R
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import WMI


class FakeDev:
    """Returns the recorded EIFS / AR_USEC reads so the EIFS round-trip and USEC fields match."""
    def __init__(self):
        self.cmds = []
        self._reads = {R.AR_D_GBL_IFS_EIFS: 0x3E38, R.AR_USEC: 0x12E0002B}

    def write(self, ep, data, timeout=None):
        cmd_id, seq = struct.unpack_from(">HH", data, 8)
        body = data[12:]
        self.cmds.append((cmd_id, body))
        val = 0
        if cmd_id == 0x14:
            val = self._reads.get(struct.unpack_from(">I", body, 0)[0], 0)
        self._resp = struct.pack(">BBH", 1, 0, 8) + b"\x00\x00\x00\x00" + \
            struct.pack(">HH", cmd_id, seq) + struct.pack(">I", val)
        return len(data)

    def read(self, ep, length, timeout=None):
        return bytearray(self._resp)


def _run():
    dev = FakeDev()
    h = hw.AthHw(WMI(AR9271Transport(dev), ctrl_epid=1))
    h.init_global_settings(chanmod.channel_2ghz(1))
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


def test_sifs_slot_eifs():
    w = _writes(_run())
    assert w[R.AR_D_GBL_IFS_SIFS] == 0x160       # mac_to_clks(10-2) = 8*44
    assert w[R.AR_D_GBL_IFS_SLOT] == 0x18C       # mac_to_clks(9) = 9*44
    assert w[R.AR_D_GBL_IFS_EIFS] == 0x3E38      # round-trip of the recorded read


def test_ack_cts_timeouts():
    rmw = _rmws(_run())
    # ack 64us -> 64*44=0xB00; cts 48us -> 48*44=0x840 << 16.
    assert (R.AR_TIME_OUT, 0xB00, R.AR_TIME_OUT_ACK) in rmw
    assert (R.AR_TIME_OUT, R.SM(0x840, R.AR_TIME_OUT_CTS), R.AR_TIME_OUT_CTS) in rmw


def test_misc_mode_and_usec():
    dev = _run()
    rmw = _rmws(dev)
    assert (R.AR_PCU_MISC, R.AR_PCU_MIC_NEW_LOC_ENA, 0) in rmw
    # AR_USEC low byte = clockrate-1 = 43.
    usec = [r for r in rmw if r[0] == R.AR_USEC][0]
    assert (usec[1] & R.AR_USEC_USEC) == 43
