from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import rtw88_pcap_replay as rp
from wifit3.chips.rtl8192fu.tx import TX_DESC_SIZE, build_tx_packet

CAP_DIR = REPO / "src/wifit3/scripts/captures_rtl8192fu"


def deauth_window(pcap: Path) -> tuple[float, float]:
    log = pcap.with_name(f"{pcap.stem}_logs") / "aireplay-ng.log"
    match = re.search(
        r"\[([0-9.]+)\] Executing: [^\n]* aireplay-ng -0 1 [^\n]*\n.*?"
        r"\[([0-9.]+)\] Execution completed",
        log.read_text(errors="replace"),
        re.DOTALL,
    )
    if not match:
        raise ValueError("capture has no completed aireplay directed-deauth segment")
    return float(match.group(1)), float(match.group(2))


def run(cap: str | None = None) -> int:
    name = Path(cap or "capture-2").stem
    pcap = CAP_DIR / f"{name}.pcap"
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1

    try:
        start, end = deauth_window(pcap)
    except ValueError as error:
        print(f"FAIL: {error}")
        return 1

    frame_numbers, epochs = rp.frame_epochs(pcap)
    frame_epoch = dict(zip(frame_numbers, epochs, strict=True))
    device = rp.find_card_device(pcap)
    packets = [
        operation["data"]
        for operation in rp.extract_bulk_out_ops(pcap, device)
        if start <= frame_epoch[operation["frame"]] <= end
        and len(operation["data"]) >= TX_DESC_SIZE + 24
    ]
    if not packets:
        print("FAIL: directed-deauth segment has no TX bulk-OUT payloads")
        return 1

    for index, packet in enumerate(packets):
        expected = build_tx_packet(packet[TX_DESC_SIZE:])
        if len(packet) != len(expected):
            print(
                f"FAIL: TX packet {index} length differs: "
                f"capture={len(packet)} port={len(expected)}"
            )
            return 1
        if packet != expected:
            difference = next(
                offset for offset, (actual, wanted) in enumerate(zip(packet, expected))
                if actual != wanted
            )
            print(f"FAIL: TX packet {index} differs at byte {difference}")
            return 1

    print(f"PASS: {len(packets)}/{len(packets)} directed-deauth TX payloads byte-match")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1] if len(sys.argv) > 1 else None))
