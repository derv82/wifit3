"""
verify_pcap for MT7921AU — the faithfulness gate for the cold-init register sequence.

Single cursor walks the captured control-transfer stream (reads + writes, with
VALUES — parsed straight from the pcapng/usbmon bytes, since tshark's usb.capdata
doesn't surface control data). It runs the driver's REAL `_dma_init` against a mock
transport that serves the captured reads and asserts every write the driver emits
matches the kernel's wire, in order. PASS ⇔ zero divergences.

This is the gate that would have caught a wrong magic number (e.g. a transposed
Q_MAP or SCHED_SET constant) instantly — the register init was value-verifiable all
along; we just hadn't built the check.

Scope: the WFDMA cold-init window (prefetch → GLO_CFG → DMASHDL → WLCFG →
rx_evt_ep4 → epctl_rst_opt). MCU/FW-data bulk path is bulk-diffed + sha256 elsewhere;
extend here when a Kali full-payload re-capture lands.

Usage: uv run python scripts/mt7921au/verify_pcap.py [<pcap>]
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import wifit3.chips.mt7921au as mt_pkg
from wifit3.chips.mt7921au.firmware import MT7921AUFirmwareLoader

DEFAULT_CAP = "usb_dumps_new/captures_mt7921u_pau0f-no-adapter/capture-3.pcap"
DEV = 5                       # mt7921 device address in this capture
PREFETCH0 = 0x7C024600        # first WFDMA-init register touched — start of window
REG_BREQ = {0x63, 0x66, 0x01, 0x02}   # unified rd/wr + UHW rd/wr (register access)


def parse_pcapng(path):
    data = Path(path).read_bytes()
    pkts, off = [], 0
    while off + 12 <= len(data):
        btype, blen = struct.unpack_from("<II", data, off)
        if blen < 12 or off + blen > len(data):
            break
        if btype == 0x00000006:  # Enhanced Packet Block
            cap_len = struct.unpack_from("<I", data, off + 8 + 12)[0]
            pkts.append(data[off + 8 + 20: off + 8 + 20 + cap_len])
        off += blen
    return pkts


def build_op_stream(pkts):
    """Ordered list of ('R'|'W', addr, value) for device-DEV register control ops."""
    ops, pending = [], {}
    for pkt in pkts:
        if len(pkt) < 64:
            continue
        urb_type, xfer, _ep, devnum = pkt[8], pkt[9], pkt[10], pkt[11]
        if devnum != DEV or xfer != 0x02:       # control transfers only
            continue
        urb_id = pkt[0:8]
        if urb_type == 0x53:                      # SUBMIT (has setup)
            bmReq, bReq = pkt[40], pkt[41]
            if bReq not in REG_BREQ:
                continue
            wValue, wIndex, wLength = struct.unpack_from("<HHH", pkt, 42)
            addr = (wValue << 16) | wIndex
            if bmReq & 0x80:                      # IN = read; value arrives on COMPLETE
                pending[urb_id] = addr
            elif wLength >= 4 and len(pkt) >= 68:  # OUT = write; value in data
                ops.append(("W", addr, struct.unpack_from("<I", pkt, 64)[0]))
        elif urb_type == 0x43:                    # COMPLETE
            addr = pending.pop(urb_id, None)
            if addr is not None and len(pkt) >= 68:
                ops.append(("R", addr, struct.unpack_from("<I", pkt, 64)[0]))
    return ops


class Divergence(Exception):
    pass


class ReplayTransport:
    """Serves captured reads; asserts driver writes match the captured stream."""
    def __init__(self, ops):
        self.ops = ops
        self.i = 0
        self.matched = []      # (addr, value) writes confirmed against the wire

    def _next(self):
        if self.i >= len(self.ops):
            raise Divergence(f"driver did an extra op past the captured stream (op #{self.i})")
        op = self.ops[self.i]
        self.i += 1
        return op

    def read_reg32_unified(self, addr):
        kind, a, v = self._next()
        if kind != "R" or a != addr:
            raise Divergence(f"op #{self.i-1}: driver READ 0x{addr:08x}, wire has {kind} 0x{a:08x}")
        return v

    def write_reg32_unified(self, addr, value):
        kind, a, v = self._next()
        if kind != "W" or a != addr:
            raise Divergence(f"op #{self.i-1}: driver WROTE 0x{addr:08x}, wire has {kind} 0x{a:08x}")
        if v != value:
            raise Divergence(f"op #{self.i-1}: VALUE MISMATCH at 0x{addr:08x} — "
                             f"driver 0x{value:08x} vs wire 0x{v:08x}")
        self.matched.append((addr, value))


def main():
    cap = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CAP
    pkts = parse_pcapng(cap)
    if not pkts:
        print(f"[FAIL] no packets parsed from {cap}")
        return 1
    ops = build_op_stream(pkts)
    # window starts at the first op touching the prefetch base register
    start = next((k for k, o in enumerate(ops) if o[1] == PREFETCH0), None)
    if start is None:
        print("[FAIL] WFDMA prefetch register not found in capture")
        return 1
    window = ops[start:]

    loader = MT7921AUFirmwareLoader(ReplayTransport(window), Path(mt_pkg.__file__).parent / "assets")
    print(f"verify_pcap mt7921au — {cap}")
    print(f"  {len(pkts)} packets, {len(ops)} register ops; WFDMA window starts at op {start}")
    try:
        loader._dma_init()
    except Divergence as e:
        print(f"\n[FAIL] DIVERGENCE — {e}")
        print("  (the cursor stops at the first op the driver does not reproduce — that IS the bug)")
        return 1

    t = loader.transport
    print(f"\n[PASS] _dma_init reproduced {len(t.matched)} register writes byte-for-byte "
          f"({t.i} ops consumed). Next captured op: "
          f"{('%s 0x%08x' % (t.ops[t.i][0], t.ops[t.i][1])) if t.i < len(t.ops) else '(end)'}")
    print("  confirmed writes:")
    for addr, val in t.matched:
        print(f"    0x{addr:08x} = 0x{val:08x}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
