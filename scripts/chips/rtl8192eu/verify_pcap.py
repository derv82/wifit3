"""Verify the RTL8192EU port against a mainline rtl8xxxu usbmon capture.

One forward cursor over the captured control and bulk-OUT ops drives the driver's real _bringup(),
set_channel() and inject_frame(); every op must match byte-for-byte. Ops the driver does not emit
are waived by name and counted against the match; the first unmatched op is the frontier.

    uv run python scripts/porting/verify_pcap.py rtl8192eu [capture] [--verbose]
"""
from __future__ import annotations

import asyncio
import logging
import struct
import sys
from collections import Counter, namedtuple
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from wifit3.chips.rtl8192eu import SUPPORTED_IDS
from wifit3.chips.rtl8192eu import constants as C
from wifit3.chips.rtl8192eu.core import rtl8xxxu_watchdog_callback
from wifit3.chips.rtl8192eu.driver import RTL8192EUDriver
from wifit3.chips.rtl8192eu.rx import rtl8xxxu_parse_rxdesc24
from wifit3.chips.rtl8192eu.tx import RTL8XXXU_TXDESC40_SIZE
from wifit3.dot11.parser import WlanFrameParser

DEFAULT_CAP = "driver_captures/captures_rtl8xxxu_wn823n/capture-1.pcap"

# rtl8xxxu_config opens with set_tx_power's read of REG_TX_AGC_A_CCK1_MCS32 (8192e.c:764, core.c config).
_HOP_OPENER = C.REG_TX_AGC_A_CCK1_MCS32
# The 2 s watchdog (core.c rtl8xxxu_watchdog_callback -> rtl8xxxu_track_cfo) opens with either
# set_crystal_cap's read of REG_MAC_PHY_CTRL or set_atc_status's read of REG_OFDM1_CFO_TRACKING.
_WATCHDOG_OPENERS = (C.REG_MAC_PHY_CTRL, C.REG_OFDM1_CFO_TRACKING)

_OFF_TYPE, _OFF_XFER, _OFF_EP, _OFF_DEV, _OFF_LENCAP, _OFF_SETUP, _OFF_DATA = 8, 9, 10, 11, 36, 40, 64
_URB_SUBMIT, _URB_COMPLETE = 0x53, 0x43
_XFER_CTRL, _XFER_BULK = 0x02, 0x03
_MIN_RECORD = _OFF_SETUP + 8
_USB_REQ_GET_DESCRIPTOR = 0x06
_USB_DT_CONFIG_VALUE = 0x0200
_USB_DT_INTERFACE, _USB_DT_ENDPOINT = 4, 5


class Divergence(Exception):
    def __init__(self, msg: str, idx: int):
        super().__init__(msg)
        self.idx = idx


class CaptureExhausted(Divergence):
    """The port issued an op past the end of the capture: benign only if every capture op matched."""


Waiver = namedtuple("Waiver", "name why match")

WAIVERS = [
    Waiver("USB enumeration", "standard control requests usbcore issues while enumerating",
           lambda op: op["kind"] == "ctrl" and op["breq"] != C.REALTEK_USB_CMD_REQ),
]


def parse_pcapng(path: str) -> list[bytes]:
    data = Path(path).read_bytes()
    pkts, off = [], 0
    while off + 12 <= len(data):
        btype, blen = struct.unpack_from("<II", data, off)
        if blen < 12 or off + blen > len(data):
            break
        if btype == 0x00000006:
            cap_len = struct.unpack_from("<I", data, off + 8 + 12)[0]
            pkts.append(data[off + 8 + 20: off + 8 + 20 + cap_len])
        off += blen
    return pkts


def detect_dev(pkts: list[bytes]) -> int | None:
    """Devnum issuing the most Realtek register-access control transfers."""
    counts: Counter[int] = Counter()
    for pkt in pkts:
        if len(pkt) >= _MIN_RECORD and pkt[_OFF_XFER] == _XFER_CTRL and pkt[_OFF_TYPE] == _URB_SUBMIT \
                and pkt[_OFF_SETUP + 1] == C.REALTEK_USB_CMD_REQ:
            counts[pkt[_OFF_DEV]] += 1
    return counts.most_common(1)[0][0] if counts else None


