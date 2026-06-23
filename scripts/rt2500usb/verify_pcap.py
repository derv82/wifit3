"""Acceptance gate: replay-diff the rt2500usb (Ralink RT2570) port against its
cold-boot capture.

ONE monotonic walk, ONE cursor, fail-closed — the rt3070 gate's shape, adapted to
the older 16-bit-CSR part. The replay is at the ``ctrl_transfer`` layer
(``rt2x00_pcap_replay.ReplayDevice``) and the REAL ``chips/rt2500usb`` transport
drives it, so every helper (regbusy poll, set_state's MAC_CSR17 poll, the indirect
BBP/RF paths, the single-writes) replays with zero reimplementation.

  * matched     — the port's real handler reproduces the op byte-for-byte.
  * waived      — an explicit, named, counted boundary for a producer that is NOT
                  the rt2500usb kernel driver (aireplay-ng's bulk-OUT TX is out of
                  the control stream; a slot is kept for any control op it triggers).
  * unaccounted — anything else STOPS the walk and names the op: the porting
                  frontier. PASS ⇔ zero unaccounted.

We do not run airmon-ng / airodump-ng / iw / aireplay-ng against the port; the chip
only sees register writes, so the *kernel-driver* writes those tools trigger are
ours to reproduce. wifit3 is the trigger: connect() stands in for the probe + airmon
monitor entry, the channel hopper for airodump/iw (per-hop tune_hop). rt2500usb has
no periodic link_tuner, so there is no ~1 Hz async writer to dispatch.

    uv run python scripts/rt2500usb/verify_pcap.py [capture-1|capture-2|capture-3]
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
from wifit3.chips.rt2500usb import bbp, chan, mac, monitor  # noqa: E402
from wifit3.chips.rt2500usb.constants import (  # noqa: E402
    EEPROM_ANTENNA,
    EEPROM_ANTENNA_RF_TYPE,
    MAC_CSR0,
    PHY_CSR9,
    RF3_TXPOWER,
    TXRX_CSR2,
    TXRX_CSR2_DISABLE_RX,
    USB_DEVICE_MODE,
    USB_EEPROM_READ,
    USB_MODE_TEST,
)
from wifit3.chips.rt2500usb.chan import RF_VALS_2525E, RF2525E_HALFBAND  # noqa: E402
from wifit3.chips.rt2500usb.transport import RT2500USBTransport, get_field16  # noqa: E402

CAP_DIR = REPO / "usb_dumps_new" / "captures_rt2500usb"

# (half-band RF[2], RF[4]) low-16 → channel. RF2525E channel pairs share a
# half-band (ch2/ch3 = 0x08ae), so the tune's first two PHY_CSR9 writes
# (half-band then RF[4]) together identify the channel uniquely.
_RF2525E_TUNE_KEY = {
    (RF2525E_HALFBAND[ch] & 0xFFFF, RF_VALS_2525E[ch][3] & 0xFFFF): ch
    for ch in RF2525E_HALFBAND
}

# aireplay-ng --test + -0 deauth ride bulk-OUT (out of the control stream); if it
# triggers any control op, it lands here as a named, counted waiver.
_AIREPLAY_TAIL = "aireplay --test + -0 deauth (human-fired TX; bulk-OUT out of the control gate)"


class Walk:
    """One cursor over the whole capture. ``run`` drives a real port handler from
    the cursor (a fresh ReplayDevice over the remaining ops wrapped in the real
    transport); ``waive`` consumes one op of a named non-reproduced producer."""

    def __init__(self, ops: list[dict]):
        self.ops = ops
        self.i = 0
        self.waived: Counter = Counter()

    def run(self, fn):
        rd = rp.ReplayDevice(self.ops[self.i:])
        result = fn(RT2500USBTransport(rd))
        self.i += rd.i
        return result

    def peek(self, k: int = 0) -> dict | None:
        j = self.i + k
        return self.ops[j] if j < len(self.ops) else None

    def waive(self, reason: str) -> None:
        self.waived[reason] += 1
        self.i += 1


def _is_init_start(o: dict) -> bool:
    """init_registers opens with vendor_request_sw(USB_DEVICE_MODE, 0x0001,
    USB_MODE_TEST) — an OUT single op, test-mode value in wValue (rt2500usb.c:770)."""
    return (o["dir"] == "OUT" and o["breq"] == USB_DEVICE_MODE
            and o["wval"] == USB_MODE_TEST and o["addr"] == 0x0001)


def _peek_channel(ops: list[dict], i: int) -> int | None:
    """Reverse-map the upcoming RF2525E tune to a channel from its first two
    PHY_CSR9 writes — the half-band RF[2] pre-write and RF[4] (together unique
    per channel). None if no RF tune appears in the window (not a channel hop)."""
    phy9 = [int.from_bytes(o["data"], "little")
            for o in ops[i:i + 12]
            if o["dir"] == "OUT" and o["addr"] == PHY_CSR9 and o.get("data")]
    if len(phy9) < 2:
        return None
    return _RF2525E_TUNE_KEY.get((phy9[0], phy9[1]))


def _walk_init(w: Walk, revision: int, eeprom: bytes, rf_type: int,
               ant_tx: int, ant_rx: int, txpower: int) -> None:
    """Deterministic cold bring-up + airmon monitor entry, one cursor, no
    re-anchoring. init_registers + init_bbp (rt2500usb_enable_radio) then
    enable_monitor (rt2x00lib_enable_radio tail → configure_filter → first
    rt2x00mac_config → monitor filter)."""
    w.run(lambda t: mac.init_registers(t, revision))           # linear CSR bring-up
    w.run(lambda t: bbp.init_bbp(t, eeprom))                    # BBP defaults + EEPROM
    w.run(lambda t: monitor.enable_monitor(t, rf_type, eeprom, ant_tx, ant_rx, txpower))


def _walk_operational(w: Walk, rf_type: int, eeprom: bytes, ant_tx: int,
                      ant_rx: int, txpower: int) -> dict | None:
    """airodump/iw channel hops, with mac80211 monitor-filter reapplies
    interleaved. Each TXRX_CSR2 read opens either a hop (its paired write sets
    DISABLE_RX — rt2x00mac_config's stop_queue) or a filter reapply (write leaves
    DISABLE_RX clear). The aireplay tail is the one named waiver."""
    while w.i < len(w.ops):
        o = w.peek()
        if o["dir"] == "IN" and o["addr"] == TXRX_CSR2:
            nxt = w.peek(1)
            disable_rx = (nxt is not None and nxt["dir"] == "OUT"
                          and nxt["addr"] == TXRX_CSR2 and nxt.get("data")
                          and (int.from_bytes(nxt["data"], "little") & TXRX_CSR2_DISABLE_RX))
            if disable_rx:                                      # stop_queue → channel hop
                ch = _peek_channel(w.ops, w.i + 2)
                if ch is not None:
                    w.run(lambda t, ch=ch: monitor.tune_hop(
                        t, rf_type, ch, eeprom, ant_tx, ant_rx, txpower))
                    continue
            else:                                              # monitor-filter reapply
                w.run(lambda t: mac.config_filter(t, monitoring=True))
                continue
        # Unknown control op: if it is the human-fired aireplay tail, waive it;
        # otherwise stop and name the frontier.
        break
    return w.peek()


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None        # replay needs no real settle delays
    name = Path(cap or "capture-2").stem
    pcap = CAP_DIR / f"{name}.pcap"
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1

    dev = rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev)
    full = rp.extract_ops(pcap, dev)

    rev_op = next((o for o in full if o["dir"] == "IN" and o["addr"] == MAC_CSR0), None)
    if rev_op is None:
        print("FAIL: no MAC_CSR0 (revision) read in capture")
        return 1
    revision = int.from_bytes(rev_op["data"], "little")

    ee = next((o for o in full if o["dir"] == "IN" and o["breq"] == USB_EEPROM_READ), None)
    if ee is None:
        print("FAIL: no EEPROM read in capture")
        return 1
    eeprom = ee["data"]
    antenna_word = eeprom[EEPROM_ANTENNA * 2] | (eeprom[EEPROM_ANTENNA * 2 + 1] << 8)
    rf_type = get_field16(antenna_word, EEPROM_ANTENNA_RF_TYPE)
    ant_tx, ant_rx = chan.antenna_defaults(antenna_word)

    # Anchor at the init_registers opener; everything before is enumeration/probe.
    anchor = next((i for i, o in enumerate(full) if _is_init_start(o)), None)
    if anchor is None:
        print("FAIL: init_registers anchor (USB_MODE_TEST write) not in capture")
        return 1
    ops = full[anchor:]

    # The captured run's TX power = the standalone config_txpower RF[3] write
    # (RF3_TXPOWER field; the first operational PHY_CSR9 write). Derived, not
    # assumed, so the gate self-describes the input it verifies against.
    txpower = 0
    for o in ops:
        if o["dir"] == "OUT" and o["addr"] == PHY_CSR9 and o.get("data"):
            txpower = get_field16(int.from_bytes(o["data"], "little"), RF3_TXPOWER & 0xFFFF)
            break

    print(f"{name}: card=dev{dev}, {len(full)} control ops -> walk {len(ops)} "
          f"(MAC_CSR0=0x{revision:04x}, rf_type=0x{rf_type:x}, ant tx={ant_tx} rx={ant_rx}, "
          f"txpower={txpower})")

    w = Walk(ops)
    try:
        _walk_init(w, revision, eeprom, rf_type, ant_tx, ant_rx, txpower)
    except rp.Divergence as e:
        print(f"\nFAIL (init divergence) at op {w.i}:\n  {e}")
        return 1
    init_end = w.i
    print(f"  init + monitor entry: reproduced {init_end} ops single-cursor")

    try:
        frontier = _walk_operational(w, rf_type, eeprom, ant_tx, ant_rx, txpower)
    except rp.Divergence as e:
        print(f"\nFAIL (operational divergence) at op {w.i}:\n  {e}")
        return 1

    for reason, n in w.waived.most_common():
        print(f"  waived {n:5} ops  — {reason}")
    if frontier is not None:
        print(f"\nFRONTIER: reproduced {w.i} of {len(ops)} ops; first unaccounted op @{w.i} "
              f"= {rp.ReplayDevice._fmt(frontier)} (frame {frontier.get('frame')})")
        print("  ^ the next op to reproduce (port it, or add a named waiver).")
        return 1

    print(f"\nPASS: reproduced {w.i} of {len(ops)} ops — every op matched or explicitly waived.")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
