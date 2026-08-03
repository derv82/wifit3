"""RTL8822BU — offline CCK capture-reference: what CCK beacon rate did the VENDOR driver
actually achieve, decoded from the cold-boot capture's bulk-IN RX stream.

The decisive accuracy check for the 2.4 GHz CCK bug: capture-1.pcap has a 15 s FIXED-CH1
phase (main.log: [FIXED-CH1] start -w (15s)) where the vendor driver monitored ch1 and delivered
bulk-IN RX. We replay those bulk-IN buffers through the SAME rx_pkt_desc walk our live diagnostic
uses (cck_diag.Tally) and report the vendor's per-AP CCK beacon capture% over that window. If the
vendor also got ~55% on the same APs, our port matches and CCK starvation is hardware/monitor
reality; if the vendor got ~95%, we have a real port gap.

No hardware. tshark pulls the bulk-IN completion payloads (usb.capdata, endpoint 0x84) inside the
window; each is an aggregated [rxdesc|drvinfo|mpdu] buffer the Tally walk already understands.

Usage:
    uv run python scripts/chips/rtl8822bu_dkms/cck_capref.py            # default: cap-1 FIXED-CH1 window
    uv run python scripts/chips/rtl8822bu_dkms/cck_capref.py --pcap driver_captures/.../capture-1.pcap \
        --start 1780355738.466 --end 1780355753.704
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cck_diag import Tally   # noqa: E402  (same-dir diagnostic; reuse the rx_pkt_desc walk)

TSHARK = r"C:/Program Files/Wireshark/tshark.exe"
DEFAULT_PCAP = ("driver_captures/captures_rtl88x2bu/capture-1.pcap")
# main.log: [FIXED-CH1] start 1780355738.466 .. stop 1780355753.704 (15.2 s, fixed channel 1).
FIXED_CH1_START, FIXED_CH1_END = 1780355738.466, 1780355753.704


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pcap", default=DEFAULT_PCAP)
    ap.add_argument("--start", type=float, default=FIXED_CH1_START)
    ap.add_argument("--end", type=float, default=FIXED_CH1_END)
    ap.add_argument("--tshark", default=TSHARK)
    args = ap.parse_args()

    flt = (f"usb.transfer_type==0x03 && usb.endpoint_address==0x84 && usb.capdata && "
           f"frame.time_epoch>={args.start} && frame.time_epoch<={args.end}")
    out = subprocess.run(
        [args.tshark, "-r", args.pcap, "-Y", flt, "-T", "fields",
         "-e", "frame.time_epoch", "-e", "usb.capdata"],
        capture_output=True, text=True, check=True).stdout

    dwell = args.end - args.start
    tally = Tally()
    t0 = None
    for line in out.splitlines():
        p = line.rstrip("\n").split("\t")
        if len(p) < 2 or not p[1]:
            continue
        ep = float(p[0])
        if t0 is None:
            t0 = ep
        buf = bytes.fromhex(p[1].replace(":", ""))
        tally.walk(buf, ep)

    print(f"[CAP-REF] vendor bulk-IN RX, {args.pcap}")
    print(f"          window {args.start:.3f}..{args.end:.3f} ({dwell:.1f}s, FIXED-CH1)")
    tally.report(1, dwell)
    return 0


if __name__ == "__main__":
    sys.exit(main())
