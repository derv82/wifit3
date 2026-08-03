"""SEVERE AUDIT: single-cursor, zero-waiver replay of the WHOLE cap-1 driver wire.

Premise (the standard we kept violating): the driver is a deterministic function of its recorded
reads. Given the capture's reads fed back, EVERY op the driver emits must reproduce byte-for-byte;
the only legitimately-unmatched ops are a different program's traffic (aireplay TX). This runs our
real driver phases — cold_bringup -> enable_monitor -> set_channel_bw per captured hop — against ONE
cursor over cap-1's full ctrl+bulk-OUT stream, and classifies every capture op:

  MATCHED   — our driver emitted it byte-for-byte (read answered / write+bulk byte-checked).
  DARK      — a capture op our driver NEVER emits (a step the real driver did and we skip; this is
              the antenna-mux bug class). Surfaced by forward-scan resync: when our emitted op is
              found N ops ahead, those N ops are DARK.
  DIVERGENT — our driver emitted an op whose addr matches but VALUE differs (a write we got wrong),
              or emitted something not found ahead at all (extra/cascaded). Hard bug.

No slicing, no matched-prologue, no skip-lists. Output: the honest coverage number + every DARK span
(addr/frame/value) as the bug worklist. cap-1 only (cap-2/3 are warm — they diff at op 0).

Run: uv run python scripts/chips/rtl8822bu_dkms/verify_full_audit.py
"""
from __future__ import annotations

import re
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import rtw88_pcap_replay as rp
from wifit3.chips.rtl8822bu_dkms import (bringup, chan, chipid, efuse, mac,
                                         txpower, usbphy)
from wifit3.chips.rtl8822bu_dkms.transport import Rtl8822buTransport

CAP_DIR = REPO / "driver_captures" / "captures_rtl88x2bu"
_IW_LINE = re.compile(r"^\[(\d+\.\d+)\] Executing:.*set channel (\d+)")
SCAN_WINDOW = 8000          # max ops to forward-scan for a resync before declaring DIVERGENT


class AuditDevice:
    """Like ReplayDevice but resyncs on a miss (logging the skipped span as DARK) instead of
    raising on the first mismatch, so one pass enumerates EVERY gap. A write whose addr matches
    at the resync point but value differs is logged DIVERGENT (a wrong write), not skipped."""

    def __init__(self, ops):
        self.ops = ops
        self.i = 0
        self.matched = 0
        self.dark: list[dict] = []        # each capture op we skipped
        self.divergent: list[str] = []    # wrong-value writes / unfound emissions
        self.phase = "?"

    def _sig(self, op):
        if op.get("dir") == "BULK":
            return ("BULK",)
        return (op["dir"], op["breq"], op["wval"], op["widx"])

    def _scan(self, want_sig):
        for j in range(self.i, min(len(self.ops), self.i + SCAN_WINDOW)):
            if self._sig(self.ops[j]) == want_sig:
                return j
        return None

    def _consume_dark_until(self, j):
        for k in range(self.i, j):
            o = self.ops[k]
            self.dark.append({"phase": self.phase, "idx": k, "dir": o.get("dir"),
                              "wval": o.get("wval"), "frame": o.get("frame"),
                              "data": o.get("data", b"")})
        self.i = j

    def write(self, endpoint, data, timeout=None):
        j = self._scan(("BULK",))
        if j is None:
            self.divergent.append(f"[{self.phase}] bulk[{len(data)}B] not found ahead "
                                  f"(cursor op#{self.i})")
            return len(bytes(data))
        if j > self.i:
            self._consume_dark_until(j)
        op = self.ops[self.i]
        self.i += 1
        self.matched += 1
        if op["data"] != bytes(data):
            self.divergent.append(f"[{self.phase}] op#{self.i-1} bulk payload differs "
                                  f"@f{op['frame']}")
        return len(bytes(data))

    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex, data_or_wLength, timeout=None):
        is_in = bool(bmRequestType & 0x80)
        want = ("IN" if is_in else "OUT", bRequest, wValue, wIndex)
        j = self._scan(want)
        if j is None:
            self.divergent.append(f"[{self.phase}] emitted {'IN' if is_in else 'OUT'} "
                                  f"0x{wValue:04x} not found ahead (cursor op#{self.i})")
            return b"\x00" * (data_or_wLength if is_in else 0) if is_in else 0
        if j > self.i:
            self._consume_dark_until(j)
        op = self.ops[self.i]
        self.i += 1
        self.matched += 1
        if is_in:
            return op["data"]
        payload = bytes(data_or_wLength) if data_or_wLength else b""
        if op["data"] != payload:
            self.divergent.append(
                f"[{self.phase}] op#{self.i-1} write 0x{wValue:04x} VALUE differs: "
                f"port {payload.hex() or '-'} != cap {op['data'].hex() or '-'} @f{op['frame']}")
        return len(payload)


