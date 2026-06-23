"""Acceptance gate: ONE monotonic single-cursor walk of the rtl8821cu_dkms cold-boot capture.

Fail-closed, never short-circuited. One cursor runs from the driver's first vendor op to the
end of the capture; every op has exactly one honest fate as the cursor advances:

  * matched     — a real port handler reproduces it byte-for-byte at the cursor.
  * unaccounted — anything else STOPS the walk and names the op: the porting frontier.

Two phases. The **deterministic** phase (``bringup.cold_bringup``) is the single-threaded cold
init + airmon monitor entry, reproduced in driver source order. The **operational** phase
dispatches each interleaved async burst to its real port handler at the cursor — a channel hop
(``chan.set_channel``) or an LED blink (``led.led_blink``, the BlinkTimer producer) — each
distinguished by a unique opener op. Nothing is stripped: the LED writes are reproduced by the
driver's LED code, not waived (PORTING.md Step 3).

The replay drives the real ``Rtl8821cuTransport`` over a ``ReplayDevice`` (one ctrl_transfer
cursor), so the 0x4E0 ON-section mirror + the merged bulk-OUT FW stream replay unchanged and the
transport's session state (btc / HMEBOX / band / cached BB regs) persists across handlers.

    uv run python scripts/verify_pcap.py rtl8821cu_dkms
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8821cu_dkms import bringup, chan, led  # noqa: E402
from wifit3.chips.rtl8821cu_dkms.transport import Rtl8821cuTransport  # noqa: E402

DEFAULT_CAP = REPO / "usb_dumps_new2" / "captures_rtl8821cu" / "capture-1.pcap"

# Operational-dispatch openers (each the unique first op of a vendor handler at the cursor):
_OP_HOP = 0x2860                 # R: read_rf(0x18) = 0x2800+(0x18<<2) opens a same-band tune
_OP_LED = 0x004E                 # R: SwLedBlink1 / pinmux_wl_led_sw_ctrl opens an LED blink tick
_REG_RF_CHNL_LSSI = 0x0C90       # path-A LSSI write — carries the tuned channel in addr 0x18


def _fmt(op: dict) -> str:
    if op.get("dir") == "BULK":
        return f"BULK[{len(op['data'])}B]"
    d = op.get("data", b"")
    val = f"=0x{int.from_bytes(d, 'little'):0{max(len(d) * 2, 2)}x}" if d else ""
    return f"{op['dir']} 0x{op['wval']:04x}/{op['width']}{val}"


class Walk:
    """One ctrl_transfer cursor over the whole capture, driving the real transport. ``run`` calls
    a port handler against the shared transport; the device cursor advances by exactly the ops the
    handler consumed. Session state lives on the transport, so it persists across handlers."""

    def __init__(self, ops: list[dict]):
        self.ops = ops
        self.dev = rp.ReplayDevice(ops)
        self.t = Rtl8821cuTransport(self.dev)

    @property
    def i(self) -> int:
        return self.dev.i

    def peek(self) -> dict | None:
        return self.ops[self.dev.i] if self.dev.i < len(self.ops) else None

    def run(self, fn, label: str):
        return fn()


def _peek_channel(w: Walk, start: int, window: int = 200) -> int | None:
    """The channel a tune targets is a runtime input (airodump chooses it); read it from the
    upcoming path-A LSSI write to RF 0x18. The LSSI word is (addr[7:0]<<20)|data[19:0]; RF
    0x18[7:0] holds the channel number."""
    for o in w.ops[start:start + window]:
        if o["dir"] == "OUT" and o.get("wval") == _REG_RF_CHNL_LSSI and o["width"] == 4:
            v = int.from_bytes(o["data"], "little")
            if (v >> 20) == 0x18:
                return v & 0xFF
    return None


def _walk_operational(w: Walk, info) -> tuple[int, int, dict | None]:
    """Dispatch each operational burst to its real handler at the cursor. Two producers interleave:
    channel hops (``set_channel``, opener = read_rf 0x18) and the LED blink (``led_blink``, opener
    = read 0x4e). The first op that opens no handler STOPS the walk and is returned as the frontier."""
    led_st = led.LedBlinkState()
    hops = leds = 0
    while w.i < len(w.ops):
        o = w.peek()
        if o["dir"] == "IN" and o.get("wval") == _OP_HOP:
            ch = _peek_channel(w, w.i)
            if ch is None:
                break
            try:
                w.run(lambda c=ch: chan.set_channel(w.t, info, c), f"hop{ch}")
            except Exception as e:  # noqa: BLE001
                return hops, leds, _frontier(w, o, f"hop ch{ch}", e)
            hops += 1
            continue
        if o["dir"] == "IN" and o.get("wval") == _OP_LED:
            try:
                w.run(lambda: led.led_blink(w.t, led_st), f"led#{leds + 1}")
            except Exception as e:  # noqa: BLE001
                return hops, leds, _frontier(w, o, f"led #{leds + 1}", e)
            leds += 1
            continue
        break  # frontier: unknown opener
    return hops, leds, w.peek()


def _frontier(w: Walk, o: dict, label: str, e: Exception) -> dict:
    kind = "DIVERGED" if isinstance(e, rp.Divergence) else "ERROR"
    print(f"\n  {label} {kind} at op {w.i}:\n    {type(e).__name__}: {e}")
    return o


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None       # replay needs no settle delays

    pcap = Path(cap) if cap else DEFAULT_CAP
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1

    dev_addr = rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev_addr)
    ops = rp.merge_ops_by_frame(rp.extract_ctrl_ops(pcap, dev_addr),
                                rp.extract_bulk_out_ops(pcap, dev_addr))
    total = len(ops)
    print(f"{pcap.name}: card=dev{dev_addr}, {total} ctrl+bulk ops")

    w = Walk(ops)
    try:
        info = bringup.cold_bringup(w.t)
    except rp.Divergence as e:
        print(f"\nFAIL (cold-init/airmon divergence) after {w.i} ops:\n  {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR (harness/port bug) at op {w.i}: {type(e).__name__}: {e}")
        return 2
    print(f"  deterministic init+airmon: reproduced {w.i} ops single-cursor (cold probe -> FW dl"
          " -> MAC/BB/RF -> channel tune -> monitor entry -> media-connect coex)")

    init_end = w.i
    hops, leds, frontier = _walk_operational(w, info)
    print(f"  operational: {hops} channel hops + {leds} LED blinks reproduced "
          f"({w.i - init_end} ops)")

    if frontier is not None:
        print(f"\nFRONTIER -> op #{w.i} (frame {frontier['frame']}): {_fmt(frontier)}")
        print("  ^ the next op to reproduce (port it).")
        return 1

    print(f"\nPASS: reproduced all {w.i} of {total} ops single-cursor — entire capture "
          f"byte-for-byte (init + airmon + {hops} hops + {leds} LED blinks).")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
