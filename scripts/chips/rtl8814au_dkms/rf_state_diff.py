"""Live-vs-capture state diff over the RF (radio) registers — the last RX-relevant seam.

The BB/MAC state diff (cck_state_diff.py) covers MMIO registers. RF registers are written
via the per-path LSSI window (0xc90/0xe90/0x1890/0x1a90, value = (rf_addr<<20)|data) and read
back via the MMIO readback window (rf._rf_read). verify_pcap proves our RF *writes* match the
capture, but an RMW that reads a different live RF value (e.g. _copy_rck1, the channel/bw RMW)
is invisible to it. This walks every RF register the capture wrote per path, reads it back off
live silicon after our bring-up, and diffs — a divergent RF gain/LNA/cal register is RX-critical.

    uv run python scripts/chips/rtl8814au_dkms/rf_state_diff.py [capture-N]
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))
sys.path.insert(0, str(REPO / "scripts" / "chips" / "rtl8814au_dkms"))

import libusb_package
import usb.core

import rtw88_pcap_replay as rp
import verify_pcap as vp
import dump_tune_regs as dt

from wifit3.chips.rtl8814au_dkms import constants as C
from wifit3.chips.rtl8814au_dkms.rf import _rf_read
from wifit3.chips.rtl8814au_dkms.transport import Rtl8814auTransport

CAP_DIR = REPO / "driver_captures" / "captures_rtl8814au"
LSSI = {0x0C90: "a", 0x0E90: "b", 0x1890: "c", 0x1A90: "d"}
# RF regs that are per-channel/bw or RFK-dynamic (legitimately differ from a ch1-only snapshot).
_DYN = {0x18}     # CHNLBW (channel + bw) — per-hop; our bring-up is ch1 only


def _capture_rf_finals(name: str) -> dict:
    """{path: {rf_addr: data}} — final LSSI-written value per RF reg, init+airmon window."""
    pcap = CAP_DIR / f"{name}.pcap"
    dev = vp.DEV_ADDR.get(name) or rp.find_card_device(pcap)
    ops = rp.extract_ops(pcap, dev)
    start = next((i for i, o in enumerate(ops)
                  if o["kind"] == "R" and o.get("addr") == C.REG_SYS_CFG1), 0)
    # End BEFORE the operational phase (hops) so the RF state is the ch1 init+airmon state,
    # comparable to our ch1-only bring-up. Operational tail starts ~frame 15247; RF regs are
    # heavily channel-dependent, so including hops compares ch1 (ours) vs the last-hop channel.
    _OP_FRAME = 15247
    end = next((i for i, o in enumerate(ops) if o.get("frame", 0) >= _OP_FRAME), len(ops))
    finals: dict = {"a": {}, "b": {}, "c": {}, "d": {}}
    for o in ops[start:end]:
        if o["kind"] == "W" and o.get("width") == 4 and o.get("addr") in LSSI:
            path = LSSI[o["addr"]]
            v = o["value"]
            rf_addr = (v >> 20) & 0xFF
            data = v & 0xFFFFF
            finals[path][rf_addr] = data
    return finals


def main() -> int:
    name = Path(sys.argv[1] if len(sys.argv) > 1 else "capture-1").stem
    print(f"[*] extracting capture RF (LSSI) finals from {name}...", file=sys.stderr)
    finals = _capture_rf_finals(name)
    for p in "abcd":
        print(f"    path {p}: {len(finals[p])} RF registers written", file=sys.stderr)

    dev = usb.core.find(idVendor=C.VID_REALTEK, idProduct=C.PID_RTL8814AU,
                        backend=libusb_package.get_libusb1_backend())
    if dev is None:
        print("[-] RTL8814AU not found.", file=sys.stderr)
        return 1
    t = Rtl8814auTransport(dev)
    print("[*] bringing up live...", file=sys.stderr)
    dt._bring_up(t, 1)

    print("\n[*] live-vs-capture RF register diff (per path, excluding per-channel 0x18):")
    diffs = checked = 0
    for path in "abcd":
        for addr in sorted(finals[path]):
            if addr in _DYN:
                continue
            checked += 1
            exp = finals[path][addr]
            live = _rf_read(t, path, addr)
            if live != exp:
                diffs += 1
                print(f"  DIVERGE RF-{path} 0x{addr:02x}: live=0x{live:05x}  capture=0x{exp:05x}  "
                      f"(xor=0x{live ^ exp:05x})")
    t.close()
    print(f"\n  checked {checked} RF registers across 4 paths; {diffs} diverged.")
    if diffs == 0:
        print("  => RF register state is identical to the kernel's bring-up. RX RF path matches.")
    else:
        print("  NOTE: many RF registers read back a chip-computed value (PLL/RC-cal/status) that\n"
              "  differs from the LSSI-written value — e.g. writing 0x8a=0x43e50 reads back 0x42470\n"
              "  uniformly on all 4 paths, immediately after the radio table and independent of\n"
              "  _copy_rck1. So a live-read vs capture-WRITE mismatch on these is NOT a divergence:\n"
              "  our writes match the kernel (verify_pcap), and the kernel's chip reads back the same.\n"
              "  Treat a divergence here as a lead only if the register is a static config field.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