def build_ops(pkts: list[bytes], dev: int) -> list[dict]:
    """Every control op and bulk-OUT op of the device, in capture order; a control read's value is
    stitched on from its COMPLETE record. Bulk-IN (RX) is not a driver op."""
    ops: list[dict] = []
    pending: dict[bytes, dict] = {}
    for frame, pkt in enumerate(pkts, start=1):
        if len(pkt) < _MIN_RECORD or pkt[_OFF_DEV] != dev:
            continue
        urb = bytes(pkt[0:8])
        if pkt[_OFF_XFER] == _XFER_CTRL:
            if pkt[_OFF_TYPE] == _URB_SUBMIT:
                wval, widx, wlen = struct.unpack_from("<HHH", pkt, _OFF_SETUP + 2)
                op = {"kind": "ctrl", "bmreq": pkt[_OFF_SETUP], "breq": pkt[_OFF_SETUP + 1],
                      "is_in": bool(pkt[_OFF_SETUP] & 0x80),
                      "wval": wval, "widx": widx, "wlen": wlen, "data": b"", "frame": frame}
                if op["is_in"]:
                    pending[urb] = op
                else:
                    dlen = min(wlen, len(pkt) - _OFF_DATA)
                    op["data"] = bytes(pkt[_OFF_DATA:_OFF_DATA + dlen]) if dlen > 0 else b""
                ops.append(op)
            elif pkt[_OFF_TYPE] == _URB_COMPLETE:
                op = pending.pop(urb, None)
                if op is not None:
                    dlen = min(op["wlen"], len(pkt) - _OFF_DATA)
                    op["data"] = bytes(pkt[_OFF_DATA:_OFF_DATA + dlen]) if dlen > 0 else b""
        elif pkt[_OFF_XFER] == _XFER_BULK and pkt[_OFF_TYPE] == _URB_SUBMIT and not (pkt[_OFF_EP] & 0x80):
            wlen = struct.unpack_from("<I", pkt, _OFF_LENCAP)[0]
            dlen = min(wlen, len(pkt) - _OFF_DATA)
            ops.append({"kind": "bulk", "breq": None, "ep": pkt[_OFF_EP], "wlen": wlen,
                        "data": bytes(pkt[_OFF_DATA:_OFF_DATA + dlen]), "frame": frame})
    return ops


def bulk_in_buffers(pkts: list[bytes], dev: int) -> list[bytes]:
    return [bytes(pkt[_OFF_DATA:]) for pkt in pkts
            if len(pkt) >= _MIN_RECORD and pkt[_OFF_DEV] == dev and pkt[_OFF_XFER] == _XFER_BULK
            and pkt[_OFF_TYPE] == _URB_COMPLETE and (pkt[_OFF_EP] & 0x80)]


def interface_endpoints(ops: list[dict]) -> list[SimpleNamespace]:
    """Interface 0 alt 0's endpoints from the captured configuration descriptor: what claim() gets
    from PyUSB, which _bringup() skips."""
    config = max((op["data"] for op in ops if op["kind"] == "ctrl" and op["is_in"]
                  and op["breq"] == _USB_REQ_GET_DESCRIPTOR and op["wval"] == _USB_DT_CONFIG_VALUE),
                 key=len, default=b"")
    endpoints = []
    in_interface_0 = False
    off = 0
    while off + 2 <= len(config) and config[off] > 0:
        length, dtype = config[off], config[off + 1]
        if dtype == _USB_DT_INTERFACE:
            in_interface_0 = config[off + 2] == 0 and config[off + 3] == 0
        elif dtype == _USB_DT_ENDPOINT and in_interface_0:
            endpoints.append(SimpleNamespace(bEndpointAddress=config[off + 2], bmAttributes=config[off + 3]))
        off += length
    return endpoints


def _op_addr(op: dict) -> int:
    return op["wval"]


def _waiver_for(op: dict) -> Waiver | None:
    return next((w for w in WAIVERS if w.match(op)), None)


def _fmt(op: dict) -> str:
    if op["kind"] == "bulk":
        return f"bulk-OUT ep=0x{op['ep']:02x} ({op['wlen']}B) {op['data'][:16].hex()}… @f{op['frame']}"
    tag = "" if op["breq"] == C.REALTEK_USB_CMD_REQ else f" breq=0x{op['breq']:02x}"
    if op["is_in"]:
        return f"read 0x{_op_addr(op):04x} ({op['wlen']}B) = {op['data'].hex()}{tag} @f{op['frame']}"
    return f"write 0x{_op_addr(op):04x} = {op['data'].hex()}{tag} @f{op['frame']}"