def _parse_iw(iw_log):
    cmds = []
    with open(iw_log) as f:
        for line in f:
            m = _IW_LINE.match(line)
            if m:
                cmds.append(int(m.group(2)))
    return cmds


def main() -> int:
    time.sleep = lambda *a, **k: None
    pcap = CAP_DIR / "capture-1.pcap"
    dev = rp.find_card_device(pcap)
    ctrl = rp.extract_ctrl_ops(pcap, dev)
    bulk = rp.extract_bulk_out_ops(pcap, dev)
    ops = rp.merge_ops_by_frame(ctrl, bulk)
    print(f"capture-1: dev{dev}, {len(ops)} ctrl+bulk-OUT ops total")

    # PG TX-power decode (EFUSE prefix on a throwaway strict cursor) so hops can write TXAGC.
    ct = Rtl8822buTransport(rp.ReplayDevice(list(ops)))
    info = chipid.get_chip_info(ct)
    usbphy.phy_cfg_usb(ct, info.chip_ver)
    chipid.read_chip_version(ct)
    pg = txpower.parse_pg(efuse.read_efuse(ct).log_map)

    a = AuditDevice(ops)
    t = Rtl8822buTransport(a)

    a.phase = "cold_bringup"
    bringup.cold_bringup(t)
    a.phase = "enable_monitor"
    mac.enable_monitor(t)
    # Reproduce the captured hop sequence (iw.log explicit hops). The initial tune is a band change
    # from the cold-init 5 GHz default; thread prev_ch so switch_band fires exactly as on the wire.
    a.phase = "hops"
    prev = None
    for ch in _parse_iw(CAP_DIR / "capture-1_logs" / "iw.log"):
        crossing = prev is not None and (prev <= 14) != (ch <= 14)
        chan.set_channel_bw(t, ch, prev_ch=prev, txpwr_pg=(None if crossing else pg))
        prev = ch

    print("\n=== AUDIT (cap-1, cold -> hops) ===")
    print(f"  cursor reached op#{a.i} of {len(ops)}")
    print(f"  MATCHED  : {a.matched}")
    print(f"  DARK     : {len(a.dark)} ops our driver never emits")
    print(f"  DIVERGENT: {len(a.divergent)}")
    # DARK ops grouped into contiguous spans, summarised by phase + register histogram.
    spans: list[list[dict]] = []
    for d in a.dark:
        if spans and d["idx"] == spans[-1][-1]["idx"] + 1:
            spans[-1].append(d)
        else:
            spans.append([d])
    print(f"\n  DARK spans ({len(spans)} contiguous; the bug worklist):")
    for span in spans[:40]:
        s, e = span[0], span[-1]
        regs = Counter(f"0x{o['wval']:04x}" for o in span if o["dir"] != "BULK")
        top = " ".join(f"{r}x{n}" for r, n in regs.most_common(6))
        print(f"    [{s['phase']}] op#{s['idx']}-{e['idx']} (f{s['frame']}-{e['frame']}, "
              f"{len(span)} ops): {top}")
    if len(spans) > 40:
        print(f"    ... +{len(spans)-40} more spans")
    if a.divergent:
        print("\n  DIVERGENT (wrong-value writes / unfound emissions — investigate first):")
        for d in a.divergent[:25]:
            print(f"    {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
