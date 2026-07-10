"""Acceptance gate: ONE monotonic single-cursor walk of the rtl8814au_dkms cold-boot capture.

Fail-closed, never short-circuited. One cursor runs from the driver's first vendor op
(the probe chip-version read) to the end of the capture; every op has exactly one honest
fate as the cursor advances:

  * matched       — the port's real handler reproduces it byte-for-byte at the cursor.
  * unaccounted   — anything else STOPS the walk and names the op: the porting frontier,
                    the next op to reproduce. PASS <=> zero unaccounted.

There is **no legitimate waiver** — every op reproduce-or-fail to the last frame: the whole
control conversation (init + airmon STA->monitor dance + airodump hops + the phydm
dynamic-check watchdog) AND the aireplay-ng bulk-OUT injections. Two capture sets are wired:
``usb_dumps_new`` (2.4 GHz aireplay, the default) and ``usb_dumps_new2`` (5 GHz injection,
select with a ``new2/`` prefix). Both PASS 100% byte-for-byte.

We do not run airmon/airodump/iw against our port; the chip only sees register writes, so
the vendor-driver writes those tools trigger are ours to reproduce. wifit3 is the trigger:
the bring-up + airmon dance is the deterministic init walk; the operational phase dispatches
each burst to the real handler at the cursor — a channel hop (set_channel_bw), a dynamic-check
tick (the sreset poll + phydm watchdog), or a frame injection (update_txdesc + bulk-OUT) —
distinguished by a unique opener. The LED blink and injection run on their own timers, so they
splice into a mid-flight handler; ``_drain_async`` drains each at the cursor.

    uv run python scripts/rtl8814au_dkms/verify_pcap.py [[new2/]capture-N]
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

# Two capture sets. Select the 5 GHz one with a "new2/" prefix (e.g. "new2/capture-1");
# a bare capture name defaults to the 2.4 GHz set.
CAP_DIRS = {
    "new":  REPO / "usb_dumps_new"  / "captures_rtl8814au",   # Kali 6.18, 2.4 GHz aireplay
    "new2": REPO / "usb_dumps_new2" / "captures_rtl8814au",   # Kali 6.19 (this VM), 5 GHz injection
}
# dev-addr per (set, capture); anything absent falls back to find_card_device(pcap).
DEV_ADDR = {
    ("new", "capture-1"): 51, ("new", "capture-2"): 53, ("new", "capture-3"): 54,
}

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


def _peek_txrate(desc: bytes) -> tuple[int, int]:
    """An injected frame's (rate_id, tx_rate) are runtime inputs — aireplay-ng picks them per
    frame via radiotap, so read them back from the recorded descriptor the way _peek_channel
    reads the tune target. update_txdesc's RATE_ID is word1[20:16] and TX_RATE is word4[6:0]
    [SRC rtl8814au_xmit.c:106,232]; the driver rebuilds the other 38 bytes from the frame."""
    rate_id = (int.from_bytes(desc[4:8], "little") >> 16) & 0x1F
    hw_rate = int.from_bytes(desc[16:20], "little") & 0x7F
    return rate_id, hw_rate


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

    def _drain_async(t, addr):
        """Two independent producers run on their own timers, so the wire splices them into
        whatever handler is mid-flight (a tune's txagc burst, an IQK one-shot, a watchdog tick):
        the LED blink (0x0060 R/W pair, ~2 s cadence) and aireplay's frame injection (a bulk-OUT).
        Drain each at the cursor through its real handler so the host handler resumes byte-aligned.
        Skips 0x0060 itself so led_blink's own ops don't re-enter this."""
        nonlocal leds, injects
        if addr == _OP_LED:
            return
        while t.i < len(t.ops):
            nxt = t.ops[t.i]
            if nxt["kind"] == "R" and nxt.get("addr") == _OP_LED:
                watchdog.led_blink(t, st)
                leds += 1
            elif nxt["kind"] == "B":
                rec = bytes(nxt["data"])
                rate_id, hw_rate = _peek_txrate(rec[:C.TXDESC_SIZE])
                driver._inject(t, rec[C.TXDESC_SIZE:], hw_rate=hw_rate, rate_id=rate_id)
                injects += 1
            else:
                break

    while w.i < len(w.ops):
        o = w.peek()
        if o["kind"] == "R" and o.get("addr") == _OP_HOP:
            ch = _peek_channel(w.ops, w.i)
            if ch is None:
                break
            try:
                # driver._tune = set_channel_bw + on_channel_switch, the body set_channel runs;
                # it advances driver._current_band / driver._channel just like the live hop.
                w.run(lambda t, c=ch: driver._tune(t, c), f"hop{ch}", interleave=_drain_async)
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
                      f"tick#{ticks + 1}", interleave=_drain_async)
            except (rp.Divergence, Exception) as e:  # noqa: BLE001
                return hops, ticks, leds, injects, _frontier(w, o, f"tick #{ticks + 1}", e)
            ticks += 1
            continue
        if o["kind"] == "B":
            # aireplay TX injection: the wire's bulk-OUT is [40-byte TX desc | 802.11 frame].
            # Feed the frame to driver._inject (the body inject_frame runs) and check the
            # rebuilt [desc | frame] against the wire — verifies our update_txdesc descriptor.
            # rate_id/tx_rate are aireplay's per-frame radiotap picks (a Null rides VHT MCS9,
            # deauths ride CCK 1M), read back from the recorded desc like _peek_channel.
            rec = bytes(o["data"])
            frame = rec[C.TXDESC_SIZE:]
            rate_id, hw_rate = _peek_txrate(rec[:C.TXDESC_SIZE])
            try:
                w.run(lambda t, f=frame, r=rate_id, hr=hw_rate:
                      driver._inject(t, f, hw_rate=hr, rate_id=r), f"inject#{injects + 1}")
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
    raw = cap or "capture-1"
    setkey, _, rest = raw.partition("/")     # optional "<set>/<capture>" selector
    if rest and setkey in CAP_DIRS:
        cap_dir, name = CAP_DIRS[setkey], Path(rest).stem
    else:
        setkey, cap_dir, name = "new", CAP_DIRS["new"], Path(raw).stem
    pcap = cap_dir / f"{name}.pcap"
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1
    dev = DEV_ADDR.get((setkey, name)) or rp.find_card_device(pcap)
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
          f"capture byte-for-byte (init + airmon + {hops} hops + {ticks} watchdog ticks + "
          f"{injects} injections).")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
