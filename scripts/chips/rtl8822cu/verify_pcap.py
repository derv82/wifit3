"""Byte-for-byte replay-diff of the rtl8822cu port vs the vendor rtl88x2cu cold-boot capture.

The 8822cu transport is dev-centric: every register access is one bRequest 0x05 control
transfer, and (ON-section registers only) the vendor follows it with a 1-byte echo write to
0x4e0 [SRC os_dep/linux/usb_ops_linux.c:172-203]. That echo lives in the transport, below the
read8/write8 surface, so the replay runs at the ctrl_transfer layer: extract_ctrl_ops +
extract_bulk_out_ops merged by frame, driven through rtw88_pcap_replay.ReplayDevice. One
monotonic cursor walks the whole capture: the deterministic cold init runs via the driver's
``_bringup``, then the operational phase dispatches each interleaved burst (monitor arm, channel
tune, phydm watchdog tick) to its real driver handler at the cursor, distinguished by a unique
opener op; TX frames (MGMT queue) are skipped. A frontier inside ``airmon-ng stop`` replays the teardown
(``airmon-ng stop`` + ``rmmod``) through ``_close``. The first op that opens no handler is the
frontier. Modelled on the sibling rtl8821cu_dkms recipe (same 0x4e0 family, same ReplayDevice engine).

Captures (D-Link AC13U 2001:3329): capture-1 sweeps ch1-12 + 5G, capture-2 adds ch13/ch14.
capture-3 is unusable: its usbmon file is truncated (1 bulk-IN completion), so the CFG_PARAM ACK
wait during bring up starves and the walk stops at op 4721, before the operational phase.
The teardown and reload replays need a capture from the current capture.py, which records both.

    uv run python scripts/porting/verify_pcap.py rtl8822cu [capture-N]
    uv run python scripts/chips/rtl8822cu/verify_pcap.py [capture-N | path/to/capture-N.pcap]

Exit code is 0 only on full reproduction. Any frontier or divergence exits nonzero: a
divergence is the next op to port, a call to action, not a win. The final line names the
capture command the frontier sits in (e.g. still inside ``sudo airmon-ng start wlan0``).
"""
from __future__ import annotations

import asyncio
import bisect
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import rtw88_pcap_replay as rp
import usb.util
from wifit3.chips.rtl8822cu import dack_cache, dm, driver, watchdog
from wifit3.chips.rtl8822cu.driver import RTL8822CUDriver, _DEFAULT_CHANNEL
from wifit3.chips.rtl8822cu.phy import MASKDWORD, get_bb_reg, set_bb_reg, switch_channel
from wifit3.chips.rtl8822cu.tx import TX_DESC_QSEL_MGMT

CAP_DIR = REPO / "driver_captures" / "captures_rtl88x2cu"


def _dig_init_without_igi_start(t, dm_state) -> None:
    """dm.dig_init minus the port's IGI 0x1C start; phydm_dig_init only reads IGI
    [SRC phydm_dig.c:1036-1050]."""
    dm_state.cur_ig_value = get_bb_reg(t, 0x1D70, 0x0000007F)
    set_bb_reg(t, 0x1E80, MASKDWORD, 0x55005500)


# Waived port-only writes the vendor build doesn't make: the IGI 0x1C start, and the TX LED
# blink (CONFIG_RTW_LED is off [SRC include/autoconf.h:178]).
dm.dig_init = _dig_init_without_igi_start
driver.enable_tx_blink = lambda t: None
driver.disable_tx_blink = lambda t: None

# Operational-dispatch openers (each the unique first op of a vendor handler at the cursor):
_OP_HOP = 0x3C60         # IN: path-A RF18 read opens a channel tune
_OP_TICK = 0x0210        # IN: TXDMA-status read opens a phydm dynamic-check watchdog tick
_OP_UNLOAD = 0x0086      # IN: check_ips_status read opens rtw_dev_unload [SRC rtl8822c_ops.c:905]

_RX_BULK_IN_EP = 0x84    # the RTL8822CU RX endpoint (rx_pkt_desc + frame, and in-band C2H)


def _fmt(op: dict) -> str:
    if op.get("dir") == "BULK":
        return f"BULK[{len(op['data'])}B]"
    d = op.get("data", b"")
    val = f"=0x{int.from_bytes(d, 'little'):0{max(len(d) * 2, 2)}x}" if d else ""
    return f"{op['dir']} 0x{op['wval']:04x}/{op['width']}{val}"


