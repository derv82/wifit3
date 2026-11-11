"""Replay-diff gate: the shipped RT3572Driver against the mainline rt2800usb noair captures.
One cursor; each main.log command drives a driver call; exit 0 only on a full match.

    uv run python scripts/porting/verify_pcap.py rt3572 [capture-N | path/to/capture-N.pcap]
"""
from __future__ import annotations

import asyncio
import bisect
import enum
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import rt2x00_pcap_replay as rp
from pcap_slicer import parse_log
from wifit3.chips.rt3572 import firmware
from wifit3.chips.rt3572.constants import (
    H2M_MAILBOX_CSR, HOST_CMD_CSR, MCU_LED, MCU_LED_ACT_CONF, MCU_LED_AG_CONF,
    MCU_LED_LED_POLARITY, RX_FILTER_CFG, TX_STA_FIFO, USB_MULTI_READ, USB_MULTI_WRITE,
)
from wifit3.chips.rt3572.driver import RT3572Driver

CAP_DIR = REPO / "driver_captures" / "captures_rt2800usb_rt3572_noair"

MCU_LED_STRENGTH = 0x51     # rt2800.h:3028; the port has no LED-quality path
LED_COMMANDS = (MCU_LED, MCU_LED_STRENGTH, MCU_LED_AG_CONF, MCU_LED_ACT_CONF,
                MCU_LED_LED_POLARITY)

# mac80211's dflt_chandef: first channel of the first registered band.
MAC80211_DEFAULT_CHANNEL = 1


def _reads(op: dict, addr: int) -> bool:
    return op["dir"] == "IN" and op["breq"] == USB_MULTI_READ and op["addr"] == addr


def _writes(op: dict, addr: int) -> bool:
    return op["dir"] == "OUT" and op["breq"] == USB_MULTI_WRITE and op["addr"] == addr


def _led_mcu_request(ops: list[dict], i: int) -> int:
    j = i
    while j < len(ops) and _reads(ops[j], H2M_MAILBOX_CSR):
        j += 1
    if j == i or j + 1 >= len(ops):
        return 0
    if not (_writes(ops[j], H2M_MAILBOX_CSR) and _writes(ops[j + 1], HOST_CMD_CSR)):
        return 0
    if ops[j + 1]["data"][0] not in LED_COMMANDS:
        return 0
    return j + 2 - i


def _tx_status_read(ops: list[dict], i: int) -> int:
    return 1 if _reads(ops[i], TX_STA_FIFO) else 0


def _filter_written_back(ops: list[dict], i: int) -> int:
    if (i + 1 < len(ops) and _reads(ops[i], RX_FILTER_CFG)
            and _writes(ops[i + 1], RX_FILTER_CFG) and ops[i + 1]["data"] == ops[i]["data"]):
        return 2
    return 0


@dataclass(frozen=True)
class Waiver:
    name: str
    reason: str
    match: Callable[[list[dict], int], int]


WAIVERS = (
    Waiver("LED mailbox command",
           "[IN H2M_MAILBOX_CSR]+ OUT H2M_MAILBOX_CSR OUT HOST_CMD_CSR=MCU_LED*: rt2x00leds "
           "triggers and the enable_radio LED config (rt2800lib.c:1598-1653, 10863-10874, "
           "rt2x00dev.c:69-70/111-112). LED behaviour is outside the gate.",
           _led_mcu_request),
    Waiver("TX status read",
           "IN TX_STA_FIFO: rt2800usb's txstatus hrtimer after each TX URB "
           "(rt2800usb.c:143-170); wifit3 reads no TX status.",
           _tx_status_read),
    Waiver("RX filter written back unchanged",
           "IN RX_FILTER_CFG, OUT RX_FILTER_CFG of the same value: mac80211 reconfig_filter "
           "when aireplay-ng opens its socket, FIF_* flags unchanged (mac80211 main.c:87-93, "
           "rt2800lib.c:1967).",
           _filter_written_back),
)
TX_URB_WAIVER = ("aireplay-ng TX URBs",
                 "bulk-OUT not compared: aireplay-ng and mac80211 choose the descriptor (rate, "
                 "ACK, seq, duration), which inject_frame() does not take as input.")
PORT_LED_WAIVER = ("port MCU_LED* requests",
                   "not sent: the port's LED commands are outside the gate, like the kernel's.")


