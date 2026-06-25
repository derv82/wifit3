"""M2b-1: read_revisions derives the AR9271 SREV, and the power-on reset emits the exact
RTC/RC register sequence (single FORCE_WAKE, the two multi-write batches, the STATUS poll)."""
import struct

from wifit3.chips.ar9271_v2 import hw, reg as R
from wifit3.chips.ar9271_v2.transport import AR9271Transport
from wifit3.chips.ar9271_v2.wmi import WMI


class FakeDev:
    """Answers each WMI command. REG_READ values are served from per-register queues so the
    SREV read, the STATUS poll (6 not-ready then ON) and the INTR_SYNC/RC reads drive the
    real control flow; writes get a dummy echo."""

    def __init__(self, read_values):
        self.cmds = []                       # (cmd_id, body) issued
        self._reads = {addr: list(vals) for addr, vals in read_values.items()}

    def write(self, ep, data, timeout=None):
        cmd_id, seq = struct.unpack_from(">HH", data, 8)
        body = data[12:]
        self.cmds.append((cmd_id, body))
        if cmd_id == 0x0014:                 # REG_READ -> value of the (last) addr requested
            addr = struct.unpack(">I", body[-4:])[0]
            val = self._reads[addr].pop(0)
        else:
            val = 0
        self._resp = struct.pack(">BBH", 1, 0, 8) + b"\x00\x00\x00\x00" + \
            struct.pack(">HH", cmd_id, seq) + struct.pack(">I", val)
        return len(data)

    def read(self, ep, length, timeout=None):
        return bytearray(self._resp)


def _run():
    dev = FakeDev({
        R.AR_SREV: [0x001411FF],                       # -> macVersion 0x140, macRev 1
        R.AR_RTC_STATUS: [0, 0, 0, 0, 0, 0, R.AR_RTC_STATUS_ON],   # 7th read = ON
        R.AR_INTR_SYNC_CAUSE: [0],                      # masked 0 -> AR_RC=AHB branch
        R.AR_RTC_RC: [0],                               # reset cleared immediately
    })
    w = WMI(AR9271Transport(dev), ctrl_epid=1)
    ath = hw.init_reset(w)
    return ath, dev


def test_srev_derivation():
    ath, _ = _run()
    assert ath.macVersion == R.AR_SREV_VERSION_9271      # 0x140
    assert ath.macRev == 1
    assert ath.is_9271() and not ath.is_9300_20_or_later()


def test_reset_register_sequence():
    _, dev = _run()
    READ, WRITE = 0x0014, 0x0015

    # The exact command stream the power-on reset issues (cmd_id, body-hex).
    expected = [
        (READ,  "00004020"),                                   # SREV
        (WRITE, "0000704c00000003"),                            # FORCE_WAKE single (set_reset_reg)
        (WRITE, "0000704c00000003" "0000400000000001" "0000704000000000"),  # power_on flush
        (WRITE, "0000400000000000"),                            # AR_RC = 0
        (WRITE, "0000704000000001"),                            # AR_RTC_RESET = 1
    ]
    got = [(c, b.hex()) for c, b in dev.cmds]
    assert got[:5] == expected

    # STATUS poll: 7 reads of AR_RTC_STATUS.
    status_reads = [b for c, b in dev.cmds if c == READ and b.hex() == "00007044"]
    assert len(status_reads) == 7

    # set_reset(WARM): INTR_SYNC read then the warm flush batch [FORCE_WAKE, AR_RC, AR_RTC_RC].
    assert (READ, bytes.fromhex("00004028")) in dev.cmds
    warm_flush = "0000704c00000003" "0000400000000001" "0000700000000001"
    assert (WRITE, bytes.fromhex(warm_flush)) in dev.cmds
