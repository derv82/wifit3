"""M2b mechanism: WMI command framing + register I/O reproduce the exact wire bytes the
ath9k_htc init issues (sampled from the cold-boot capture)."""
from concurrent.futures import ThreadPoolExecutor
import struct
import threading
import time

from wifit3.chips.ar9271_v2 import wmi as W
from wifit3.chips.ar9271_v2.transport import AR9271Transport


class FakeDev:
    """Records REG_OUT writes; answers each command with a canned REG_IN response carrying
    the right (command_id, seq) header so _await_response accepts it."""

    def __init__(self):
        self.writes = []
        self._seq = 0

    def write(self, ep, data, timeout=None):
        self.writes.append(bytes(data))
        # Synthesize a matching response: htc hdr(ep1) + wmi hdr(echo cmd_id, seq) + value.
        cmd_id, seq = struct.unpack_from(">HH", data, 8)
        body = struct.pack(">HH", cmd_id, seq) + b"\xde\xad\xbe\xef"
        self._resp = struct.pack(">BBH", 1, 0, len(body)) + b"\x00\x00\x00\x00" + body
        return len(data)

    def read(self, ep, length, timeout=None):
        return bytearray(self._resp)


def _wmi():
    return W.WMI(AR9271Transport(FakeDev()), ctrl_epid=1)


def test_reg_read_bytes():
    w = _wmi()
    val = w.reg_read(0x4020)
    assert w.t.dev.writes[0].hex() == "01000008000000000014000100004020"
    assert val == 0xDEADBEEF


def test_reg_write_single_bytes():
    w = _wmi()
    w.reg_write(0x704C, 3)                   # fresh channel -> seq1 (op26 on the wire is seq2)
    assert w.t.dev.writes[0].hex() == "0100000c00000000001500010000704c00000003"


def test_reg_write_multi_batches_between_enable_and_flush():
    w = _wmi()
    w.reg_read(0x4020)                       # seq1, so the batch lands at seq3 like the wire
    w.reg_write(0x704C, 3)                    # seq2 single
    w.enable_write_buffer()
    w.reg_write(0x704C, 3)
    w.reg_write(0x4000, 1)
    w.reg_write(0x7040, 0)
    assert len(w.t.dev.writes) == 2          # nothing sent yet — still buffered
    w.write_flush()
    assert w.t.dev.writes[2].hex() == (
        "0100001c00000000001500030000704c0000000300004000000000010000704000000000")


def test_reg_rmw_single_bytes():
    w = _wmi()
    w.reg_rmw(0x704C, 1, 0)
    # htc(ep1,len16) + wmi(REG_RMW=0x20, seq1) + reg/set/clr.
    assert w.t.dev.writes[0].hex() == "010000100000000000200001" + "0000704c0000000100000000"


def test_get_fw_version():
    w = _wmi()
    # Override the canned response to a wmi_fw_version (major=1, minor=4).
    dev = w.t.dev

    def write(ep, data, timeout=None):
        cmd_id, seq = struct.unpack_from(">HH", data, 8)
        dev.writes.append(bytes(data))
        body = struct.pack(">HH", cmd_id, seq) + struct.pack(">HH", 1, 4)
        dev._resp = struct.pack(">BBH", 1, 0, len(body)) + b"\x00\x00\x00\x00" + body
        return len(data)

    dev.write = write
    assert w.get_fw_version() == (1, 4)
    # Command is empty (no payload): htc(8) + wmi hdr(4) only.
    assert dev.writes[0].hex() == "0100000400000000" + "00030001"


def test_stale_response_sequence_is_ignored():
    class StaleThenCurrentDev(FakeDev):
        def write(self, ep, data, timeout=None):
            result = super().write(ep, data, timeout)
            cmd_id, seq = struct.unpack_from(">HH", data, 8)
            stale_body = struct.pack(">HHI", cmd_id, seq - 1, 0xBAD0BAD0)
            current_body = struct.pack(">HHI", cmd_id, seq, 0xDEADBEEF)
            self._responses = [
                struct.pack(">BBH4x", 1, 0, len(stale_body)) + stale_body,
                struct.pack(">BBH4x", 1, 0, len(current_body)) + current_body,
            ]
            return result

        def read(self, ep, length, timeout=None):
            return bytearray(self._responses.pop(0))

    w = W.WMI(AR9271Transport(StaleThenCurrentDev()), ctrl_epid=1)
    assert w.reg_read(0x4020) == 0xDEADBEEF


def test_seq_increments_per_command():
    w = _wmi()
    for _ in range(5):
        w.reg_read(0x4020)
    seqs = [struct.unpack_from(">H", wr, 10)[0] for wr in w.t.dev.writes]
    assert seqs == [1, 2, 3, 4, 5]


def test_concurrent_commands_serialize_request_response_pairs():
    class OverlapDetectingDev(FakeDev):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def write(self, ep, data, timeout=None):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            result = super().write(ep, data, timeout)
            time.sleep(0.005)
            return result

        def read(self, ep, length, timeout=None):
            result = super().read(ep, length, timeout)
            with self.lock:
                self.active -= 1
            return result

    dev = OverlapDetectingDev()
    w = W.WMI(AR9271Transport(dev), ctrl_epid=1)
    with ThreadPoolExecutor(max_workers=16) as executor:
        values = list(executor.map(lambda _: w.reg_read(0x4020), range(32)))

    assert values == [0xDEADBEEF] * 32
    assert dev.max_active == 1
    seqs = [struct.unpack_from(">H", wr, 10)[0] for wr in dev.writes]
    assert seqs == list(range(1, 33))