def _is_ctrl_read(op: dict, addr: int) -> bool:
    return op["kind"] == "ctrl" and op["is_in"] and op["breq"] == C.REALTEK_USB_CMD_REQ and _op_addr(op) == addr


def _is_ctrl_write(op: dict, addr: int) -> bool:
    return op["kind"] == "ctrl" and not op["is_in"] and op["breq"] == C.REALTEK_USB_CMD_REQ \
        and _op_addr(op) == addr


def _first_start_index(ops: list[dict]) -> int:
    """rtl8xxxu_start's REG_RXFLTMAP2 = 0xffff write: no async event happens before it (core.c start)."""
    return next((j for j, op in enumerate(ops)
                 if _is_ctrl_write(op, C.REG_RXFLTMAP2) and op["data"] == b"\xff\xff"), len(ops))


class _Bucket:
    def __init__(self, why: str):
        self.why = why
        self.frames: list[int] = []


class ReplayDev:
    """Stand-in for usb.core.Device over the op stream; each port op must match the next
    non-waived op or it raises Divergence."""

    def __init__(self, ops: list[dict]):
        self.ops = ops
        self.i = 0
        self.matched = 0
        self.waived_reg = 0
        self.waived: dict[str, _Bucket] = {}
        self.injected: Counter[str] = Counter()
        self.async_from = _first_start_index(ops)
        self.driver: RTL8192EUDriver | None = None
        self.first_divergence: Divergence | None = None
        self._injecting = False

    def _diverge(self, msg: str) -> Divergence:
        """The port's try/finally cleanup writes would replace the first divergence; keep it."""
        error = Divergence(f"op #{self.i - 1}: {msg}", self.i - 1)
        if self.first_divergence is None:
            self.first_divergence = error
        return error

    def _next(self) -> dict:
        while self.i < len(self.ops):
            op = self.ops[self.i]
            self.i += 1
            w = _waiver_for(op)
            if w is None:
                return op
            self.waived.setdefault(w.name, _Bucket(w.why)).frames.append(op["frame"])
        raise CaptureExhausted(f"port issued an op past the end of the capture (op #{self.i})", self.i)

    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex, data_or_wLength, timeout=None):
        is_in = bool(bmRequestType & 0x80)
        if not self._injecting:
            self._absorb_async(wValue, is_in)
        op = self._next()
        if op["kind"] != "ctrl":
            raise self._diverge(f"port issued a control transfer, wire has {_fmt(op)}")
        if bmRequestType != op["bmreq"] or bRequest != op["breq"] or wValue != op["wval"] or wIndex != op["widx"]:
            raise self._diverge(
                f"port {'read' if is_in else 'write'} 0x{wValue:04x} "
                f"(bmReq=0x{bmRequestType:02x} bReq=0x{bRequest:02x} wI=0x{wIndex:04x}), wire has "
                f"bmReq=0x{op['bmreq']:02x} {_fmt(op)}")
        if is_in:
            if int(data_or_wLength) != op["wlen"]:
                raise self._diverge(f"read width {int(data_or_wLength)} vs wire {_fmt(op)}")
        else:
            payload = bytes(data_or_wLength) if data_or_wLength else b""
            if payload != op["data"]:
                raise self._diverge(f"port write 0x{wValue:04x} = {payload.hex()}, wire has {_fmt(op)}")
        self.matched += 1
        return bytearray(op["data"]) if is_in else len(op["data"])

    def write(self, endpoint, data, timeout=None):
        if not self._injecting:
            self._absorb_async(None, False)
        op = self._next()
        payload = bytes(data)
        if op["kind"] != "bulk":
            raise self._diverge(f"port bulk-OUT ep=0x{endpoint:02x} ({len(payload)}B), wire has {_fmt(op)}")
        if endpoint != op["ep"] or payload != op["data"]:
            first = next((k for k in range(min(len(payload), len(op["data"])))
                          if payload[k] != op["data"][k]), min(len(payload), len(op["data"])))
            raise self._diverge(
                f"bulk-OUT ep=0x{endpoint:02x} {len(payload)}B vs wire ep=0x{op['ep']:02x} "
                f"{len(op['data'])}B, first differing byte {first} "
                f"(port {payload[first:first + 8].hex()} wire {op['data'][first:first + 8].hex()}) "
                f"@f{op['frame']}")
        self.matched += 1
        return len(payload)

    def read(self, endpoint: int, size: int, timeout: int | None = None) -> bytes:
        return b""

    def _absorb_async(self, incoming_addr: int | None, incoming_is_in: bool) -> None:
        """Run any async event the wire has queued at the cursor that the driver is not emitting itself:
        the 2 s watchdog (the driver's real tick) or a mac80211 configure_filter (waived by name)."""
        while True:
            j = self.next_register_op()
            if j is None or j < self.async_from:
                return
            op = self.ops[j]
            if op["kind"] != "ctrl" or op["breq"] != C.REALTEK_USB_CMD_REQ:
                return
            addr = _op_addr(op)
            if addr == incoming_addr and op["is_in"] == incoming_is_in:
                return
            before = self.i
            if op["is_in"] and addr in _WATCHDOG_OPENERS:
                self._run_injected("watchdog", rtl8xxxu_watchdog_callback)
            elif not op["is_in"] and addr == C.REG_RCR:
                self.i = j + 1
                self.waived_reg += 1
                self.waived.setdefault(
                    "mac80211 configure_filter",
                    _Bucket("mac80211 re-filters on interface and flag changes; wifit3 writes the monitor "
                            "filter once, in start_monitor_interface")).frames.append(op["frame"])
            else:
                return
            if self.i == before:
                return

    def _run_injected(self, name: str, handler) -> None:
        self._injecting = True
        try:
            self.injected[name] += 1
            handler(self.driver.priv)
        finally:
            self._injecting = False

    def next_register_op(self) -> int | None:
        j = self.i
        while j < len(self.ops):
            if _waiver_for(self.ops[j]) is None:
                return j
            j += 1
        return None

    def stack_trace(self, center: int, span: int = 10) -> str:
        lo, hi = max(0, center - span), min(len(self.ops), center + span + 1)
        lines = [f"    ... op {lo}..{hi - 1} of {len(self.ops)} (stopped at op {center}) ..."]
        for j in range(lo, hi):
            mark = " >>> " if j == center else "     "
            waived = "  [waived]" if _waiver_for(self.ops[j]) else ""
            lines.append(f"{mark}op {j:<6} {_fmt(self.ops[j])}{waived}")
        return "\n".join(lines)


