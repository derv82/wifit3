"""Does verify_pcap actually CATCH divergences? — an adversarial self-test of the gate.

A gate that PASSES a correct port is worthless if it also passes a BROKEN one. This mutates the
real driver's output one op-class at a time (corrupt a computed value / inject an extra write),
re-runs verify_pcap against the SAME capture, and asserts the gate flips to FAIL. A mutation the
gate still PASSES is a blind spot — a class of real divergence the gate cannot see.

Each mutation is a monkeypatch of a driver write-emitter, applied only for that one run and then
restored; the driver source on disk is never changed. The call-counter distinguishes a genuine
blind spot (emitter ran, gate still PASSED) from "not exercised in this capture" (emitter never
ran — inconclusive, try another capture).

    uv run python scripts/chips/rtl8814au_dkms/verify_pcap_selftest.py
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import importlib.util  # noqa: E402

# Load the CHIP-specific verify_pcap by path — a bare `import verify_pcap` resolves to the
# top-level scripts/porting/verify_pcap.py dispatcher (also on sys.path), which has no run().
_spec = importlib.util.spec_from_file_location(
    "chip_verify_pcap", Path(__file__).resolve().parent / "verify_pcap.py")
vp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vp)

from wifit3.chips.rtl8814au_dkms import chan, driver as drv_mod, iqk, watchdog  # noqa: E402


class Mutation:
    """One monkeypatch that corrupts a single op-class of driver output."""

    def __init__(self, name, op_class, module, attr, wrap, capture):
        self.name, self.op_class = name, op_class
        self.module, self.attr, self.wrap, self.capture = module, attr, wrap, capture
        self.calls = 0

    def __enter__(self):
        self._orig = getattr(self.module, self.attr)
        counter = self

        def patched(*a, **k):
            counter.calls += 1
            return self.wrap(self._orig, *a, **k)
        setattr(self.module, self.attr, patched)
        return self

    def __exit__(self, *exc):
        setattr(self.module, self.attr, self._orig)


# --- corruption styles -------------------------------------------------------
def _corrupt_return_int(orig, *a, **k):
    return orig(*a, **k) ^ 0x1                       # flip a bit of a computed register value

def _corrupt_return_bytes(orig, *a, **k):
    b = bytearray(orig(*a, **k)); b[0] ^= 0x80; return bytes(b)   # flip a byte of the TX descriptor

def _prepend_bogus_write(orig, t, *a, **k):
    t.write32(0xFFF8, 0xDEADBEEF)                    # an op the wire does NOT have -> must diverge
    return orig(t, *a, **k)


MUTATIONS = [
    Mutation("chan._fc_area (channel-tune write)", "tune/bring-up",
             chan, "_fc_area", _corrupt_return_int, "capture-1"),
    Mutation("watchdog.led_blink (LED producer)", "led",
             watchdog, "led_blink", _prepend_bogus_write, "capture-1"),
    Mutation("watchdog._dig (DIG watchdog tick)", "tick",
             watchdog, "_dig", _prepend_bogus_write, "capture-1"),
    Mutation("iqk.do_iqk_8814a (IQK calibration)", "iqk",
             iqk, "do_iqk_8814a", _prepend_bogus_write, "capture-1"),   # IQK fires 2x here
    Mutation("build_mgmt_txdesc (TX-inject descriptor)", "inject",
             drv_mod, "build_mgmt_txdesc", _corrupt_return_bytes, "capture-2"),
]


def _run_gate(capture: str) -> int:
    """verify_pcap.run(capture), stdout suppressed; return its exit code (0=PASS, !=0=FAIL)."""
    with contextlib.redirect_stdout(io.StringIO()):
        return vp.run(capture)


def main() -> int:
    print("baseline (unmutated) — every capture must PASS first:")
    for cap in ("capture-1", "capture-2", "new2/capture-1"):
        rc = _run_gate(cap)
        print(f"  {cap:<16} {'PASS' if rc == 0 else f'FAIL(rc={rc})'}")
        if rc != 0:
            print("  !! baseline is not green — fix that before trusting the self-test")
            return 2

    print("\nmutation                                    class        calls  gate     verdict")
    print("-" * 92)
    blind = 0
    inconclusive = 0
    for m in MUTATIONS:
        with m:
            rc = _run_gate(m.capture)
        if m.calls == 0:
            verdict, tag = "NOT EXERCISED (try another capture)", "?"
            inconclusive += 1
        elif rc != 0:
            verdict, tag = "caught", "OK"
        else:
            verdict, tag = "*** BLIND SPOT — gate PASSED a broken driver ***", "!!"
            blind += 1
        print(f"  {m.name:<42} {m.op_class:<11} {m.calls:>5}  "
              f"{'PASS' if rc == 0 else 'FAIL':<7} [{tag}] {verdict}")

    print("-" * 92)
    if blind:
        print(f"RESULT: {blind} BLIND SPOT(S) — the gate does not catch these divergences.")
        return 1
    if inconclusive:
        print(f"RESULT: no blind spots, but {inconclusive} class(es) not exercised by their capture "
              "— rerun those against a capture that exercises them before trusting coverage.")
        return 3
    print("RESULT: every mutated op-class flips the gate to FAIL — divergences DO show up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