def _absorb(ops: list[dict], i: int) -> tuple[str, int] | None:
    for w in WAIVERS:
        n = w.match(ops, i)
        if n:
            return w.name, n
    return None


class Step(enum.Enum):
    NEW_DRIVER = "new RT3572Driver"
    BRINGUP = "driver._bringup()"
    SET_CHANNEL = "driver.set_channel(1), driver.set_channel(N)"
    CHANNEL_REJECTED = "none (iw failed, the kernel tuned nothing)"
    AIREPLAY = "none (TX URBs waived)"
    CLOSE = "driver.close()"


@dataclass(frozen=True)
class Action:
    step: Step
    command: str
    channel: int = 0


def _iw_results(iw_log: Path) -> list[tuple[int, int]]:
    """(channel, return code) per ``iw ... set channel`` in iw.log order."""
    out: list[tuple[int, int]] = []
    channel = None
    for line in iw_log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.search(r"Executing: .*\biw dev \S+ set channel (\d+)\s*$", line)
        if m:
            channel = int(m.group(1))
            continue
        m = re.search(r"return code: (-?\d+)", line)
        if m and channel is not None:
            out.append((channel, int(m.group(1))))
            channel = None
    return out


def _session(log_dir: Path) -> list[Action]:
    """The capture's commands in main.log order, each mapped to the driver call it implies."""
    iw = _iw_results(log_dir / "iw.log")
    actions: list[Action] = []
    for line in (log_dir / "main.log").read_text(encoding="utf-8", errors="replace").splitlines():
        if "INSERT THE USB CARD NOW" in line:
            actions.append(Action(Step.NEW_DRIVER, "card plug-in"))
            continue
        m = re.search(r"\] Running: (.*)$", line)
        if not m:
            continue
        cmd = m.group(1).strip()
        tune = re.search(r"\biw dev \S+ set channel (\d+)$", cmd)
        if tune:
            channel, rc = iw.pop(0)
            if channel != int(tune.group(1)):
                raise SystemExit(f"iw.log out of step with main.log at: {cmd}")
            step = Step.SET_CHANNEL if rc == 0 else Step.CHANNEL_REJECTED
            actions.append(Action(step, cmd, channel))
        elif "airmon-ng start" in cmd:
            actions.append(Action(Step.BRINGUP, cmd))
        elif "aireplay-ng" in cmd:
            actions.append(Action(Step.AIREPLAY, "aireplay-ng"))
        elif "airmon-ng stop" in cmd or re.search(r"\brmmod\b", cmd):
            actions.append(Action(Step.CLOSE, cmd))
        elif re.search(r"\bmodprobe\b", cmd):
            actions.append(Action(Step.NEW_DRIVER, cmd))
        else:
            raise SystemExit(f"main.log command with no driver action: {cmd}")
    return actions


class Walk:
    def __init__(self, ops: list[dict]):
        self.dev = rp.ReplayDevice(ops, absorb=_absorb)
        self.driver: RT3572Driver | None = None
        self.done = 0
        self.release_tunes = 0
        self.port_led_requests = 0


async def _run_action(w: Walk, a: Action) -> None:
    if a.step is Step.NEW_DRIVER:
        w.driver = RT3572Driver(w.dev)
    elif a.step is Step.BRINGUP:
        await w.driver._bringup(lambda pct, msg: None)
    elif a.step is Step.SET_CHANNEL:
        for ch in (MAC80211_DEFAULT_CHANNEL, a.channel):
            if not await w.driver.set_channel(ch):
                raise RuntimeError(f"driver.set_channel({ch}) returned False")
        w.release_tunes += 1
    elif a.step is Step.CLOSE:
        await w.driver.close()


async def _walk(w: Walk, actions: list[Action]) -> None:
    for a in actions:
        await _run_action(w, a)
        w.dev.drain()
        if w.dev.divergence is not None:      # raised inside the driver and swallowed there
            raise w.dev.divergence
        w.done += 1


def _step_at(pcap: Path, log_dir: Path, frame: int) -> str:
    cmds = parse_log(str(log_dir / "main.log"))
    nums, eps = rp.frame_epochs(pcap)
    if not cmds or not nums:
        return "?"
    epoch = eps[min(bisect.bisect_left(nums, frame), len(nums) - 1)]
    k = bisect.bisect_right([c["epoch"] for c in cmds], epoch) - 1
    return cmds[k]["cmd"] if k >= 0 else "<before the first logged step>"


