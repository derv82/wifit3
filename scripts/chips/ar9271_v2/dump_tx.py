"""Decode the aireplay-ng TX frames out of an ar9271_v2 cold-boot capture, in layers.

    uv run python scripts/chips/ar9271_v2/dump_tx.py [capture-1|capture-2|capture-3]

Prints every bulk-OUT (EP 0x01) frame so you can see exactly what ``driver.inject_frame`` must
reproduce byte-for-byte:

  * HIF stream header (le16 len, le16 tag 0x697e)
  * htc_frame_hdr (endpoint_id -> 5 = mgmt service, 6 = data service)
  * tx_mgmt_hdr (8 B) / tx_frame_hdr (12 B) -- note the per-frame COOKIE (the TX slot)
  * the 802.11 frame -- note its sequence number (it comes from the caller / the recorded frame,
    NOT from inject_frame)

Pair with ``scripts/porting/pcap_slicer.py <logs>/main.log <pcap>`` to see which phase (aireplay-ng
``--test`` vs deauth ``-0``) each frame belongs to. The verify gate strips the headers with
``tx.dot11_from_bulk`` and hands the 802.11 frame to ``inject_frame``; your job is to rebuild the
full wrapper from it.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts" / "porting"))
sys.path.insert(0, str(REPO / "scripts" / "chips" / "ar9271_v2"))
import ar9271_pcap_replay as rp

CAP_DIR = REPO / "driver_captures" / "captures_ath9k_htc_newddevice"

# 802.11 frame-control byte 0 (subtype<<4 | type<<2 | version)
_FC = {0x40: "ProbeReq", 0x50: "ProbeResp", 0x80: "Beacon", 0xb0: "Auth", 0xc0: "Deauth",
       0x00: "AssocReq", 0x10: "AssocResp", 0xa0: "Disassoc", 0xb4: "RTS", 0xc4: "CTS",
       0xd4: "ACK", 0x88: "QoSData", 0x08: "Data", 0x48: "Null"}


def _mac(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


def decode_tx(data: bytes) -> str:
    hif_len, hif_tag = struct.unpack_from("<HH", data, 0)
    epid, flags, plen = struct.unpack_from(">BBH", data, 4)
    lines = [f"  HIF(len={hif_len} tag=0x{hif_tag:04x})  htc(epid={epid} flags={flags} plen={plen})"]
    off = 12
    if epid == 5:                                   # tx_mgmt_hdr [SRC] htc.h:85
        node, vif, tid, fl, kt, kix, cookie, pad = struct.unpack_from(">8B", data, off)
        lines.append(f"  tx_mgmt_hdr  node={node} vif={vif} tid={tid} flags=0x{fl:02x} "
                     f"key_type={kt} keyix=0x{kix:02x} COOKIE={cookie} pad={pad}")
        off += 8
    else:                                           # tx_frame_hdr [SRC] htc.h:73
        dtype, node, vif, tid = struct.unpack_from(">4B", data, off)
        fl32 = struct.unpack_from(">I", data, off + 4)[0]
        kt, kix, cookie, pad = struct.unpack_from(">4B", data, off + 8)
        lines.append(f"  tx_frame_hdr data_type={dtype} node={node} vif={vif} tid={tid} "
                     f"flags=0x{fl32:08x} key_type={kt} keyix=0x{kix:02x} COOKIE={cookie} pad={pad}")
        off += 12
    d = data[off:]
    if len(d) >= 24:
        fc = struct.unpack_from("<H", d, 0)[0]
        sub = _FC.get(fc & 0xff, f"0x{fc & 0xff:02x}")
        seqctl = struct.unpack_from("<H", d, 22)[0]
        lines.append(f"  802.11({len(d)}B) {sub} fc=0x{fc:04x} a1={_mac(d[4:10])} "
                     f"a2={_mac(d[10:16])} a3={_mac(d[16:22])} seq={seqctl >> 4} frag={seqctl & 0xf}")
        lines.append(f"      body: {d[24:].hex()}")
    else:
        lines.append(f"  802.11({len(d)}B) {d.hex()}")
    return "\n".join(lines)


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "capture-1"
    pcap = CAP_DIR / f"{name}.pcap"
    pkts = rp.parse_pcapng(str(pcap))
    dev = rp.detect_card(pkts)
    ops = rp.extract(pkts, dev)["host_ops"]
    n = 0
    for i, op in enumerate(ops):
        if op.get("ep") != 0x01:
            continue
        n += 1
        data = bytes(op.get("data") or b"")
        print(f"#op {i}  @frame {op.get('frame')}  ({len(data)}B)")
        print(decode_tx(data))
        print()
    print(f"{n} TX (bulk-OUT) frames in {pcap.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
