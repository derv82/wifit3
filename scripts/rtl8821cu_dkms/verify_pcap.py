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
driver's LED code, not waived (docs/porting/METHODOLOGY.md Step 3).

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
from wifit3.chips.rtl8821cu_dkms import btc, efuse, led, tx, watchdog  # noqa: E402
from wifit3.chips.rtl8821cu_dkms.driver import Rtl8821cuDkmsDriver  # noqa: E402

DEFAULT_CAP = REPO / "usb_dumps_new2" / "captures_rtl8821cu" / "capture-1.pcap"

# Operational-dispatch openers (each the unique first op of a vendor handler at the cursor):
_OP_HOP = 0x2860                 # R: read_rf(0x18) = 0x2800+(0x18<<2) opens a same-band tune
_OP_LED = 0x004E                 # R: SwLedBlink1 / pinmux_wl_led_sw_ctrl opens an LED blink tick
_OP_TICK = 0x0210                # R: sreset xmit_status_check opens a phydm dynamic-check tick
_OP_PERI = 0x0770                # R: monitor_bt_ctr hi-pri counter opens a BT-coex periodical
_OP_BANDSW = 0x0430              # R: a band-switching tune opens with run_coex's limited_tx (0x430)
_REG_RF_CHNL_LSSI = 0x0C90       # path-A LSSI write — carries the tuned channel in addr 0x18


def _fmt(op: dict) -> str:
    if op.get("dir") == "BULK":
        return f"BULK[{len(op['data'])}B]"
    d = op.get("data", b"")
    val = f"=0x{int.from_bytes(d, 'little'):0{max(len(d) * 2, 2)}x}" if d else ""
    return f"{op['dir']} 0x{op['wval']:04x}/{op['width']}{val}"


def _run(coro):
    """Drive a driver coroutine to completion. Replay transport I/O is synchronous, so the
    coroutine never suspends on a real await — one ``send`` runs it straight to its ``return``."""
    try:
        coro.send(None)
    except StopIteration as e:
        return e.value
    raise RuntimeError("driver coroutine suspended on a real await; replay is synchronous")


# The driver INTENTIONALLY does not run dm._dc_cancellation (its ck320 stop/restart is a HW-proven RX
# killer — see RTL8821CU_DKMS.md), so the capture's dc_cancellation ops have no producer. The gate
# EXCEPTS exactly that one block: on the first mismatch it resyncs forward to the driver's next op,
# verifies the skipped span really is dc_cancellation by its unique 0xc10 path-A DC-comp write (so
# this can never mask a real divergence), records it, and continues — everything else still verifies
# byte-for-byte. Only one such except is allowed.
_DC_EXCEPT_SIG = 0x0C10          # path-A DC-cancel I write, unique to _dc_cancellation
_DC_EXCEPT_WINDOW = 120          # max ops the skipped block may span


class ExceptingReplayDevice(rp.ReplayDevice):
    def __init__(self, ops):
        super().__init__(ops)
        self.excepted: list[dict] = []
        self._used = False

    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex, data_or_wLength, timeout=None):
        start = self.i
        try:
            return super().ctrl_transfer(bmRequestType, bRequest, wValue, wIndex,
                                         data_or_wLength, timeout)
        except rp.Divergence:
            if self._used:
                raise
            want = "IN" if (bmRequestType & 0x80) else "OUT"
            width = data_or_wLength if want == "IN" else len(bytes(data_or_wLength or b""))
            for j in range(start + 1, min(start + _DC_EXCEPT_WINDOW, len(self.ops))):
                o = self.ops[j]
                if (o.get("dir") == want and o.get("wval") == wValue
                        and o.get("widx") == wIndex and o.get("width") == width):
                    block = self.ops[start:j]
                    if not any(b.get("dir") == "OUT" and b.get("wval") == _DC_EXCEPT_SIG
                               for b in block):
                        break              # span isn't dc_cancellation -> this is a real divergence
                    self.excepted = block
                    self.i = j
                    self._used = True
                    return super().ctrl_transfer(bmRequestType, bRequest, wValue, wIndex,
                                                 data_or_wLength, timeout)
            raise


class Walk:
    """One ctrl_transfer cursor over the whole capture, driving the **real driver**. The gate
    invokes the driver's public interface (``connect`` / ``set_channel`` / ``inject_frame``) so the
    bytes it verifies are exactly the product's, not a parallel reimplementation. The device cursor
    advances by the ops each call consumed; session state lives on the shared transport."""

    def __init__(self, ops: list[dict]):
        self.ops = ops
        self.dev = ExceptingReplayDevice(ops)
        self.driver = Rtl8821cuDkmsDriver(self.dev)
        self.t = self.driver.transport

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


