"""Acceptance gate: replay-diff the clean-room rt3070 port against its cold-boot capture.

ONE monotonic walk, ONE cursor, fail-closed. Modelled on the gold standard
``scripts/rtl8188eus_dkms/verify_pcap.py`` (single extract, single cursor, real-function
calls, explicit named boundaries), adapted to the **rt2x00 USB family**: the replay is at
the ``ctrl_transfer`` layer (``rt2x00_pcap_replay.ReplayDevice``) and the REAL
``chips/rt3070`` transport drives it, so every helper (regbusy poll, write_multi chunking,
the EFUSE walker) replays with zero reimplementation.

  * **matched**     — the port's real handler reproduces the op byte-for-byte at the cursor.
  * **waived**      — an explicit, *named*, *counted* boundary for a producer that is NOT the
                      rt2800usb kernel driver (aireplay-ng's injected TX is bulk-OUT and never
                      enters the control op stream, so it needs no waiver here; a waiver slot
                      is kept for any aireplay-*triggered* control op the agent identifies).
  * **unaccounted** — anything else STOPS the walk and names the op. That op IS the porting
                      frontier: the next thing to make faithful. PASS ⇔ zero unaccounted.

RULES (do not violate — this is the whole point of the gate):
  * NEVER edit this file to make it print PASS. A prior rt2800usb gate was whittled down to a
    firmware-only block that printed PASS over a driver that mis-read EFUSE — exactly the
    disaster this rewrite exists to prevent.
  * NEVER copy logic from chips/rt2800usb/ — it is a *structural* reference only and has a
    CONFIRMED EFUSE word-vs-byte addressing bug (see chips/rt3070/RT3070.md). Port from the
    kernel C in data_dumps/rt2x00-source-v6.18/ and let THIS wire confirm it.
  * The cursor only advances by reproducing the wire or by an explicit named waiver.

We do not run airmon-ng / airodump-ng / iw / aireplay-ng against the port; the chip only sees
register writes, so the *kernel-driver* writes those tools trigger are ours to reproduce.
wifit3 is the trigger: connect() stands in for the probe + airmon monitor entry, the channel
hopper for airodump/iw (per-hop set_channel), and the periodic link tuner for rt2x00link's
~1 Hz BBP66 AGC work.

    uv run python scripts/verify_pcap.py rt3070 [capture-1|capture-2|capture-3]
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

CAP_DIR = REPO / "usb_dumps_new" / "captures_rt3070"
MAC_CSR0 = 0x1000          # silicon id + revision; the first vendor op of the probe

# The clean-room driver you are building. These imports fail until you scaffold the modules;
# that is expected — the recipe then tells you to start at M1. As each module lands, its
# milestone in _walk_init starts reproducing wire and the frontier advances.
_IMPORT_ERR = None
try:
    from wifit3.chips.rt3070.transport import RT3070Transport  # noqa: E402
    from wifit3.chips.rt3070 import bbp, chan, eeprom, firmware, mac, rfcsr  # noqa: E402
except ImportError as e:  # driver not scaffolded yet
    _IMPORT_ERR = e


class Walk:
    """One cursor over the whole capture. ``run`` drives a real port handler against the wire
    from the cursor (a fresh ReplayDevice over the remaining ops, wrapped in the real chip
    transport); ``waive`` consumes one op of a named non-reproduced producer. Both advance."""

    def __init__(self, ops: list[dict]):
        self.ops = ops
        self.i = 0
        self.waived: Counter = Counter()

    def run(self, fn, label: str):
        rd = rp.ReplayDevice(self.ops[self.i:])
        t = RT3070Transport(rd)
        result = fn(t)
        self.i += rd.i
        return result

    def peek(self) -> dict | None:
        return self.ops[self.i] if self.i < len(self.ops) else None

    def waive(self, reason: str) -> None:
        self.waived[reason] += 1
        self.i += 1


def _walk_init(w: Walk, out: dict) -> None:
    """Deterministic cold bring-up, in kernel source order (rt2800_probe_hw flattened), one
    cursor, no re-anchoring. Mirror driver.py connect(). Implement these in order; run the
    gate after each — the first unimplemented call is your frontier.

    Source: data_dumps/rt2x00-source-v6.18/{rt2800usb.c,rt2800lib.c}. Line cites in RT3070.md.

    M2a  firmware upload + MCU boot          rt2800usb.c rt2800usb_write_firmware
    M2b  usb bootstrap (USB_DEVICE_MODE)     rt2800usb.c rt2800usb_init_registers
    M2c  EFUSE dump (WORD offset!) + parse   rt2800lib.c rt2800_read_eeprom_efuse (10955)
    M2d  MAC config block                    rt2800lib.c rt2800_init_registers
    M2e  BBP init (30xx)                     rt2800lib.c rt2800_init_bbp_30xx (6521)
    M2f  RFCSR init (30xx) + rx-filter cal   rt2800lib.c rt2800_init_rfcsr_30xx (7618)
    M3   enable radio (TX/RX/WPDMA + filter) rt2800lib.c rt2800_enable_radio / config_filter
    M4   tune to default channel (RF3020)    rt2800lib.c rt2800_config_channel_rf3xxx (2547)
    """
    fw = firmware.load_firmware_blob()
    w.run(lambda t: firmware.upload(t, fw), "firmware")              # M2a
    w.run(lambda t: mac.usb_init_registers(t), "usb-init")           # M2b
    buf = w.run(lambda t: eeprom.read_eeprom_efuse(t), "efuse")      # M2c
    out["eeprom"] = eeprom.parse_eeprom(buf)
    ev = out["eeprom"]
    w.run(lambda t: mac.init_registers(t, ev), "mac-cfg")            # M2d
    w.run(lambda t: bbp.prepare_bbp(t), "bbp-prep")
    w.run(lambda t: bbp.init_bbp_30xx(t, ev), "bbp-init")            # M2e
    w.run(lambda t: rfcsr.init_rfcsr_30xx(t, ev), "rfcsr-init")      # M2f (incl. rx-filter cal)
    w.run(lambda t: mac.enable_radio(t, ev), "enable-radio")         # M3
    w.run(lambda t: chan.set_channel(t, ev, 1), "chan1")             # M4


# Operational-phase openers (airmon monitor entry, airodump/iw channel hops, the ~1 Hz link
# tuner). Identify each opener's first register op from the wire and dispatch it to the real
# handler, carrying channel/AGC state across fires — the rtl8188eus_dkms pattern. TODO(agent):
# fill these in as you reach the operational frontier (frames 1943+; see RT3070.md timeline).
def _walk_operational(w: Walk, ev, out: dict) -> dict | None:
    while w.i < len(w.ops):
        o = w.peek()
        # TODO(agent): dispatch monitor-entry / set_channel / link-tuner bursts here.
        #   e.g.  if o["dir"] == "IN" and o["addr"] == <CHAN_OPENER>:
        #             ch = _peek_channel(w.ops, w.i); w.run(lambda t: chan.set_channel(t, ev, ch), f"chan{ch}")
        #             continue
        break  # frontier: first op no handler claims
    return w.peek()


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
    rev = int.from_bytes(csr0["data"], "little") & 0xFFFF if csr0 else 0

    # Anchor at the first vendor op (the MAC_CSR0 silicon/rev read at probe).
    anchor = next((i for i, o in enumerate(full) if o is csr0), 0)
    ops = full[anchor:]
    print(f"{name}: card=dev{dev}, {len(full)} vendor ops -> walk {len(ops)} "
          f"(silicon=0x{silicon:04x} rev=0x{rev:04x})")

    if _IMPORT_ERR is not None:
        print(f"\nrt3070 driver not scaffolded yet ({_IMPORT_ERR}).")
        first = ops[0] if ops else None
        if first:
            print(f"FRONTIER: op 0 = {rp.ReplayDevice._fmt(first)}")
        print("  ^ start at M1 (firmware). Build chips/rt3070/{transport,firmware,...}.py from "
              "the kernel C; see chips/rt3070/RT3070.md. Re-run after each milestone.")
        return 1

    w = Walk(ops)
    out: dict = {}
    try:
        _walk_init(w, out)
    except rp.Divergence as e:
        print(f"\nFAIL (init divergence) at op {w.i}:\n  {e}")
        return 1
    except (AttributeError, NotImplementedError) as e:
        fr = w.peek()
        print(f"\nFRONTIER at op {w.i}: handler not ported yet ({type(e).__name__}: {e})")
        if fr:
            print(f"  next wire op = {rp.ReplayDevice._fmt(fr)}")
        return 1
    init_end = w.i
    print(f"  init: reproduced {init_end} ops single-cursor (firmware -> chan1, no gaps)")

    frontier = _walk_operational(w, out.get("eeprom"), out)
    for reason, n in w.waived.most_common():
        print(f"  waived {n:5} ops  — {reason}")
    if frontier is not None:
        print(f"\nFRONTIER: reproduced {w.i} of {len(ops)} ops; first unaccounted op @{w.i} "
              f"= {rp.ReplayDevice._fmt(frontier)} (frame {frontier.get('frame')})")
        print("  ^ the next thing to make faithful (port it, or add a named waiver).")
        return 1

    print(f"\nPASS: reproduced {w.i} of {len(ops)} ops — every op matched or explicitly waived.")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
