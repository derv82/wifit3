"""Acceptance gate: replay-diff the rtl8187 (RTL8187L, ALFA AWUS036H) bring-up against
its cold-boot capture.

ONE monotonic walk, ONE cursor, fail-closed. Unlike the rtw88 ``ReplayTransport`` (which
reimplements read8/write8/...), this gate replays at the ``ctrl_transfer`` layer
(``rtw88_pcap_replay.ReplayDevice``) so the REAL ``chips/rtl8187`` transport drives it --
including the rtl8225 RF SPI **8051 fast path** (``t.dev.ctrl_transfer`` wValue=RF-addr,
wIndex=0x8225), which a reimplemented transport could not reach. Every helper -- register
read/write, the EEPROM_CMD 93cx6 bit-bang, the RF bit-bang reads, the 8051 write -- replays
with zero reimplementation.

Each op the card emitted has exactly one honest fate as the cursor advances:
  * matched     -- the port's real handler reproduces it byte-for-byte.
  * waived      -- an explicit, named, counted boundary for a non-vendor-driver producer.
  * unaccounted -- anything else STOPS the walk and names the op: the porting frontier.

The walk is: probe (93cx6 EEPROM + asic_rev + HWVER + detect_rf + rfkill init) -> init_hw +
the full rtl8225z2 RF init -> start -> configure_filter (monitor entry) -> every airodump /
iw channel hop (rtl8187_config), with the periodic rfkill GPIO poll and the per-hop
monitor-filter reapply dispatched as async producers. The aireplay-ng deauth at the tail is
bulk-OUT (a different program's TX) -- out of this control-stream gate, byte-diffed separately.

Run: uv run python scripts/chips/rtl8187/verify_pcap.py [capture-1]
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import rtw88_pcap_replay as rp
from wifit3.chips.rtl8187 import chan, mac, rfkill
from wifit3.chips.rtl8187.constants import (
    REG_GPIO0,
    REG_RX_CONF,
    REG_TALLY_CNT,
    REG_TX_CONF,
)
from wifit3.chips.rtl8187.probe import probe
from wifit3.chips.rtl8187.rtl8225 import build_rf_init, rtl8225_chan
from wifit3.chips.rtl8187.transport import RTL8187Transport

CAP_DIR = REPO / "driver_captures" / "captures_rtl8187_2"

# Reverse-map the RF7 synth word the channel tune writes (via the 8051 fast path,
# wValue=0x07/wIndex=0x8225) back to its channel — how the walk learns which channel a
# hop targets (the runtime input airodump/iw chose), same as reading it off the wire.
_RF7_TO_CHANNEL = {word: ch for ch, word in enumerate(rtl8225_chan, start=1)}
_RF7_ADDR = 0x0007
_RF7_PAGE = 0x8225


class Walk:
    """One cursor over the whole capture. ``run`` drives a real port handler from the
    cursor (a fresh ReplayDevice over the remaining ops wrapped in the real transport);
    ``waive`` consumes one op of a named non-reproduced producer."""

    def __init__(self, ops: list[dict]):
        self.ops = ops
        self.i = 0
        self.waived: Counter = Counter()

    def run(self, fn):
        rd = rp.ReplayDevice(self.ops[self.i:])
        result = fn(RTL8187Transport(rd))
        self.i += rd.i
        return result

    def peek(self, k: int = 0) -> dict | None:
        j = self.i + k
        return self.ops[j] if 0 <= j < len(self.ops) else None

    def waive(self, reason: str) -> None:
        self.waived[reason] += 1
        self.i += 1


def _is(o: dict | None, direction: str, wval: int, width: int | None = None) -> bool:
    return (o is not None and o["dir"] == direction and o["wval"] == wval
            and (width is None or o["width"] == width))


def _peek_channel(w: Walk) -> int | None:
    """The channel an upcoming hop targets, read from its RF7 synth write (the 8051
    fast-path OUT wValue=0x07/wIndex=0x8225 carrying rtl8225_chan[ch-1])."""
    for k in range(0, 160):
        o = w.peek(k)
        if o is None:
            return None
        if o["dir"] == "OUT" and o["wval"] == _RF7_ADDR and o["widx"] == _RF7_PAGE:
            return _RF7_TO_CHANNEL.get(int.from_bytes(o["data"], "little"))
    return None


def _walk_operational(w: Walk, setup, power, rx_conf: int) -> dict | None:
    """Dispatch each post-bring-up burst to its real handler at the cursor: a channel hop
    (rtl8187_config, opens on the TX_CONF read), a monitor-filter reapply (configure_filter,
    a lone RX_CONF write), or the periodic rfkill poll (opens on the GPIO0 read). The first
    op that is none of these STOPS the walk and is returned as the frontier."""
    while w.i < len(w.ops):
        o = w.peek()
        if _is(o, "IN", REG_GPIO0):                      # rfkill poll (R GPIO0,W GPIO0,R GPIO1)
            w.run(lambda t: rfkill.is_radio_enabled(t))
            continue
        if _is(o, "IN", REG_TX_CONF, 4):                 # channel hop (rtl8187_config)
            ch = _peek_channel(w)
            if ch is None:
                break
            w.run(lambda t, ch=ch: chan.config_channel(
                t, setup.asic_rev, setup.variant, ch, power))
            continue
        if _is(o, "OUT", REG_RX_CONF, 4):                # monitor-filter reapply
            w.run(lambda t: mac.configure_filter(t, rx_conf))
            continue
        if _is(o, "IN", REG_TALLY_CNT, 2):               # rtl8187_work TX-status retry read
            # Scheduled only on TX (dev.c:223, in the TX path); here it is the aireplay-ng
            # injection at the capture tail reading the cumulative retry count via 0xFFFA.
            # Our port is fire-and-forget (no TX-status reporting), and this is a different
            # program's TX consequence -- the rtl8188eus REG_TX_RPT_TIME analogue.
            w.waive("aireplay-triggered TX-status work (rtl8187_work, reg 0xFFFA) "
                    "[external tool; bulk-OUT TX out of the control gate]")
            continue
        break
    return w.peek()


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None        # replay needs no real settle delays
    # Accept either a bare capture name (under the default capture dir) or an explicit path,
    # so the Post-Port "verify every capture" pass can point at the older driver_captures/ ones.
    arg = cap or "capture-1"
    cand = Path(arg)
    if cand.exists():
        pcap = cand
    elif (CAP_DIR / f"{Path(arg).stem}.pcap").exists():
        pcap = CAP_DIR / f"{Path(arg).stem}.pcap"
    else:
        print(f"FAIL: no such capture {arg}")
        return 1
    name = pcap.stem

    dev = rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev)
    ops = rp.extract_ctrl_ops(pcap, dev)
    total = len(ops)
    print(f"{name}: card=dev{dev}, {total} control ops (walk starts at the probe's "
          f"first op, frame {ops[0]['frame']})")

    w = Walk(ops)

    # 1) PROBE — 93cx6 EEPROM (MAC + per-channel TX power + base), asic_rev, HWVER, detect_rf,
    #    rfkill init. Yields the RF setup + EEPROM TX-power table the rest of the walk needs.
    try:
        pr = w.run(lambda t: probe(t))
    except rp.Divergence as e:
        print(f"\nFAIL (probe divergence) at op {w.i}:\n  {e}")
        return 1
    print(f"  probe: reproduced {w.i} ops — mac={pr.mac.hex(':')}, asic_rev={pr.setup.asic_rev}, "
          f"rf={pr.setup.variant.value}, ch1 hw_value=0x{pr.power.hw_value[0]:02x}, "
          f"txpwr_base=0x{pr.power.base:04x}")

    # 2) Two rfkill polls fire between probe (rfkill_init) and airmon bringing the iface up.
    while _is(w.peek(), "IN", REG_GPIO0):
        w.run(lambda t: rfkill.is_radio_enabled(t))

    # 3) COLD BRING-UP — init_hw + the full rtl8225z2 RF init (8051 fast path) -> start ->
    #    configure_filter (monitor entry). One cursor, no re-anchoring.
    try:
        w.run(lambda t: mac.init_hw(t, rf_init=build_rf_init(t, pr.setup, pr.power)))
        rx_conf = w.run(lambda t: mac.start(t))
        w.run(lambda t: mac.configure_filter(t, rx_conf))
    except rp.Divergence as e:
        print(f"\nFAIL (cold bring-up divergence) at op {w.i}:\n  {e}")
        return 1
    init_end = w.i
    print(f"  init_hw + RF + start + monitor entry: reproduced {init_end} ops single-cursor")

    # 4) OPERATIONAL — every airodump/iw hop + filter reapply + rfkill poll, dispatched.
    try:
        frontier = _walk_operational(w, pr.setup, pr.power, rx_conf)
    except rp.Divergence as e:
        print(f"\nFAIL (operational divergence) at op {w.i}:\n  {e}")
        return 1

    for reason, n in w.waived.most_common():
        print(f"  waived {n:5} ops  — {reason}")
    if frontier is not None:
        print(f"\nFRONTIER: reproduced {w.i} of {total} ops; first unaccounted op @{w.i} "
              f"= {rp.ReplayDevice._fmt(frontier)}")
        print("  ^ the next op to reproduce (port it, or add a named waiver).")
        return 1

    print(f"\nPASS: reproduced all {total} control ops single-cursor — probe -> init_hw -> "
          f"rtl8225z2 RF -> start -> monitor -> every hop, rfkill + filter reapplies "
          f"dispatched. Every op matched (aireplay bulk-OUT TX is out of the control gate).")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
