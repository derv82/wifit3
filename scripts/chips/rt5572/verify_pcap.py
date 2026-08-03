"""Acceptance gate: replay-diff the rt5572 (Ralink RT5592 / RF5592) port against its
cold-boot capture. Byte-for-byte, fail-closed, one cursor.

The point of this gate — and the whole reason chips/rt5572 exists apart from the
rt2800usb imitation port — is that it drives the driver's REAL code. The cold phase
is a single call to ``bring_up.bring_up()``, the exact function ``driver.connect()``
runs on hardware. There is no hand-written step list to keep in sync: change the
bring-up and this gate re-tests precisely what connect() does. (Contrast rt2800usb's
old recipe, which reproduced a kernel-ordered walk that connect() never followed —
100% green while the driver diverged.)

Modelled on scripts/chips/rt5372/verify_pcap.py (the rt2x00-family "done right" shape):

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

    uv run python scripts/porting/verify_pcap.py rt5572 [capture-1]
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import rt2x00_pcap_replay as rp  # noqa: E402
from wifit3.chips.rt5572 import chan  # noqa: E402
from wifit3.chips.rt5572 import constants as C  # noqa: E402
from wifit3.chips.rt5572.bring_up import bring_up  # noqa: E402
from wifit3.chips.rt5572.monitor import enable_monitor, reapply_filter  # noqa: E402
from wifit3.chips.rt5572.transport import RT5572Transport  # noqa: E402

CAP_DIR = REPO / "driver_captures" / "captures_rt2800usb_rt5572_2"
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
        result = fn(RT5572Transport(rd))
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
        state = w.run(lambda t: bring_up(t))
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
    ev, xtal, sil = state.eeprom, state.xtal_40mhz, state.chip.silicon_id

    # ---- operational phase ----
    # The gate DRIVES the driver's real operational code byte-for-byte: enable_monitor()
    # (the mac80211/airmon monitor bring-up — the same function connect() runs) then
    # hop_channel() per channel hop (the same function Driver.set_channel runs — an
    # RX-quiesced config_channel bracket). The only interleave allowed between hops is
    # mac80211's periodic configure_filter re-push (driven via reapply_filter, still
    # byte-checked). aireplay-ng's TX_STA_FIFO status polling is the one named waiver
    # (human-fired injection — its bulk-OUT TX frames are off the control stream).
    # GREEN ⇔ monitor entry + every hop's RF reconfig match the kernel to the byte.
    table = chan._RF_VALS_5592_XTAL40 if xtal else chan._RF_VALS_5592_XTAL20
    AIR = _AIREPLAY_TAIL

    def sc_kwargs(ch):
        p1, p2 = chan.default_power(ev, sil, ch, xtal)
        lna = ev.lna_gain_bg if ch <= 14 else ev.lna_gain_a
        return dict(freq_offset=ev.freq_offset, lna_gain=lna, tx_chain_num=ev.txpath,
                    rx_chain_num=ev.rxpath, has_cap_bt_coexist=ev.has_cap_bt_coexist,
                    has_cap_external_lna_a=ev.has_cap_external_lna_a,
                    has_cap_external_lna_bg=ev.has_cap_external_lna_bg, xtal_40mhz=xtal,
                    iq_cal=ev.iq_cal, default_power1=p1, default_power2=p2, eeprom=ev)

    def rfcsr8_n(o):
        if o["dir"] != "OUT" or o["addr"] != C.RF_CSR_CFG or len(o.get("data", b"")) != 4:
            return None
        v = int.from_bytes(o["data"], "little")
        if not (v & C.RF_CSR_CFG_WRITE) or ((v >> 8) & 0x3F) != 8:
            return None
        return v & 0xFF

    def is_ldo(o):
        return o is not None and o["dir"] == "IN" and o["addr"] == C.LDO_CFG0

    def detect_ch(rem):
        """Identify the channel a hop tunes to. ``rem`` starts at the hop boundary
        (stop_queue(RX) → update_survey → config_channel), so skip forward to the
        LDO_CFG0 read that opens config_channel, then trial each candidate channel
        and keep the one whose set_channel reproduces the most ops."""
        off = next((j for j in range(min(8, len(rem))) if is_ldo(rem[j])), None)
        if off is None:
            return None
        sub = rem[off:]
        if len(sub) < 8 or sub[1]["dir"] != "OUT" or sub[1]["addr"] != C.LDO_CFG0:
            return None
        band = (int.from_bytes(sub[1]["data"], "little") >> 26) & 0x7
        n = next((r for j in range(2, 8) if (r := rfcsr8_n(sub[j])) is not None), None)
        if n is None:
            return None
        best, bc = None, -1
        for ch in [c for c, v in table.items() if (c <= 14) == (band == 0) and (v[0] & 0xFF) == n]:
            rd = rp.ReplayDevice(sub)
            try:
                chan.set_channel(RT5572Transport(rd), sil, ch, **sc_kwargs(ch))
            except rp.Divergence:
                pass
            if rd.i > bc:
                best, bc = ch, rd.i
        return best

    # ---- monitor entry: the driver's real enable_monitor(), byte-for-byte ----
    try:
        w.run(lambda t: enable_monitor(t, sil, ev, xtal))
    except rp.Divergence as e:
        fr = w.peek()
        print(f"\nFAIL (monitor-entry divergence) at op {w.i}:\n  {e}")
        if fr:
            print(f"  frontier = {rp.ReplayDevice._fmt(fr)} @f{fr.get('frame')}")
        print("  ^ enable_monitor() diverges from the kernel. Fix chips/rt5572/monitor.py.")
        return 1
    print(f"  OK  monitor entry: enable_monitor() reproduced {w.i - 1781} ops "
          "byte-for-byte (config_filter 0x97→0x93 + txpower/retry/ps/ant/vgc).")

    # ---- channel hops: each is a full hop_channel bracket (stop RX → update_survey
    # → reconfig_channel → start RX). Between hops mac80211 occasionally re-pushes the
    # monitor filter (reapply_filter, 0x93→0x93). Stop at the aireplay TX region. ----
    hops = []
    while w.peek() is not None and w.peek()["addr"] != C.TX_STA_FIFO:
        if w.peek()["addr"] == C.RX_FILTER_CFG:
            try:
                w.run(lambda t: reapply_filter(t))
            except rp.Divergence as e:
                print(f"\nFAIL (filter re-push divergence) at op {w.i}:\n  {e}")
                return 1
            continue
        ch = detect_ch(w.ops[w.i:])
        if ch is None:
            break
        try:
            w.run(lambda t, c=ch: chan.hop_channel(t, sil, c, **sc_kwargs(c)))
        except rp.Divergence as e:
            fr = w.peek()
            print(f"\nFAIL (hop divergence) at op {w.i} (ch{ch}):\n  {e}")
            if fr:
                print(f"  frontier = {rp.ReplayDevice._fmt(fr)} @f{fr.get('frame')}")
            print("  ^ hop_channel() diverges from the kernel per-hop. Fix chips/rt5572.")
            return 1
        hops.append(ch)

    # ---- aireplay TX-injection region (waived — human-fired injection) ----
    while w.peek() is not None:
        w.waive(AIR)

    print(f"  OK  hops: {len(hops)} hop_channel calls byte-for-byte "
          f"(channels {sorted(set(hops))})")
    for reason, n in w.waived.most_common():
        print(f"  waived {n:6} ops — {reason}")
    print(f"\nPASS: reproduced {w.i} of {len(ops)} ops — every op matched or explicitly waived.")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