def _peek_channel(ops: list[dict], start: int, window: int = 400) -> int | None:
    """The hop's target channel, a runtime input (airodump picks it): the first RF6052_REG_MODE_AG write
    on path A's LSSI (core.c rtl8xxxu_gen2_config_channel, rtl8xxxu_write_rfreg)."""
    for op in ops[start:start + window]:
        if _is_ctrl_write(op, C.REG_FPGA0_XA_LSSI_PARM) and len(op["data"]) == 4:
            dataaddr = struct.unpack("<I", op["data"])[0]
            if (dataaddr >> 20) & 0xFF == C.RF6052_REG_MODE_AG:
                return dataaddr & C.MODE_AG_CHANNEL_MASK
    return None


async def _drive(driver: RTL8192EUDriver, replay: ReplayDev, ops: list[dict]) -> Counter[str]:
    """_bringup() (connect() minus the interface claim), then real set_channel() hops and
    inject_frame() TX wherever the wire has them. Returns how many of each ran."""
    await driver._bringup(lambda _value, _message: None)
    await driver._stop_tasks()
    calls: Counter[str] = Counter()
    while True:
        j = replay.next_register_op()
        if j is None:
            break
        op = ops[j]
        before = replay.i
        if op["kind"] == "bulk":
            frame = op["data"][RTL8XXXU_TXDESC40_SIZE:]
            if len(frame) >= 24:
                driver._tx_sequence = int.from_bytes(frame[22:24], "little") >> 4
            await driver.inject_frame(frame)
            calls["inject_frame"] += 1
        elif _is_ctrl_read(op, _HOP_OPENER):
            channel = _peek_channel(ops, j)
            if channel is None:
                break
            await driver.set_channel(channel)
            calls[f"set_channel({channel})"] += 1
        elif j >= replay.async_from and op["kind"] == "ctrl" and op["breq"] == C.REALTEK_USB_CMD_REQ \
                and _op_addr(op) in _WATCHDOG_OPENERS + (C.REG_RCR,):
            replay._absorb_async(None, False)
        else:
            break
        if replay.i == before:
            break
    return calls