def _resolve(cap: str) -> Path:
    p = Path(cap)
    return p if p.suffix == ".pcap" else CAP_DIR / f"{p.stem}.pcap"


def _gate(pcap: Path) -> int:
    log_dir = pcap.parent / f"{pcap.stem}_logs"
    if not pcap.exists() or not log_dir.is_dir():
        print(f"FAIL: missing {pcap} or {log_dir}")
        return 1
    card = rp.find_card_device(pcap)
    audit_ok = rp.audit_coverage(pcap, card)
    ops = rp.extract_ops(pcap, card)
    tx_urbs = rp.extract_bulk_out(pcap, card)
    actions = _session(log_dir)
    rejected = [a.channel for a in actions if a.step is Step.CHANNEL_REJECTED]
    print(f"{pcap.name}: card dev{card}, {len(ops)} control ops, {len(tx_urbs)} bulk-OUT URBs, "
          f"{len(actions)} main.log actions (iw rejected: {rejected or 'none'})")

    w = Walk(ops)
    real_mcu_request = firmware.mcu_request

    def mcu_request_without_led(t, command, token=0, arg0=0, arg1=0):
        if command in LED_COMMANDS:
            w.port_led_requests += 1
            return
        real_mcu_request(t, command, token, arg0, arg1)

    firmware.mcu_request = mcu_request_without_led
    error = None
    try:
        asyncio.run(_walk(w, actions))
    except rp.Divergence:
        pass
    except Exception as e:  # noqa: BLE001 -- a port exception ends the walk; reported below
        error = e
    finally:
        firmware.mcu_request = real_mcu_request

    dev = w.dev
    complete = (error is None and dev.divergence is None and w.done == len(actions)
                and dev.i == len(ops))
    if not complete:
        current = actions[w.done] if w.done < len(actions) else None
        where = f"{current.command} -> {current.step.value}" if current else "after the last action"
        print(f"\nFRONTIER at op {dev.i}, action {w.done + 1}/{len(actions)}: {where}")
        if dev.divergence is not None:
            print(f"  {dev.divergence}")
        elif error is not None:
            print(f"  {type(error).__name__}: {error}")
        else:
            print("  capture ops remain after the last action")
        if dev.i < len(ops):
            op = ops[dev.i]
            print(f"  capture op: {rp.ReplayDevice._fmt(op)}, main.log step: "
                  f"{_step_at(pcap, log_dir, op['frame'])}")

    frontier_frame = ops[dev.i]["frame"] if dev.i < len(ops) else None
    tx_waived = sum(1 for u in tx_urbs if frontier_frame is None or u["frame"] < frontier_frame)
    waived = sum(dev.waived_ops.values())
    print(f"\nops: {dev.matched} matched by the driver, {waived} waived, "
          f"{len(ops) - dev.i} not reached, of {len(ops)}")
    print(f"actions completed: {w.done}/{len(actions)} "
          f"(mac80211 release retunes to ch{MAC80211_DEFAULT_CHANNEL} driven: {w.release_tunes})")
    print("waivers:")
    for wv in WAIVERS:
        print(f"  {wv.name}: {dev.waived_hits[wv.name]} hits, {dev.waived_ops[wv.name]} ops. "
              f"{wv.reason}")
    print(f"  {TX_URB_WAIVER[0]}: {tx_waived} of {len(tx_urbs)}. {TX_URB_WAIVER[1]}")
    print(f"  {PORT_LED_WAIVER[0]}: {w.port_led_requests}. {PORT_LED_WAIVER[1]}")
    if not audit_ok:
        print("FAIL: the coverage audit found a blind spot")
        return 1
    if not complete:
        print("FAIL")
        return 1
    print("PASS")
    return 0


def run(cap: str | None = None, verbose: bool = False) -> int:
    if not verbose:
        logging.getLogger("wifit3").setLevel(logging.CRITICAL)
    real_sleep, time.sleep = time.sleep, lambda *a, **k: None
    try:
        pcaps = [_resolve(cap)] if cap else sorted(CAP_DIR.glob("capture-*.pcap"))
        if not pcaps:
            print(f"FAIL: no captures in {CAP_DIR}")
            return 1
        results = [_gate(p) for p in pcaps]
        return max(results)
    finally:
        time.sleep = real_sleep


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--verbose"]
    return run(args[0] if args else None, verbose="--verbose" in sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
