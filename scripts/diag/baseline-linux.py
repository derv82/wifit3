"""Linux/Kali side of the card health check.

Captures (or reads) a fixed-channel monitor pcap per channel, parses it with
OUR parser, and feeds the shared aggregator — so it groups identically to the
wifit3 side. No tshark: we read the pcap by hand, strip the radiotap header
(RSSI lives there), honor the FCS flag, and hand the 802.11 frame to
``WlanFrameParser`` (the radiotap parse is byte-validated against tshark on a
real tcpdump capture).

Capture mode (Kali, needs root + a monitor interface — ``airmon-ng start <dev>``)::

    sudo python baseline-linux.py --capture --iface wlan0mon --chip rt5370 \
        --channels 1,6,11 --secs 15

Parse mode (read pcaps you already have — one per channel)::

    python baseline-linux.py --chip rt5370 --pcap 1=cap-ch1.pcap 6=cap-ch6.pcap

Writes ``linux-<chip>.json`` and, if ``wifit3-<chip>.json`` exists alongside,
prints the diff.
"""
from __future__ import annotations

import argparse
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "src"))
sys.path.insert(0, str(_HERE))

from driver_health import Health, diff  # noqa: E402

from wifit3.wlan.packet import WlanFrameParser  # noqa: E402

# radiotap main-namespace field (size, align) by present-bit index, up to the
# antenna-signal field (bit 5) — all we need to locate RSSI + the FCS flag.
_RT = {0: (8, 8), 1: (1, 1), 2: (1, 1), 3: (4, 2), 4: (2, 2), 5: (1, 1),
       6: (1, 1), 7: (2, 2), 8: (2, 2), 9: (2, 2), 10: (1, 1), 11: (1, 1),
       12: (1, 1), 13: (1, 1), 14: (2, 2)}


def parse_radiotap(buf: bytes) -> tuple[int, int | None, int | None]:
    """Return (header_len, rssi_dbm_or_None, flags_byte_or_None)."""
    _ver, _pad, rtlen = struct.unpack_from("<BBH", buf, 0)
    off = 4
    while True:  # present words; high bit chains another
        p = struct.unpack_from("<I", buf, off)[0]
        off += 4
        if not (p & 0x80000000):
            break
    present = struct.unpack_from("<I", buf, 4)[0]
    pos, rssi, flags = off, None, None
    for bit in range(15):
        if not (present & (1 << bit)):
            continue
        size, align = _RT[bit]
        if pos % align:
            pos += align - (pos % align)
        if bit == 1:
            flags = buf[pos]
        elif bit == 5:
            rssi = struct.unpack_from("<b", buf, pos)[0]
        pos += size
    return rtlen, rssi, flags


def read_pcap(path: str):
    """Yield (ts_seconds, frame_802_11_bytes, rssi) for every frame in a
    radiotap pcap. Radiotap stripped, FCS removed when the flag says it's there."""
    data = Path(path).read_bytes()
    magic = struct.unpack_from("<I", data, 0)[0]
    nano = magic == 0xA1B23C4D
    off = 24
    while off + 16 <= len(data):
        ts_sec, ts_frac, incl, _orig = struct.unpack_from("<IIII", data, off)
        off += 16
        pkt = data[off:off + incl]
        off += incl
        if len(pkt) < 8:
            continue
        rtlen, rssi, flags = parse_radiotap(pkt)
        body = pkt[rtlen:]
        if flags is not None and (flags & 0x10):  # FCS present at frame end
            body = body[:-4]
        yield ts_sec + ts_frac / (1e9 if nano else 1e6), body, rssi


def feed_pcap(health: Health, path: str, channel: int) -> None:
    n = 0
    for ts, frame, rssi in read_pcap(path):
        parsed = WlanFrameParser.parse_80211_frame(frame, rssi if rssi is not None else 0)
        health.feed(ts, parsed, rssi, channel)
        n += 1
    print(f"  CH{channel:>3}: {n} frames from {Path(path).name}", file=sys.stderr)


def capture(iface: str, channel: int, secs: int) -> str:
    """iw set channel + tcpdump for ``secs`` into a temp pcap; return its path."""
    subprocess.run(["iw", "dev", iface, "set", "channel", str(channel)], check=True)
    out = tempfile.NamedTemporaryFile(suffix=f"-ch{channel}.pcap", delete=False).name
    print(f"  CH{channel:>3}: capturing {secs}s...", file=sys.stderr)
    subprocess.run(
        ["timeout", str(secs), "tcpdump", "-i", iface, "-w", out, "-U"],
        check=False,  # timeout exits non-zero by design
    )
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Linux-side card health baseline.")
    p.add_argument("--chip", required=True, help="Chip slug for the output filename.")
    p.add_argument("--capture", action="store_true", help="Capture live (Kali, root).")
    p.add_argument("--iface", default="wlan0mon", help="Monitor interface (capture mode).")
    p.add_argument("--channels", default="1,6,11", help="Channels (capture mode).")
    p.add_argument("--secs", type=int, default=15, help="Seconds per channel (capture mode).")
    p.add_argument("--pcap", nargs="+", default=[], metavar="CH=FILE",
                   help="Parse mode: one channel=pcap per channel.")
    args = p.parse_args()

    health = Health(args.chip, "linux")
    if args.capture:
        for ch in (int(c) for c in args.channels.split(",") if c.strip()):
            feed_pcap(health, capture(args.iface, ch, args.secs), ch)
    elif args.pcap:
        for spec in args.pcap:
            ch, _, path = spec.partition("=")
            feed_pcap(health, path, int(ch))
    else:
        print("[-] give --capture or --pcap CH=FILE ...", file=sys.stderr)
        return 1

    out = _HERE / f"linux-{args.chip}.json"
    health.to_json(out)
    wifit3 = _HERE / f"wifit3-{args.chip}.json"
    if wifit3.exists():
        diff(wifit3, out)
    else:
        print(f"[*] no {wifit3.name} yet - run baseline-wifit3.py, then "
              f"`python driver_health.py --diff {wifit3.name} {out.name}`", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
