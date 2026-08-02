"""pair_mcu_io.py -- pair each MCU OUT command with its (expected) IN
response and report any anomalies. Reads a pcap, walks OUT and IN
frames in time order, matches by sequence number, prints OK/MISSING/STALE.

Usage:
    uv run python scripts/mt76x0u/pair_mcu_io.py \\
        --pcap driver_captures/captures_mt76x0u/capture-2.pcap \\
        --device 14 --frames 4913-5742 --label KERNEL

Output marks any of:
  OK         OUT seq matched its IN response (sub-second latency)
  NO_RESP    OUT had no IN response in the window (= chip silence)
  STALE      IN response came that didn't match any pending OUT seq
  ECHO       OUT with seq=0 (no-wait intermediate chunk), expected no resp
"""
from __future__ import annotations

import argparse
import struct
import subprocess


CMD_NAMES = {
    1: "FUN_SET_OP", 8: "BURST_WRITE", 10: "RANDOM_READ",
    12: "RANDOM_WRITE", 13: "RANDOM_WRITE_alt", 31: "CALIBRATION_OP",
}


def extract_frames(pcap, device, fs, fe, ep):
    cmd = ["tshark", "-r", pcap,
           "-Y", (f"usb.device_address == {device} "
                  f"and usb.endpoint_address == 0x{ep:02x} "
                  f"and usb.capdata "
                  f"and frame.number >= {fs} and frame.number <= {fe}"),
           "-T", "fields",
           "-e", "frame.number", "-e", "frame.time_relative", "-e", "usb.capdata"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    frames = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        try:
            raw = bytes.fromhex(parts[2].replace(":", ""))
        except ValueError:
            continue
        if len(raw) < 4:
            continue
        frames.append((int(parts[0]), float(parts[1]), raw))
    return frames


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pcap", required=True)
    p.add_argument("--device", type=int, required=True)
    p.add_argument("--frames", required=True)
    p.add_argument("--label", default="stream")
    args = p.parse_args()

    fs, fe = (int(x) for x in args.frames.split("-"))

    outs = extract_frames(args.pcap, args.device, fs, fe, 0x08)
    ins = extract_frames(args.pcap, args.device, fs, fe, 0x85)

    print(f"\n=== {args.label} ({len(outs)} OUT, {len(ins)} IN) ===\n")

    # Build OUT seq sequence with metadata
    out_records = []
    for fn, t, raw in outs:
        info = struct.unpack_from("<I", raw, 0)[0]
        cmd_t = (info >> 20) & 0x7F
        seq   = (info >> 16) & 0xF
        out_records.append({"frame": fn, "t": t, "cmd": cmd_t, "seq": seq})

    # Build IN seq sequence with metadata
    in_records = []
    for fn, t, raw in ins:
        rxfce = struct.unpack_from("<I", raw, 0)[0]
        seq = (rxfce >> 16) & 0xF
        evt = (rxfce >> 20) & 0xF
        in_records.append({"frame": fn, "t": t, "seq": seq, "evt": evt,
                           "matched": False})

    # Walk OUT in order; for each OUT with seq != 0, find the next IN
    # (by time) that has matching seq and isn't already matched.
    n_ok = 0
    n_noresp = 0
    n_echo = 0
    anomalies = []
    for o in out_records:
        cmd_name = CMD_NAMES.get(o["cmd"], f"CMD_{o['cmd']:#x}")
        if o["seq"] == 0:
            # no-wait intermediate chunk
            n_echo += 1
            continue
        # Find next IN with matching seq, time > o['t'], not matched
        match = None
        for i in in_records:
            if i["matched"]:
                continue
            if i["t"] < o["t"]:
                continue
            if i["seq"] == o["seq"]:
                match = i
                break
        if match is None:
            anomalies.append(f"NO_RESP   OUT#{o['frame']} T+{o['t']:.3f}s "
                             f"{cmd_name:<18s} seq={o['seq']}  "
                             f"-- no matching IN response found in window")
            n_noresp += 1
        else:
            match["matched"] = True
            n_ok += 1

    # Any IN that wasn't matched to an OUT?
    n_stale = 0
    for i in in_records:
        if not i["matched"]:
            anomalies.append(f"STALE     IN#{i['frame']} T+{i['t']:.3f}s "
                             f"seq={i['seq']}  -- no preceding OUT with this seq")
            n_stale += 1

    print(f"OUT total      : {len(outs)}")
    print(f"  with seq=0 (no-wait intermediate): {n_echo}")
    print(f"  expecting response               : {len(outs) - n_echo}")
    print(f"IN total       : {len(ins)}")
    print(f"  matched OK                       : {n_ok}")
    print(f"  STALE (no preceding OUT)         : {n_stale}")
    print(f"NO_RESP (OUT with no IN)           : {n_noresp}")
    print()
    if anomalies:
        print("Anomalies:")
        for a in anomalies:
            print(f"  {a}")
    else:
        print("No anomalies. Every wait-expecting OUT matched an IN response.")


if __name__ == "__main__":
    main()
