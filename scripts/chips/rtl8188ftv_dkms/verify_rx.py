"""Check the driver's RX path on a vendor capture's bulk-IN stream against tshark.

    uv run python scripts/chips/rtl8188ftv_dkms/verify_rx.py <capture.pcap>

Each recorded bulk-IN completion goes through the driver's RX decode (``rx.iter_rx`` +
``WlanFrameParser``, as in ``_rx_dispatch``). Two independent checks:
  * frame slicing: each frame's descriptor-length slice (FCS included) is recomputed from the
    aggregate; one ending in a valid FCS was cut at exactly the right offset and length (frames
    without one are counted as unchecked). Every delivered frame must be that slice minus 4 B.
  * decode: every parsed packet's type/subtype, BSSID, SA, DA and beacon/probe-response SSID
    must equal what tshark's 802.11 dissector reports for the same bytes.
Not checked: RSSI (no reference value in a USB capture), frames the parser returns None for.
"""
from __future__ import annotations

import argparse
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import rtw88_pcap_replay as rp
from wifit3.chips.rtl8188ftv_dkms import rx
from wifit3.dot11.packet import BeaconPacket
from wifit3.dot11.parser import WlanFrameParser

LINKTYPE_IEEE802_11 = 105
FCS_LEN = 4
SHOWN_MISMATCHES = 10
TSHARK_FIELDS = ["wlan.fc.type", "wlan.fc.subtype", "wlan.bssid", "wlan.sa", "wlan.da",
                 "wlan.ssid"]


def _has_valid_fcs(frame: bytes) -> bool:
    if len(frame) <= FCS_LEN:
        return False
    return zlib.crc32(frame[:-FCS_LEN]) == int.from_bytes(frame[-FCS_LEN:], "little")


def _write_80211_pcap(path: Path, frames: list[bytes]) -> None:
    with path.open("wb") as f:
        f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, LINKTYPE_IEEE802_11))
        for frame in frames:
            f.write(struct.pack("<IIII", 0, 0, len(frame), len(frame)))
            f.write(frame)


def _tshark_rows(frames: list[bytes]) -> list[list[str]]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "frames.pcap"
        _write_80211_pcap(path, frames)
        cmd = ["tshark", "-r", str(path), "-T", "fields", "-E", "occurrence=f"]
        for field in TSHARK_FIELDS:
            cmd += ["-e", field]
        out = subprocess.run(cmd, capture_output=True, text=True, check=True,
                             encoding="utf-8", errors="replace").stdout
    rows = [(line.split("\t") + [""] * len(TSHARK_FIELDS))[:len(TSHARK_FIELDS)]
            for line in out.splitlines()]
    if len(rows) != len(frames):
        raise SystemExit(f"tshark returned {len(rows)} rows for {len(frames)} frames")
    return rows


def _mismatches(packet, row: list[str]) -> list[str]:
    ftype, subtype, bssid, sa, da, ssid = row
    found = []
    if str(packet.type_id) != ftype or str(packet.subtype_id) != subtype:
        found.append(f"type {packet.type_id}/{packet.subtype_id} vs tshark {ftype}/{subtype}")
    for name, ours, theirs in (("bssid", packet.bssid, bssid), ("sa", packet.source, sa),
                               ("da", packet.dest, da)):
        if theirs and ours != theirs:
            found.append(f"{name} {ours} vs tshark {theirs}")
    if isinstance(packet, BeaconPacket) and packet.ssid != _tshark_ssid(ssid):
        found.append(f"ssid {packet.ssid!r} vs tshark {ssid!r}")
    return found


def _tshark_ssid(field: str) -> str | None:
    """tshark's SSID field (hex bytes; ``<MISSING>`` for a zero-length IE) in the parser's
    text form: ``<hidden>`` when empty, UTF-8 with undecodable bytes dropped."""
    if not field:
        return None
    if field == "<MISSING>":
        return "<hidden>"
    return bytes.fromhex(field.replace(":", "")).decode("utf-8", errors="ignore")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Check rtl8188ftv_dkms RX decode on a capture's bulk-IN stream.")
    parser.add_argument("pcap", type=Path, help="vendor-driver USB capture")
    args = parser.parse_args(argv)
    if not args.pcap.exists():
        print(f"FAIL: no such capture {args.pcap}")
        return 1
    buffers = rp.extract_bulk_in_ops(args.pcap, rp.find_card_device(args.pcap))
    frames: list[bytes] = []
    fcs_ok: list[bool] = []
    n_c2h = n_not_stripped = 0
    for buf in buffers:
        off = 0
        for attrib, payload in rx.iter_rx(buf):
            start = off + rx.RXDESC_SIZE + attrib["drvinfo_sz"] + attrib["shift_sz"]
            pre_strip = bytes(buf[start:start + attrib["pkt_len"]])
            off = (start + attrib["pkt_len"] + 7) & ~7
            if attrib["c2h"]:
                n_c2h += 1
                continue
            if payload != pre_strip[:-FCS_LEN]:
                n_not_stripped += 1
            frames.append(payload)
            fcs_ok.append(_has_valid_fcs(pre_strip))
    if not frames:
        print(f"FAIL: no 802.11 frames in {len(buffers)} bulk-IN completions; RX unverified")
        return 1

    rows = _tshark_rows(frames)
    problems = []
    n_parsed = n_unparsed_mgmt_data = 0
    for frame, row in zip(frames, rows):
        try:
            packet = WlanFrameParser.parse_80211_frame(frame, 0)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{frame[:2].hex()} {len(frame)}B: parser raised {e!r}")
            continue
        if packet is None:
            if row[0] in ("0", "2"):
                n_unparsed_mgmt_data += 1
            continue
        n_parsed += 1
        for m in _mismatches(packet, row):
            problems.append(f"{frame[:2].hex()} {len(frame)}B: {m}")

    print(f"rx: {len(buffers)} bulk-IN completions -> {len(frames)} frames + {n_c2h} C2H")
    print(f"  slicing: {sum(fcs_ok)} frames end in a valid FCS; "
          f"{len(frames) - sum(fcs_ok)} unchecked (no valid FCS); "
          f"{n_not_stripped} delivered frames are not the slice minus the FCS")
    print(f"  decode: {n_parsed} parsed packets compared with tshark, {len(problems)} problems; "
          f"unchecked: {n_unparsed_mgmt_data} mgmt/data frames the parser returned None for, "
          f"{len(frames) - n_parsed - n_unparsed_mgmt_data} control/other")
    for p in problems[:SHOWN_MISMATCHES]:
        print(f"    {p}")
    if len(problems) > SHOWN_MISMATCHES:
        print(f"    ... {len(problems) - SHOWN_MISMATCHES} more")
    if problems or not n_parsed or n_not_stripped:
        print("FAIL")
        return 1
    print("PASS: every parsed packet agrees with tshark")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