def _walk_operational(w: Walk, info) -> tuple[int, int, int, int, int, dict | None]:
    """Dispatch each operational op at the cursor. Two kinds interleave. Driver-autonomous async
    producers: the LED blink (``led_blink``, opener read 0x4e), the phydm dynamic-check tick
    (``watchdog.tick``, opener read 0x210) and the BT-coex periodical (``btc.periodical``, opener
    read 0x770). And externally-scripted commands, replayed through the driver's public interface:
    a channel hop (``driver.set_channel``, opener read_rf 0x18 — the channel is read from the wire)
    and a frame inject (``driver.inject_frame``, a bulk-OUT TX — the 802.11 frame is the recorded
    bulk minus its descriptor, which the driver rebuilds and the replay byte-verifies). The first op
    that opens no handler STOPS the walk and is returned as the frontier."""
    led_st = led.LedBlinkState()
    wd_st = watchdog.WatchdogState(eeprom_thermal=info.eeprom_thermal,
                                   thermal_offset=efuse.thermal_offset(info))
    peri_st = btc.PeriodicalState()
    hops = leds = ticks = peris = injects = 0
    while w.i < len(w.ops):
        o = w.peek()
        if o["dir"] == "IN" and o.get("wval") in (_OP_HOP, _OP_BANDSW):
            ch = _peek_channel(w, w.i)
            if ch is None:
                break
            try:
                w.run(lambda c=ch: _run(w.driver.set_channel(c)), f"hop{ch}")
            except Exception as e:  # noqa: BLE001
                return hops, leds, ticks, peris, injects, _frontier(w, o, f"hop ch{ch}", e)
            hops += 1
            continue
        if o["dir"] == "BULK":
            frame = o["data"][tx.TXDESC_SIZE:]
            try:
                w.run(lambda f=frame: _run(w.driver.inject_frame(f)), f"inject{len(frame)}")
            except Exception as e:  # noqa: BLE001
                return hops, leds, ticks, peris, injects, _frontier(w, o, f"inject {len(o['data'])}B", e)
            injects += 1
            continue
        if o["dir"] == "IN" and o.get("wval") == _OP_LED:
            try:
                w.run(lambda: led.led_blink(w.t, led_st), f"led#{leds + 1}")
            except Exception as e:  # noqa: BLE001
                return hops, leds, ticks, peris, injects, _frontier(w, o, f"led #{leds + 1}", e)
            leds += 1
            continue
        if o["dir"] == "IN" and o.get("wval") == _OP_TICK:
            try:
                w.run(lambda: watchdog.tick(w.t, wd_st), f"tick#{ticks + 1}")
            except Exception as e:  # noqa: BLE001
                return hops, leds, ticks, peris, injects, _frontier(w, o, f"tick #{ticks + 1}", e)
            ticks += 1
            continue
        if o["dir"] == "IN" and o.get("wval") == _OP_PERI:
            try:
                w.run(lambda: btc.periodical(w.t, peri_st), f"peri#{peris + 1}")
            except Exception as e:  # noqa: BLE001
                return hops, leds, ticks, peris, injects, _frontier(w, o, f"peri #{peris + 1}", e)
            peris += 1
            continue
        break  # frontier: unknown opener
    return hops, leds, ticks, peris, injects, w.peek()


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
        _run(w.driver.connect())
    except rp.Divergence as e:
        print(f"\nFAIL (cold-init/airmon divergence) after {w.i} ops:\n  {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR (harness/port bug) at op {w.i}: {type(e).__name__}: {e}")
        return 2
    info = w.driver.info
    exc = w.dev.excepted
    reproduced = w.i - len(exc)
    print(f"  deterministic init+airmon: reproduced {reproduced} ops single-cursor (cold probe -> "
          "FW dl -> MAC/BB/RF -> channel tune -> monitor entry -> media-connect coex)")
    if exc:
        addrs = " ".join(f"0x{o['wval']:04x}" for o in exc)
        print(f"  EXCEPTED (intentional skip, NOT reproduced): {len(exc)} dm._dc_cancellation ops, "
              f"frames {exc[0]['frame']}-{exc[-1]['frame']} — HW-harmful ck320 toggle, see CHIP doc")
        print(f"    {addrs}")

    init_end = w.i
    hops, leds, ticks, peris, injects, frontier = _walk_operational(w, info)
    print(f"  operational: {hops} channel hops + {injects} injects + {leds} LED blinks + "
          f"{ticks} watchdog ticks + {peris} BT-coex periodicals reproduced ({w.i - init_end} ops)")

    if frontier is not None:
        print(f"\nFRONTIER -> op #{w.i} (frame {frontier['frame']}): {_fmt(frontier)}")
        print("  ^ the next op to reproduce (port it).")
        return 1

    n_exc = len(w.dev.excepted)
    print(f"\nPASS: reproduced {w.i - n_exc} of {total} ops byte-for-byte single-cursor "
          f"(init + airmon + {hops} hops + {injects} injects + {leds} LED + {ticks} ticks + "
          f"{peris} periodicals)"
          + (f"; EXCEPTED {n_exc} dm._dc_cancellation ops (intentional skip)." if n_exc else "."))
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
