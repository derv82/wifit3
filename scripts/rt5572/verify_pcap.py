"""Acceptance gate: replay-diff the rt5572 (Ralink RT5592 / RF5592) port against its
cold-boot capture. Byte-for-byte, fail-closed, one cursor.

The point of this gate — and the whole reason chips/rt5572 exists apart from the
rt2800usb imitation port — is that it drives the driver's REAL code. The cold phase
is a single call to ``bring_up.bring_up()``, the exact function ``driver.connect()``
runs on hardware. There is no hand-written step list to keep in sync: change the
bring-up and this gate re-tests precisely what connect() does. (Contrast rt2800usb's
old recipe, which reproduced a kernel-ordered walk that connect() never followed —
100% green while the driver diverged.)

Modelled on scripts/rt5372/verify_pcap.py (the rt2x00-family "done right" shape):

  * matched   — the driver's real handler reproduces the op byte-for-byte at the cursor.
  * waived    — a named, counted boundary for a producer that is NOT the kernel driver
                (aireplay-ng's TX_STA_FIFO status polling from human-fired injection;
                bulk-OUT TX frames are on their own endpoints, out of the control stream).
  * frontier  — anything else STOPS the walk and names the op: the next op to reproduce.

RULES:
  * NEVER edit this file to make it print PASS.
  * The cursor only advances by reproducing the wire or by an explicit named waiver.
  * The gate calls the DRIVER's functions. If a fix belongs in the port, it goes in
    chips/rt5572, never here.

    uv run python scripts/verify_pcap.py rt5572 [capture-1]
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rt2x00_pcap_replay as rp  # noqa: E402
from wifit3.chips.rt5572.bring_up import bring_up  # noqa: E402
from wifit3.chips.rt5572.transport import RT2800USBTransport  # noqa: E402

CAP_DIR = REPO / "usb_dumps_new2" / "captures_rt2800usb_rt5572"
MAC_CSR0 = 0x1000
TX_STA_FIFO = 0x1718

_AIREPLAY_TAIL = ("aireplay-ng TX_STA_FIFO polling (human-fired injection; "
                  "bulk-OUT TX frames are out of the control stream)")


class Walk:
    """One cursor over the capture. ``run`` drives a real driver handler against the
    wire from the cursor (a fresh ReplayDevice over the remaining ops wrapped in the
    real transport); ``waive`` consumes one op of a named non-driver producer."""

    def __init__(self, ops: list[dict]):
        self.ops = ops
        self.i = 0
        self.waived: Counter = Counter()

    def run(self, fn):
        rd = rp.ReplayDevice(self.ops[self.i:])
        result = fn(RT2800USBTransport(rd))
        self.i += rd.i
        return result

    def peek(self) -> dict | None:
        return self.ops[self.i] if self.i < len(self.ops) else None

    def waive(self, reason: str) -> None:
        self.waived[reason] += 1
        self.i += 1


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None        # replay needs no real settle delays
    name = Path(cap or "capture-1").stem
    pcap = CAP_DIR / f"{name}.pcap"
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1

    dev = rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev)
    full = rp.extract_ops(pcap, dev)
    csr0 = next((o for o in full if o["dir"] == "IN" and o["addr"] == MAC_CSR0), None)
    silicon = (int.from_bytes(csr0["data"], "little") >> 16) & 0xFFFF if csr0 else 0
    anchor = next((i for i, o in enumerate(full) if o is csr0), 0)
    ops = full[anchor:]
    print(f"{name}: card=dev{dev}, {len(full)} vendor ops -> walk {len(ops)} "
          f"(silicon=0x{silicon:04x})")

    w = Walk(ops)

    # ---- cold bring-up: ONE call to the driver's real bring_up() ----
    try:
        w.run(lambda t: bring_up(t))
    except rp.Divergence as e:
        fr = w.peek()
        print(f"\nFAIL (cold bring-up divergence) after {w.i} ops:\n  {e}")
        if fr:
            print(f"  frontier op = {rp.ReplayDevice._fmt(fr)} @f{fr.get('frame')}")
        print("  ^ the driver's bring_up() diverges from the kernel here. Fix it in "
              "chips/rt5572/, never in this gate.")
        return 1
    print(f"  OK  cold bring-up: driver.bring_up() reproduced {w.i} ops byte-for-byte "
          "(one call, no hand-written step list).")

    # ---- operational phase: TODO (next milestone) ----
    # Will dispatch the driver's real set_channel + monitor entry at wire openers and
    # waive aireplay's TX_STA_FIFO. Until those handlers are ported, report the frontier.
    fr = w.peek()
    if fr is not None:
        print(f"\nFRONTIER at op {w.i}: operational phase not yet driven "
              f"= {rp.ReplayDevice._fmt(fr)} @f{fr.get('frame')}")
        print("  ^ next milestone: drive set_channel + monitor entry here; waive TX_STA_FIFO.")
        return 1

    print(f"\nPASS: reproduced {w.i} of {len(ops)} ops.")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
