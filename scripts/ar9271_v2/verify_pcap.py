"""Acceptance gate: replay-diff the clean-room ar9271_v2 port against its cold-boot capture.

ONE monotonic walk, ONE cursor, fail-closed — the model from ``scripts/rt5370/verify_pcap.py``
and ``scripts/rtl8821cu_dkms/verify_pcap.py`` (async producers dispatched to real handlers at
the cursor). The replay is at the USB layer (``ar9271_pcap_replay.ReplayDevice``) and the REAL
``chips/ar9271_v2`` transport drives it, so each milestone's handler replays with zero
reimplementation.

  * **matched**     — the port's real handler reproduces the op byte-for-byte at the cursor.
  * **waived**      — an explicit, *named*, *counted* boundary for a producer that is NOT the
                      ath9k_htc kernel driver (e.g. aireplay-ng's TX from the human-fired
                      injection at the capture tail).
  * **unaccounted** — anything else STOPS the walk and names the op. That op IS the porting
                      frontier: the next op to reproduce.

This port is built in milestones; until the last lands, a clean run ends at the FRONTIER (the
first not-yet-ported op), not PASS. That is the expected, honest state mid-port — the frontier
op names exactly where the next milestone begins.

RULES (do not violate — this is the whole point of the gate):
  * NEVER edit this file to make it print PASS.
  * NEVER read chips/ar9271/ (the v1 port) — v2 is a blind re-port from the kernel C in
    data_dumps/ath9k-source-v6.18/. Let THIS wire confirm it.
  * The cursor only advances by reproducing the wire or by an explicit named waiver.

    uv run python scripts/verify_pcap.py ar9271_v2 [capture-1|capture-2|capture-3]
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "ar9271_v2"))

import ar9271_pcap_replay as rp  # noqa: E402

CAP_DIR = REPO / "usb_dumps_new" / "captures_ath9k_htc_newddevice"

_IMPORT_ERR = None
try:
    from wifit3.chips.ar9271_v2 import firmware, htc       # noqa: E402
    from wifit3.chips.ar9271_v2.transport import AR9271Transport  # noqa: E402
except ImportError as e:                                  # driver not scaffolded yet
    _IMPORT_ERR = e


class Walk:
    """One cursor over the whole capture. ``run`` drives a real port handler against the wire
    from the cursor (a fresh ReplayDevice over the remaining ops, wrapped in the real chip
    transport); ``waive`` consumes one op of a named non-reproduced producer. Both advance."""

    def __init__(self, ops: list[dict], responses: list[dict]):
        self.ops = ops
        self.responses = responses
        self.i = 0
        self.waived: Counter = Counter()

    def run(self, fn, label: str):
        rd = rp.ReplayDevice(self.ops[self.i:], self.responses)
        t = AR9271Transport(rd)
        result = fn(t)
        self.i += rd.i
        return result

    def peek(self) -> dict | None:
        return self.ops[self.i] if self.i < len(self.ops) else None

    def waive(self, reason: str) -> None:
        self.waived[reason] += 1
        self.i += 1


def _walk_init(w: Walk) -> None:
    """Cold bring-up, one cursor, no re-anchoring. WIRE ORDER (ath9k_htc):
    firmware download -> [M2 frontier: HTC/WMI handshake + ath9k_hw init].

    Source: data_dumps/ath9k-source-v6.18/ath9k/hif_usb.c.

    firmware   13x 4096B RAM writes (bRequest 0x30) + COMP (0x31)   ath9k_hif_usb_download_fw
    htc        READY -> 9x connect_service -> config credits ->      htc_hst.c / htc_drv_init.c
               setup complete
    --- M2b frontier: WMI register init (ath9k_hw) / calibration / monitor RX filter ---
    """
    fw = firmware.load_firmware_blob()
    w.run(lambda t: firmware.download(t, fw), "firmware")
    w.run(lambda t: htc.handshake(t), "htc-handshake")


def run(cap: str | None = None) -> int:
    if _IMPORT_ERR is not None:
        print(f"ar9271_v2 driver not importable yet: {_IMPORT_ERR}")
        return 2

    name = cap or "capture-1"
    pcap = CAP_DIR / f"{name}.pcap"
    if not pcap.exists():
        print(f"capture not found: {pcap}")
        return 2

    pkts = rp.parse_pcapng(str(pcap))
    dev = rp.detect_card(pkts)
    if dev is None:
        print(f"no AR9271 firmware-download traffic found in {pcap.name}")
        return 2
    ex = rp.extract(pkts, dev)
    ops, responses = ex["host_ops"], ex["responses"]
    w = Walk(ops, responses)

    print(f"ar9271_v2 verify — {pcap.name}: devnum {dev}, {len(ops)} host ops, "
          f"{len(responses)} device responses")

    try:
        _walk_init(w)
    except rp.Divergence as d:
        print(f"\nDIVERGENCE at op #{w.i}: {d}")
        return 1

    for reason, n in w.waived.most_common():
        print(f"  waived {n:5} ops  — {reason}")

    if w.i < len(ops):
        front = w.peek()
        print(f"\nFRONTIER: reproduced {w.i} of {len(ops)} ops; first unaccounted op @{w.i} "
              f"= {rp.fmt_op(front)}")
        print("  ^ the next op to reproduce (port the next milestone, or add a named waiver).")
        # Mid-port milestone markers: each frontier op names where the next milestone begins.
        if w.i == 14:
            print("  M1 OK: 14 firmware-download control ops matched; frontier is the HTC "
                  "handshake (M2a).")
        elif w.i == 25:
            print("  M2a OK: firmware + HTC handshake (9 service connects + config credits + "
                  "setup complete) matched; frontier is the WMI register init (M2b).")
        return 1

    print(f"\nPASS: reproduced {w.i} of {len(ops)} ops — every op matched or explicitly waived.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1] if len(sys.argv) > 1 else None))
