"""Throwaway: when does RX actually start in the capture, and what host op gates it?

Works backwards from the first good 802.11 MPDU on bulk-IN (ep 0x84). Builds a
frame-ordered timeline of: bulk-IN URB submits/completions, the first data-bearing
completion, the first GOOD MPDU, and the monitor-enable host writes (RCR 0x608,
RXFLTMAP 0x6a0/0x6a2/0x6a4, MSR 0x102) so we can see whether RX onset lines up with
monitor entry or with some later host op the running driver may be missing.

    uv run python scripts/rtl8821cu_dkms/rx_onset.py [capture-1.pcap]
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8821cu_dkms.rx import query_rx_desc  # noqa: E402

DEFAULT_CAP = REPO / "usb_dumps_new2" / "captures_rtl8821cu" / "capture-1.pcap"

# Monitor-enable + RX-path host writes we want to time-anchor.
_ANCHORS = {0x0608: "RCR", 0x0102: "MSR", 0x06A0: "RXFLTMAP_mgmt",
            0x06A2: "RXFLTMAP_ctrl", 0x06A4: "RXFLTMAP_data",
            0x0100: "REG_CR", 0x0808: "0x808(CCK_EN)", 0x004E: "0x4e(LED/ant)"}


def _hex(s: str) -> bytes:
    return bytes.fromhex(s.replace(":", "").strip()) if s.strip() else b""


def _bulk_in_events(pcap: Path, dev: int):
    """(frame, urb_type, datalen, n_good, n_c2h) for every ep-0x84 packet."""
    fields = ["frame.number", "usb.urb_type", "usb.capdata"]
    flt = f"usb.device_address=={dev} && usb.endpoint_address==0x84"
    cmd = ["tshark", "-r", str(pcap), "-Y", flt, "-T", "fields"]
    for f in fields:
        cmd += ["-e", f]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    ev = []
    for line in out.splitlines():
        c = (line.split("\t") + ["", "", ""])[:3]
        frame, utype, cap = c
        buf = _hex(cap)
        good = c2h = 0
        off, n = 0, len(buf)
        while off + 24 <= n:
            d = query_rx_desc(buf[off:off + 24])
            if d.pkt_len <= 0:
                break
            pkt_off = 24 + d.drvinfo_sz + d.shift_sz + d.pkt_len
            if off + pkt_off > n:
                break
            if d.rpt_sel:
                c2h += 1
            elif not (d.crc_err or d.icv_err):
                good += 1
            off += (pkt_off + 7) & ~7
        ev.append((int(frame), utype.strip("'"), len(buf), good, c2h))
    return ev


def main() -> int:
    pcap = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CAP
    if not pcap.exists():
        print(f"no such capture {pcap}")
        return 1
    dev = rp.find_card_device(pcap)
    print(f"{pcap.name}: card=dev{dev}\n")

    ev = _bulk_in_events(pcap, dev)
    submits = [e for e in ev if e[1] == "S"]
    comps = [e for e in ev if e[1] == "C"]
    with_data = [e for e in comps if e[2] > 0]
    first_good = next((e for e in comps if e[3] > 0), None)
    first_c2h = next((e for e in comps if e[4] > 0), None)

    print(f"ep0x84: {len(submits)} URB submits, {len(comps)} completions, "
          f"{len(with_data)} completions carried data")
    if submits:
        print(f"  first URB submit          @ frame {submits[0][0]}")
        print(f"  first completion          @ frame {comps[0][0]}")
    if with_data:
        print(f"  first data-bearing comp   @ frame {with_data[0][0]} ({with_data[0][2]}B)")
    if first_c2h:
        print(f"  first C2H report          @ frame {first_c2h[0]}")
    if first_good:
        print(f"  first GOOD 802.11 MPDU     @ frame {first_good[0]} "
              f"({first_good[2]}B, {first_good[3]} good in buffer)")

    # Anchor: the host->chip writes to the monitor/RX registers, with frame numbers.
    ctrl = rp.extract_ctrl_ops(pcap, dev)
    print("\nhost->chip writes to monitor/RX-path registers (frame : reg = value):")
    for o in ctrl:
        if o["dir"] == "OUT" and o["wval"] in _ANCHORS:
            v = int.from_bytes(o["data"], "little") if o["data"] else 0
            tag = ""
            if first_good and o["frame"] > first_good[0]:
                tag = "  <-- AFTER first good MPDU"
            print(f"  f{o['frame']:>7} : {_ANCHORS[o['wval']]:<16} "
                  f"0x{o['wval']:04x}/{o['width']} = 0x{v:0{o['width']*2}x}{tag}")

    # Timeline: good-MPDU count bucketed by 1000-frame windows, to see RX onset shape.
    if first_good:
        buckets: dict[int, int] = {}
        for f, ut, _ln, good, _c2h in comps:
            if good:
                buckets[f // 1000] = buckets.get(f // 1000, 0) + good
        print("\ngood-MPDU count per 1000-frame window (onset shape):")
        for b in sorted(buckets):
            print(f"  frames {b*1000:>7}-{b*1000+999:<7}: {buckets[b]:>5} good MPDUs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