class Walk:
    """One ctrl_transfer cursor over the whole capture, driving the real driver. The harness
    invokes the shipped bringup (``_bringup`` / ``_monitor_entry`` for cold init, ``set_channel``
    per hop) so the bytes it verifies ARE the shipped path, not a parallel reimplementation."""

    def __init__(self, ops: list[dict], responses: list[bytes] | None = None):
        self.ops = ops
        self.dev = rp.ReplayDevice(ops, responses=responses)
        self.driver = RTL8822CUDriver(self.dev)
        self.t = self.driver.transport

    @property
    def i(self) -> int:
        return self.dev.i

    def peek(self) -> dict | None:
        return self.ops[self.dev.i] if self.dev.i < len(self.ops) else None


def _peek_channel(ops: list[dict], i: int, window: int = 40) -> int | None:
    """The channel a tune targets is a runtime input (airodump/iw choose it); read it from the
    upcoming path-A RF18 write (0x3c60). RF 0x18[7:0] holds the channel number."""
    for o in ops[i:i + window]:
        if o.get("dir") == "OUT" and o.get("wval") == _OP_HOP and o.get("width") == 4:
            return o["data"][0]
    return None


def _frontier(w: Walk, label: str, e: Exception) -> dict | None:
    kind = "DIVERGED" if isinstance(e, rp.Divergence) else "ERROR"
    print(f"\n  {label} {kind} at op {w.i}:\n    {type(e).__name__}: {e}")
    return w.peek()


def _walk_operational(w: Walk):
    """Dispatch each operational burst to its handler at the cursor. Channel hops
    (``driver.set_channel``) and phydm watchdog ticks (``watchdog.tick``) interleave, each opened by
    a unique op. The first op that opens no handler STOPS the walk and is returned as the frontier."""
    wd_st = watchdog.WatchdogState.from_phydm(w.driver.phydm)
    hops = ticks = 0
    while w.i < len(w.ops):
        o = w.peek()
        if o.get("dir") == "IN" and o.get("wval") == _OP_HOP:
            ch = _peek_channel(w.ops, w.i)
            if ch is None:
                break
            try:
                switch_channel(w.driver.transport, ch, w.driver.txpwr)
                w.driver._current_channel = ch
                w.driver.current_band_is_2g = ch <= 14
            except Exception as e:  # noqa: BLE001
                return hops, ticks, _frontier(w, f"hop ch{ch}", e)
            hops += 1
            continue
        if o.get("dir") == "IN" and o.get("wval") == _OP_TICK:
            try:
                watchdog.tick(w.t, wd_st, w.driver._current_channel)
            except Exception as e:  # noqa: BLE001
                return hops, ticks, _frontier(w, f"tick #{ticks + 1}", e)
            ticks += 1
            continue
        break
    return hops, ticks, w.peek()


def _walk_teardown(w: Walk) -> None:
    """Replay ``rtw_dev_unload`` through the driver's shipped ``_close`` at the cursor. The pyusb
    release/dispose calls are host-side (no transfer on the wire), so they're stubbed."""
    w.driver._claimed = True
    usb.util.release_interface = lambda *a, **k: None
    usb.util.dispose_resources = lambda *a, **k: None
    asyncio.run(w.driver._close())


def _pump_rx(w: Walk) -> tuple[int, int, int]:
    """Drive the shipped RX path over the captured bulk-IN FIFO, PAST the TX-inject frontier.

    The RX stream is a decoupled FIFO (``ReplayDevice.read``), not byte-locked to the OUT
    cursor, so it is pumped once the OUT walk has stopped: each ``_rx_read_once`` pops one
    recorded EP-0x84 completion (an rx_pkt_desc + 802.11 frame, or an in-band C2H event) and
    ``_rx_dispatch`` runs the driver's real decode -- ``rx.iter_bulk_frames`` -> the 802.11
    parser -> the RX callback. A drained FIFO reads as a timeout (None) and ends the loop.
    Returns (reads, frames_parsed, beacons). This exercises the RX/C2H path the OUT-only walk
    never reached; it does not touch the OUT cursor, so the pre-frontier verification stands."""
    parsed: list = []
    w.driver._bulk_in_ep = _RX_BULK_IN_EP
    w.driver.register_rx_callback(parsed.append)
    reads = 0
    while True:
        buf = w.driver._rx_read_once()
        if buf is None:
            break
        reads += 1
        w.driver._rx_dispatch(buf)
    beacons = sum(1 for p in parsed if getattr(p, "type", None) == "beacon")
    rssis = [p.rssi for p in parsed if getattr(p, "rssi", None) is not None]
    return reads, len(parsed), beacons, rssis