def validate_rx(bufs: list[bytes], driver: RTL8192EUDriver) -> tuple[int, int, int]:
    """Captured bulk-IN buffers through the driver's real RX descriptor parse and WlanFrameParser.
    Returns (frames, parsed, beacons). Informational: it does not affect the byte-match."""
    n_frames = n_parsed = n_bcn = 0
    for buf in bufs:
        try:
            parsed_frames = list(rtl8xxxu_parse_rxdesc24(driver.priv, buf))
        except Exception:  # noqa: BLE001
            continue
        for frame, signal in parsed_frames:
            n_frames += 1
            packet = WlanFrameParser.parse_80211_frame(frame, signal if signal is not None else 0)
            if packet is not None:
                n_parsed += 1
                n_bcn += packet.type == "beacon"
    return n_frames, n_parsed, n_bcn


def run(cap: str | None = None, verbose: bool = False) -> int:
    logging.getLogger("wifit3").setLevel(logging.CRITICAL)
    path = cap or DEFAULT_CAP
    if not Path(path).exists():
        print(f"FAIL: no such capture {path}")
        return 1
    pkts = parse_pcapng(path)
    dev = detect_dev(pkts)
    if dev is None:
        print(f"FAIL: no Realtek register-access device in {path}")
        return 1
    ops = build_ops(pkts, dev)
    n_driver = sum(1 for o in ops if o["kind"] == "bulk" or o["breq"] == C.REALTEK_USB_CMD_REQ)
    n_enum = sum(1 for o in ops if _waiver_for(o) is not None)
    print(f"verify_pcap rtl8192eu {path} (dev {dev})")

    replay = ReplayDev(ops)
    driver = RTL8192EUDriver.from_usb_device(replay, SUPPORTED_IDS[0])
    driver.transport.interface = interface_endpoints(ops)
    replay.driver = driver

    diverged = None
    truncated = False
    calls: Counter[str] = Counter()
    try:
        calls = asyncio.run(_drive(driver, replay, ops))
    except CaptureExhausted as e:
        if replay.matched + replay.waived_reg == n_driver:
            truncated = True
        else:
            diverged = e
    except Divergence as e:
        diverged = e
    except Exception as e:  # noqa: BLE001
        if replay.first_divergence is None:
            print(f"harness error {type(e).__name__}: {e} (matched {replay.matched})")
            return 2
    if replay.first_divergence is not None:
        diverged = replay.first_divergence

    n_frames, n_parsed, n_bcn = validate_rx(bulk_in_buffers(pkts, dev), driver)
    pct = 100.0 * replay.matched / n_driver if n_driver else 0.0
    print(f"driver ops byte-matched: {replay.matched}/{n_driver} = {pct:.3f}%")
    for name, b in replay.waived.items():
        if name != "USB enumeration":
            print(f"  {len(b.frames)} not matched (waived): {name} [f{min(b.frames)}-{max(b.frames)}] {b.why}")
    print(f"not driver ops (usbcore issues them): {n_enum} enum")
    print(f"inbound, not byte-matched: {n_frames} RX frames ({n_parsed} parsed, {n_bcn} beacons)")
    if calls:
        print("driven: " + ", ".join(f"{k}×{v}" for k, v in sorted(calls.items())))
    if replay.injected:
        print("async ticks run where the wire fired them: "
              + ", ".join(f"{k}×{v}" for k, v in sorted(replay.injected.items())))

    if diverged is not None:
        print(f"FAIL: diverged at op #{diverged.idx}: {diverged}")
        if verbose:
            print(replay.stack_trace(diverged.idx))
        return 1
    front = replay.next_register_op()
    if front is not None:
        print(f"INCOMPLETE ({pct:.3f}%): next unported op #{front} = {_fmt(ops[front])}")
        if verbose:
            print(replay.stack_trace(front))
        return 1
    trunc = " (capture ends mid-operation; the driver continued past the recording)" if truncated else ""
    if replay.waived_reg == 0:
        print(f"PASS: 100.000% byte-match, nothing waived{trunc}")
    else:
        print(f"PASS with waivers: {pct:.3f}% byte-match, {replay.waived_reg} ops waived (above){trunc}")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    pos = [a for a in argv if not a.startswith("-")]
    return run(pos[0] if pos else None, verbose="--verbose" in argv)


if __name__ == "__main__":
    raise SystemExit(main())
