"""Acceptance gate: ONE monotonic single-cursor walk of the rtl8814au_dkms cold-boot capture.

Fail-closed, never short-circuited. One cursor runs from the driver's first vendor op
(the probe chip-version read) to the end of the capture; every op has exactly one honest
fate as the cursor advances:

  * matched       — the port's real handler reproduces it byte-for-byte at the cursor.
  * unaccounted   — anything else STOPS the walk and names the op: the porting frontier,
                    the next op to reproduce. PASS <=> zero unaccounted.

This capture has **no aireplay-ng injection** (every bulk-OUT is a firmware-download
packet in frames 6145-6667), so there is **no legitimate waiver** — the whole control
conversation, init + airmon STA->monitor dance + airodump hops + the phydm dynamic-check
watchdog, is reproduce-or-fail to the last frame.

We do not run airmon/airodump/iw against our port; the chip only sees register writes, so
the vendor-driver writes those tools trigger are ours to reproduce. wifit3 is the trigger:
the bring-up + airmon dance is the deterministic init walk; the operational phase dispatches
each burst to the real handler at the cursor — a channel hop (set_channel_bw) or a
dynamic-check tick (the sreset poll + phydm watchdog) — distinguished by a unique opener.

    uv run python scripts/rtl8814au_dkms/verify_pcap.py [capture-N[.pcap]]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8814au_dkms import monitor, watchdog  # noqa: E402
from wifit3.chips.rtl8814au_dkms import constants as C  # noqa: E402
from wifit3.chips.rtl8814au_dkms.driver import Rtl8814auDkmsDriver  # noqa: E402

CAP_DIR = REPO / "usb_dumps_new" / "captures_rtl8814au"
DEV_ADDR = {"capture-1": 51, "capture-2": 53, "capture-3": 54}

INIT_CHANNEL = 1                 # the cold-boot connect-time tune target
_ANCHOR_ADDR = C.REG_SYS_CFG1    # 0xF0 — read_chip_version, the first vendor op
# Operational-dispatch openers (each the unique first op of a vendor handler at the cursor):
_OP_HOP = C.REG_CCK_CHECK        # 0x0454 R: phy_SwBand8814A band-marker read opens a tune
_OP_LED = 0x0060                 # R: SwLedOn/Off_8814AU — the LED blink (a separate producer)
_OP_TICK = 0x0210                # R: rtl8814_sreset_xmit_status_check opens a dynamic-check tick
_REG_RF_CHNL_A = 0x0C90          # path-A RF_CHNLBW MMIO write — carries the tuned channel


class Walk:
    """One cursor over the whole capture. ``run`` drives a real port handler at the cursor
    against the recorded wire; it advances by exactly the ops the handler consumed."""

    def __init__(self, ops: list[dict]):
        self.ops = ops
        self.i = 0

    def run(self, fn, label: str, interleave=None):
        t = rp.ReplayTransport(self.ops[self.i:])
        if interleave is not None:
            t.interleave = lambda addr: interleave(t, addr)
        try:
            result = fn(t)
        except rp.Divergence:
            self.i += max(t.i - 1, 0)   # the diverging op did not match; credit only matched ops
            raise
        except BaseException:
            self.i += t.i               # a port/harness error consumed t.i matched ops
            raise
        self.i += t.i
        return result

    def peek(self) -> dict | None:
        return self.ops[self.i] if self.i < len(self.ops) else None


def _peek_channel(ops: list[dict], i: int, window: int = 120) -> int | None:
    """The channel a tune targets is a runtime input (airodump/iw choose it); read it from
    the wire's first path-A RF_CHNLBW write in the upcoming burst. RF reg 0x18[7:0] holds the
    channel number (1..165 all fit a byte; the band/sub-band bits sit higher)."""
    for o in ops[i:i + window]:
        if o["kind"] == "W" and o.get("addr") == _REG_RF_CHNL_A:
            return o["value"] & 0xFF
    return None


def _walk_bringup(w: Walk) -> Rtl8814auDkmsDriver:
    """Cold bring-up through the SHARED driver method — the gate exercises ``driver._bringup``,
    the exact sequence ``connect()`` runs (EFUSE -> firmware -> MAC/BB/RF -> tune -> InitHalDm ->
    RFE-true -> turn-on -> airmon STA dance), so a change to the driver's bring-up can't silently
    drift from the gate. ``enter_monitor`` is the one op ``connect()`` defers past the bulk-IN
    reader (RX-FIFO ordering), so it runs here as its own step. The driver is constructed with no
    transport — ``_bringup``/``_tune`` take the ReplayTransport as ``t``, so nothing touches live
    USB; the instance just carries the same band / channel / TX-power / watchdog state it does live.
    """
    driver = Rtl8814auDkmsDriver(None)
    w.run(driver._bringup, "bringup")                     # the exact connect() cold register path
    w.run(monitor.enter_monitor, "monitor")               # deferred RX gate (post-reader in connect)
    return driver


def _walk_operational(w: Walk, driver: Rtl8814auDkmsDriver) -> tuple[int, int, int, dict | None]:
    """Dispatch each operational burst to the real driver method at the cursor. Three producers
    interleave: channel hops (``driver._tune`` — the body ``set_channel`` runs), the LED blink
    (carries an ON/OFF phase), and the dynamic-check tick (``watchdog.tick`` — the DIG watchdog's
    body, carrying DM state). The driver instance carries the band / channel / watchdog state
    across bursts exactly as it does live. The first op opening no wired handler STOPS the walk."""
    st = driver._wd_state
    hops = ticks = leds = injects = 0

    def _drain_led(t, addr):
        """The LED-blink timer (0x0060) runs on its own ~2 s cadence, so the wire splices its
        R/W pair into whatever handler is mid-flight (a tune's txagc burst, an IQK one-shot). Drain
        each interleaved blink through the real led_blink at the cursor so the handler resumes
        byte-aligned. Skips 0x0060 itself so led_blink's own ops don't re-enter this."""
        nonlocal leds
        if addr == _OP_LED:
            return
        while (t.i < len(t.ops) and t.ops[t.i]["kind"] == "R"
               and t.ops[t.i].get("addr") == _OP_LED):
            watchdog.led_blink(t, st)
            leds += 1

    while w.i < len(w.ops):
        o = w.peek()
        if o["kind"] == "R" and o.get("addr") == _OP_HOP:
            ch = _peek_channel(w.ops, w.i)
            if ch is None:
                break
            try:
                # driver._tune = set_channel_bw + on_channel_switch, the body set_channel runs;
                # it advances driver._current_band / driver._channel just like the live hop.
                w.run(lambda t, c=ch: driver._tune(t, c), f"hop{ch}", interleave=_drain_led)
            except (rp.Divergence, Exception) as e:  # noqa: BLE001
                return hops, ticks, leds, injects, _frontier(w, o, f"hop ch{ch}", e)
            hops += 1
            continue
        if o["kind"] == "R" and o.get("addr") == _OP_LED:
            try:
                w.run(lambda t: watchdog.led_blink(t, st), f"led#{leds + 1}")
            except (rp.Divergence, Exception) as e:  # noqa: BLE001
                return hops, ticks, leds, injects, _frontier(w, o, f"led #{leds + 1}", e)
            leds += 1
            continue
        if o["kind"] == "R" and o.get("addr") == _OP_TICK:
            try:
                w.run(lambda t: watchdog.tick(t, st, driver._channel),
                      f"tick#{ticks + 1}", interleave=_drain_led)
            except (rp.Divergence, Exception) as e:  # noqa: BLE001
                return hops, ticks, leds, injects, _frontier(w, o, f"tick #{ticks + 1}", e)
            ticks += 1
            continue
        if o["kind"] == "B":
            # aireplay TX injection: the wire's bulk-OUT is [40-byte TX desc | 802.11 frame].
            # Feed the frame to driver._inject (the body inject_frame runs) and check the
            # rebuilt [desc | frame] against the wire — verifies our update_txdesc descriptor.
            frame = bytes(o["data"])[C.TXDESC_SIZE:]
            try:
                w.run(lambda t, f=frame: driver._inject(t, f), f"inject#{injects + 1}")
            except (rp.Divergence, Exception) as e:  # noqa: BLE001
                return hops, ticks, leds, injects, _frontier(w, o, f"inject #{injects + 1}", e)
            injects += 1
            continue
        break  # frontier: unknown opener
    return hops, ticks, leds, injects, w.peek()


def _frontier(w: Walk, o: dict, label: str, e: Exception) -> dict:
    kind = type(e).__name__
    print(f"\n  {label} {'DIVERGED' if isinstance(e, rp.Divergence) else 'ERROR'} "
          f"at op {w.i}:\n    {kind}: {e}")
    # w.run credits the ops reproduced before the stop, so the cursor now sits on the first
    # unaccounted op (e.g. a partially-ported watchdog tick that reaches an unported sub-step).
    return w.peek() or o


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None        # replay needs no real settle delays
    name = Path(cap or "capture-1").stem
    pcap = CAP_DIR / f"{name}.pcap"
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1
    dev = DEV_ADDR.get(name) or rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev)

    full = rp.extract_ops(pcap, dev)
    anchor = next((j for j, o in enumerate(full)
                   if o["kind"] == "R" and o.get("addr") == _ANCHOR_ADDR and o["width"] == 4),
                  None)
    if anchor is None:
        print("FAIL: no REG_SYS_CFG1/4 read (read_chip_version) in capture")
        return 1
    ops = full[anchor:]
    total = len(ops)
    print(f"{pcap.name}: card=dev{dev}, {len(full)} vendor ops -> walk {total} from op {anchor}")

    w = Walk(ops)
    try:
        driver = _walk_bringup(w)
        print(f"  bring-up: reproduced {w.i} ops single-cursor via driver._bringup "
              f"(efuse -> turn-on -> airmon -> monitor, no gaps)")
    except rp.Divergence as e:
        print(f"\nFAIL (bring-up divergence) at op {w.i}:\n  {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR (harness/port bug) at op {w.i}: {type(e).__name__}: {e}")
        return 2

    hops, ticks, leds, injects, frontier = _walk_operational(w, driver)
    print(f"  operational: {hops} channel hops + {ticks} dynamic-check ticks + {leds} LED "
          f"blinks + {injects} TX injections reproduced")

    if frontier is not None:
        fa = frontier
        desc = (f"{fa['kind']} 0x{fa.get('addr', 0):04x}/{fa.get('width', '?')}"
                f"=0x{fa.get('value', 0):x}" if fa["kind"] != "B" else "bulk")
        print(f"\nFRONTIER: reproduced {w.i} of {total} ops; first unaccounted op "
              f"@{w.i} = {desc} (frame {fa.get('frame')})")
        print("  ^ the next op to reproduce (port it).")
        return 1

    print(f"\nPASS: reproduced all {w.i} of {total} ops single-cursor — entire cold-boot "
          f"capture byte-for-byte (init + airmon + {hops} hops + {ticks} watchdog ticks).")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