def _rssi_histogram(rssis: list[int]) -> str:
    """Compact RSSI distribution for the pumped RX frames: explicit 0 dBm and -100 tallies
    (the pre-fix saturation pile and the undecoded-phy-type pile) plus 10 dBm buckets."""
    if not rssis:
        return "  RX RSSI: no frames carried a decoded RSSI"
    at_zero = sum(1 for r in rssis if r == 0)
    at_unknown = sum(1 for r in rssis if r == -100)
    at_floor = sum(1 for r in rssis if r == -120)
    buckets: dict[int, int] = {}
    for r in rssis:
        b = (r // 10) * 10
        buckets[b] = buckets.get(b, 0) + 1
    bars = "  ".join(f"[{b}..{b + 9}]={buckets[b]}" for b in sorted(buckets, reverse=True))
    return (f"  RX RSSI over {len(rssis)} frames: min={min(rssis)} max={max(rssis)} dBm; "
            f"=0dBm:{at_zero}  =-100(no phy status):{at_unknown}  =-120(floor):{at_floor}\n    {bars}")


def _phase_at(pcap: Path, cap_name: str, frame: int):
    """Map a frontier frame number to the capture command it falls within, read from the
    recorded main.log. Returns (passed, current, remaining) as lists of command labels, or
    None if the log or the frame epoch is unavailable. Uses the same epoch to phase bisect
    as pcap_slicer, so the two tools agree on the boundaries."""
    from pcap_slicer import parse_log

    log = pcap.parent / f"{cap_name}_logs" / "main.log"
    if not log.exists():
        return None
    cmds = parse_log(str(log))
    if not cmds:
        return None
    nums, eps = rp.frame_epochs(pcap)
    if not nums:
        return None
    epoch = eps[min(bisect.bisect_left(nums, frame), len(nums) - 1)]
    cur = -1
    for idx, c in enumerate(cmds):
        if c["epoch"] <= epoch:
            cur = idx
        else:
            break
    passed = [c["cmd"] for c in cmds[:cur]] if cur >= 0 else []
    current = cmds[cur]["cmd"] if cur >= 0 else None
    remaining = [c["cmd"] for c in (cmds[cur + 1:] if cur >= 0 else cmds)]
    return passed, current, remaining


def _print_phase(pcap: Path, cap_name: str, frame: int) -> None:
    """One compact line naming the capture command the frontier op sits in, plus how many
    commands ran before and remain. Deliberately a single line (no full command dump) to keep
    a porting agent's context lean across repeated runs. The full list is in pcap_slicer."""
    try:
        ctx = _phase_at(pcap, cap_name, frame)
    except Exception as e:  # noqa: BLE001 -- phase context is diagnostic, never fail the run over it
        print(f"  (capture phase unavailable: {type(e).__name__}: {e})")
        return
    if ctx is None:
        return
    passed, current, remaining = ctx
    where = current or "<before the first logged command>"
    print(f"  capture phase: {where}  [{len(passed)} done, {len(remaining)} to go, @frame {frame}]")


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None       # replay needs no settle delays

    name = Path(cap).stem if cap else "capture-1"
    pcap = Path(cap) if cap and cap.endswith(".pcap") else CAP_DIR / f"{name}.pcap"
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1

    dev_addr = rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev_addr)
    ctrl = rp.extract_ctrl_ops(pcap, dev_addr)
    bulk = rp.extract_bulk_out_ops(pcap, dev_addr)
    bulk_in = rp.extract_bulk_in_ops(pcap, dev_addr)
    # Waived: TX frames (MGMT queue in the TX descriptor; FW download and H2C use other queues).
    # They interleave with register ops at any point, and the capture holds no inject_frame
    # input (frame, rate) to drive the port with.
    fw = [o for o in bulk if (o["data"][5] & 0x1F) != TX_DESC_QSEL_MGMT]
    ops = rp.merge_ops_by_frame(ctrl, fw)
    total = len(ops)
    print(f"{pcap.name}: card=dev{dev_addr}, {len(ctrl)} control + {len(bulk)} bulk-OUT ops"
          f" ({len(bulk) - len(fw)} TX frames skipped) + {len(bulk_in)} bulk-IN RX completions")

    w = Walk(ops, responses=bulk_in)
    # The BB/RF FW-offload now blocks per batch on its CFG_PARAM_ACK C2H, read off the RX bulk-IN
    # FIFO. Point the driver at the RX endpoint so those reads pop the captured acks (the first 24
    # bulk-IN completions) before _pump_rx drains the frames that follow them.
    w.driver._bulk_in_ep = _RX_BULK_IN_EP
    try:
        w.driver._bringup(w.driver.transport)
        w.driver._monitor_entry()
        w.driver._current_channel = _DEFAULT_CHANNEL
    except rp.Divergence as e:
        print(f"\nFAIL (cold-init/monitor-entry divergence) after {w.i} ops:\n  {e}")
        if w.i < total:
            print(f"  frontier op #{w.i} (frame {ops[w.i]['frame']}): {_fmt(ops[w.i])}")
            _print_phase(pcap, name, ops[w.i]["frame"])
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR (harness/port bug) after {w.i} ops: {type(e).__name__}: {e}")
        if w.i < total:
            print(f"  frontier op #{w.i} (frame {ops[w.i]['frame']}): {_fmt(ops[w.i])}")
            _print_phase(pcap, name, ops[w.i]["frame"])
        return 2
    print(f"  bringup: reproduced {w.i} ops single-cursor (cold init + monitor entry)")
    if not w.driver._dack_restored:     # _connect caches the DACK after a successful bring-up
        dack_cache.put(w.driver._dack_cache_key, w.driver.dack)

    while True:
        hops, ticks, frontier = _walk_operational(w)
        print(f"  operational: {hops} channel tunes + {ticks} watchdog ticks")
        phase = _phase_at(pcap, name, frontier["frame"]) if frontier is not None else None
        if phase is None or phase[1] is None or not phase[1].startswith(("sudo airmon-ng stop", "sudo rmmod")):
            break
        # The disconnect and managed-mode ops before rtw_dev_unload are skipped: wifit3 never
        # leaves monitor mode.
        unload = next((j for j in range(w.i, len(w.ops))
                       if w.ops[j].get("dir") == "IN" and w.ops[j].get("wval") == _OP_UNLOAD), None)
        if unload is None:
            break
        skipped = unload - w.i
        w.dev.i = unload
        try:
            _walk_teardown(w)
            print(f"  teardown: {skipped} disconnect/managed-mode ops skipped, "
                  f"_close reproduced {w.i - unload} ops")
        except Exception as e:  # noqa: BLE001
            _frontier(w, "teardown _close", e)
            frontier = w.peek()
            break
        frontier = w.peek()
        phase = _phase_at(pcap, name, frontier["frame"]) if frontier is not None else None
        if phase is None or phase[1] is None or not phase[1].startswith("sudo modprobe"):
            break
        # Driver reload: the app builds a new driver per connect; the DACK cache is process-wide.
        start = w.i
        w.driver = RTL8822CUDriver(w.dev)
        w.t = w.driver.transport
        w.driver._bulk_in_ep = _RX_BULK_IN_EP
        # The reload's FW ACKs follow its own start in the bulk-IN stream; drop the earlier session's RX.
        w.dev.responses = rp.extract_bulk_in_ops(pcap, dev_addr, window=(frontier["frame"], 1 << 31))
        w.dev.resp_i = 0
        try:
            w.driver._bringup(w.driver.transport)
            w.driver._monitor_entry()
            w.driver._current_channel = _DEFAULT_CHANNEL
        except Exception as e:  # noqa: BLE001
            _frontier(w, "reload bringup", e)
            frontier = w.peek()
            break
        print(f"  reload: reproduced {w.i - start} ops (bring-up + monitor entry, "
              f"DACK {'restored' if w.driver._dack_restored else 'calibrated'})")

    reads, rx_parsed, beacons, rssis = _pump_rx(w)
    print(f"  RX stream (past frontier): pumped {reads}/{len(bulk_in)} captured bulk-IN "
          f"completions -> {rx_parsed} 802.11 frames parsed, {beacons} beacons decoded")
    print(_rssi_histogram(rssis))

    if frontier is not None:
        print(f"\nFRONTIER -> op #{w.i} (frame {frontier['frame']}): {_fmt(frontier)}")
        print("  ^ the next op to reproduce (port it).")
        _print_phase(pcap, name, frontier["frame"])
        return 1                # a frontier is a call to action, not a pass; only full reproduction exits 0
    print(f"\nPASS: full single-cursor reproduction of {w.i}/{total} ops.")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
