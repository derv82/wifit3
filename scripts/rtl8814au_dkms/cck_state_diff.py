"""Live-vs-capture state diff over the BB AGC / CCK register block.

`verify_pcap` proves our init *writes* match the capture, but it feeds recorded reads —
so a read-modify-write that lands on a wrong value on LIVE silicon is invisible to it.
This walks every BB register in the AGC/CCK range that the capture's kernel wrote during
bring-up, reads the SAME register off our live chip after our bring-up, and reports any
divergence. A mismatch in the CCK/AGC block is a prime suspect for the CCK RX deficit
(the reference AP beacons 100% CCK and is under-heard).

    uv run python scripts/rtl8814au_dkms/cck_state_diff.py [capture-N]
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "rtl8814au_dkms"))

import libusb_package  # noqa: E402
import usb.core  # noqa: E402

import rtw88_pcap_replay as rp  # noqa: E402
import verify_pcap as vp  # noqa: E402
import dump_tune_regs as dt  # noqa: E402  (reuse _bring_up)

from wifit3.chips.rtl8814au_dkms import constants as C  # noqa: E402
from wifit3.chips.rtl8814au_dkms.transport import Rtl8814auTransport  # noqa: E402

CAP_DIR = REPO / "driver_captures" / "captures_rtl8814au"

# Range is CLI-selectable: `cck_state_diff.py [capture] [LO] [HI]`. Default = BB AGC/CCK block.
# Registers that COUNT/latch/adapt at runtime are excluded — they legitimately differ from a
# single capture snapshot. (Bias-free: this compares live state vs the kernel's, not by name.)
import sys as _sys
LO = int(_sys.argv[2], 0) if len(_sys.argv) > 2 else 0x0800
HI = int(_sys.argv[3], 0) if len(_sys.argv) > 3 else 0x0BFF
_RUNTIME_EXCLUDE = {
    # BB FA/CCA counters + reset pulses, dbg-port, adapted gain/CCK-PD/EDCCA/NHM
    0x0A2C, 0x0A5C, 0x0B58, 0x09A4, 0x0F48, 0x0FA0, 0x08FC, 0x08F8, 0x198C, 0x0A0A,
    0x0A08, 0x0C50, 0x0E50, 0x1850, 0x1A50, 0x08A4, 0x0994, 0x0998, 0x099C, 0x09A0, 0x0990,
    # MAC dynamic: LED, TX/RX DMA status, EDCCA countdown, beacon/TSF/FWHW toggles, efuse, CR
    0x0060, 0x0210, 0x0288, 0x0520, 0x0524, 0x0550, 0x0422, 0x0423, 0x0205, 0x0100, 0x0002,
    0x0008, 0x0090, 0x06A4, 0x0670, 0x010C,
    # halrf TX-power thermal + txagc + RF-SIPI write regs (per-path, runtime-corrected)
    0x1998, 0x1B00, 0x0C90, 0x0E90, 0x1890, 0x1A90, 0x0440,
    # per-hop channel-dependent (our state-diff tunes ch1 only; capture window includes hops)
    0x0A20, 0x0A24, 0x0A28, 0x0958, 0x0860, 0x087C,
}


def _capture_final_writes(name: str) -> dict:
    """addr -> final 32-bit value the capture's kernel wrote during init+airmon (last write
    wins). Only the deterministic bring-up window (before the operational watchdog phase)."""
    pcap = CAP_DIR / f"{name}.pcap"
    dev = vp.DEV_ADDR.get(name) or rp.find_card_device(pcap)
    ops = rp.extract_ops(pcap, dev)
    # Trim to the bring-up: from the probe chip-version read to the first dynamic-check tick
    # opener (R 0x0210) — i.e. init + airmon, the window whose final state the chip enters
    # monitor with. Everything after is the runtime watchdog (counters, IGI adapt).
    start = next((i for i, o in enumerate(ops)
                  if o["kind"] == "R" and o.get("addr") == C.REG_SYS_CFG1), 0)
    end = next((i for i, o in enumerate(ops)
                if o["kind"] == "R" and o.get("addr") == 0x0210 and i > start), len(ops))
    finals: dict = {}
    for o in ops[start:end]:
        if o["kind"] == "W" and o.get("width") == 4 and LO <= o.get("addr", 0) <= HI:
            finals[o["addr"]] = o["value"]
    return finals


def main() -> int:
    name = Path(sys.argv[1] if len(sys.argv) > 1 else "capture-1").stem
    print(f"[*] extracting capture final BB AGC/CCK writes from {name}...", file=sys.stderr)
    finals = _capture_final_writes(name)
    print(f"[*] {len(finals)} registers written in [0x{LO:04x},0x{HI:04x}] during init+airmon",
          file=sys.stderr)

    dev = usb.core.find(idVendor=C.VID_REALTEK, idProduct=C.PID_RTL8814AU,
                        backend=libusb_package.get_libusb1_backend())
    if dev is None:
        print("[-] RTL8814AU not found.", file=sys.stderr)
        return 1
    t = Rtl8814auTransport(dev)
    print("[*] bringing up live...", file=sys.stderr)
    dt._bring_up(t, 1)

    print("\n[*] live-vs-capture diff (BB AGC/CCK block, excluding runtime counters):")
    diffs = 0
    checked = 0
    for addr in sorted(finals):
        if addr in _RUNTIME_EXCLUDE:
            continue
        checked += 1
        live = t.read32(addr)
        exp = finals[addr]
        if live != exp:
            diffs += 1
            print(f"  DIVERGE 0x{addr:04x}: live=0x{live:08x}  capture=0x{exp:08x}  "
                  f"(xor=0x{live ^ exp:08x})")
    t.close()
    print(f"\n  checked {checked} registers; {diffs} diverged from the capture.")
    if diffs == 0:
        print("  => BB AGC/CCK state is byte-identical to the kernel's bring-up. "
              "The CCK deficit is NOT a static register divergence.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
