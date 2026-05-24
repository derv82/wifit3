"""Read a frag_probe pcap and answer the only questions that matter:

  1. Did our injected fragments actually go on the air (does the card loop our
     own TX back into RX)? — find ToDS+Protected frames sourced from our STA,
     show their fragment numbers / IVs / lengths.
  2. Did the AP RELAY a reassembled frame from us? — any FromDS frame whose
     source address is our STA, or any *new* broadcast WEP data frame on the
     target BSSID clustered right after our bursts.
  3. What's just ambient channel-6 traffic (to filter the probe's noisy
     fresh-IV flag)?

This is the ground-truth read that the oracle gets coded against. Pure stdlib,
self-contained (same throwaway-independence rationale as the probe).

    uv run python scripts/wep/analyze_frag_pcap.py wifit3-wep-frag.pcap \
        --our-mac 02:5d:69:4f:eb:9a
"""
from __future__ import annotations

import argparse
import struct
import sys
from collections import Counter
from pathlib import Path


def _mac(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


def read_pcap(path: Path):
    """Yield (ts, frame_bytes) from a libpcap file (linktype assumed 105)."""
    data = path.read_bytes()
    magic = struct.unpack("<I", data[:4])[0]
    if magic != 0xA1B2C3D4:
        raise SystemExit(f"not a little-endian pcap (magic {magic:#x})")
    off = 24
    out = []
    while off + 16 <= len(data):
        sec, usec, caplen, _orig = struct.unpack("<IIII", data[off:off + 16])
        off += 16
        frame = data[off:off + caplen]
        off += caplen
        out.append((sec + usec / 1e6, frame))
    return out


def hdr_len(fc0: int, fc1: int) -> int:
    n = 24
    if (fc1 & 0x01) and (fc1 & 0x02):
        n += 6
    if ((fc0 & 0xF0) >> 4) & 0x08:
        n += 2
    if fc1 & 0x80:
        n += 4
    return n


class F:
    """Decoded view of one frame's header."""
    def __init__(self, ts: float, raw: bytes):
        self.ts = ts
        self.raw = raw
        fc0, fc1 = raw[0], raw[1]
        self.ftype = (fc0 >> 2) & 0x03
        self.subtype = (fc0 & 0xF0) >> 4
        self.tods = bool(fc1 & 0x01)
        self.fromds = bool(fc1 & 0x02)
        self.morefrag = bool(fc1 & 0x04)
        self.prot = bool(fc1 & 0x40)
        self.a1 = raw[4:10]
        self.a2 = raw[10:16] if len(raw) >= 16 else b""
        self.a3 = raw[16:22] if len(raw) >= 22 else b""
        self.seqctl = struct.unpack("<H", raw[22:24])[0] if len(raw) >= 24 else 0
        self.fragno = self.seqctl & 0x0F
        self.seqno = self.seqctl >> 4
        body = raw[hdr_len(fc0, fc1):] if len(raw) >= 24 else b""
        self.iv = body[:3] if (self.ftype == 2 and self.prot and len(body) >= 3) else b""

    @property
    def is_data(self) -> bool:
        return self.ftype == 2


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("pcap", type=Path)
    p.add_argument("--our-mac", required=True, help="our forged STA MAC")
    p.add_argument("--bssid", help="target BSSID (else inferred from our TX)")
    args = p.parse_args()

    our = bytes(int(x, 16) for x in args.our_mac.split(":"))
    frames = [F(ts, raw) for ts, raw in read_pcap(args.pcap) if len(raw) >= 24]
    print(f"Read {len(frames)} frames from {args.pcap}\n")

    # 1. Our injected fragments: data, Protected, ToDS, sourced from us (a2).
    ours = [f for f in frames if f.is_data and f.prot and f.tods and f.a2 == our]
    print(f"=== Our TX seen in RX (loopback): {len(ours)} frames ===")
    if ours:
        bssid = args.bssid and bytes(int(x, 16) for x in args.bssid.split(":"))
        bssid = bssid or ours[0].a1
        print(f"  target BSSID (a1 of our frames): {_mac(bssid)}")
        ivs = Counter(f.iv.hex() for f in ours)
        fragnos = Counter(f.fragno for f in ours)
        mf = sum(1 for f in ours if f.morefrag)
        print(f"  IVs used: {dict(ivs)}")
        print(f"  fragment numbers seen: {dict(sorted(fragnos.items()))}")
        print(f"  with More-Fragments bit: {mf}/{len(ours)}")
        print(f"  lengths: {sorted(set(len(f.raw) for f in ours))}")
        print("  first round (frag# : len : iv):")
        for f in ours[:9]:
            print(f"    {f.fragno} : {len(f.raw)} : {f.iv.hex()}")
    else:
        print("  (none — this card does NOT loop injected TX back into RX; we "
              "can only judge the relay from AP-sourced frames.)")
        bssid = args.bssid and bytes(int(x, 16) for x in args.bssid.split(":"))

    # 2. Relay from us: any FromDS frame whose SOURCE (a3 under FromDS) is us.
    print("\n=== Frames the AP sourced FROM us (a3==our MAC, FromDS): relay? ===")
    relay = [f for f in frames if f.is_data and f.fromds and f.a3 == our]
    if relay:
        for f in relay[:20]:
            print(f"  len={len(f.raw)} a1={_mac(f.a1)} a2={_mac(f.a2)} "
                  f"iv={f.iv.hex()} morefrag={f.morefrag}")
        print(f"  → {len(relay)} relayed-from-us frame(s). THIS is the oracle "
              "signal — code fragmentation.py to it.")
    else:
        print("  NONE. The AP did not rebroadcast a frame sourced from our STA.")

    # 3. Per-BSSID census of broadcast WEP data frames (filters ambient noise).
    print("\n=== Broadcast WEP data frames by BSSID (a2 under FromDS) ===")
    bcast = [f for f in frames
             if f.is_data and f.prot and f.a1 == b"\xff" * 6 and f.fromds]
    by_bssid = Counter(_mac(f.a2) for f in bcast)
    for mac, n in by_bssid.most_common():
        tgt = " <- target" if bssid and bytes(int(x, 16) for x in mac.split(":")) == bssid else ""
        print(f"  {mac}: {n}{tgt}")

    # 4. If we have the target BSSID, list every broadcast data frame IT sent
    #    (these are candidate reassembly relays even if SA isn't ours).
    if bssid:
        print(f"\n=== All broadcast WEP data frames from target {_mac(bssid)} ===")
        tgt_bcast = [f for f in bcast if f.a2 == bssid]
        for f in tgt_bcast[:40]:
            print(f"  t={f.ts:.3f} len={len(f.raw)} a3(SA)={_mac(f.a3)} "
                  f"iv={f.iv.hex()}")
        print(f"  ({len(tgt_bcast)} total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
