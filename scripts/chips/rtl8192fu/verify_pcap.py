"""Acceptance gate: replay the rtl8192fu port against a mainline rtl8xxxu cold-boot capture.

One cursor over every vendor op of the capture, fail-closed, no waivers. The probe runs the
driver's real ``rtl8xxxu_probe``; after it, each burst is dispatched by its opening op to the
real handler mac80211 would have called (start, configure_filter, config, sw_scan_start/complete,
stop, tx). Every captured bulk-IN buffer is then decoded through the driver's RX path. The first op that opens no known handler stops the walk and is reported.

    uv run python scripts/chips/rtl8192fu/verify_pcap.py [capture-N | path/to/capture.pcap]
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import rtw88_pcap_replay as rp
from wifit3.chips.rtl8192fu import constants as C
from wifit3.chips.rtl8192fu.core import (
    MONITOR_FILTER_FLAGS,
    Rtl8xxxuPriv,
    rtl8xxxu_config,
    rtl8xxxu_configure_filter,
    rtl8xxxu_probe,
    rtl8xxxu_start,
    rtl8xxxu_stop,
    rtl8xxxu_sw_scan_complete,
    rtl8xxxu_sw_scan_start,
)
from wifit3.chips.rtl8192fu.rtl8192f import rtl8192fu_fops
from wifit3.chips.rtl8192fu.rx import rtl8xxxu_parse_rxdesc24
from wifit3.chips.rtl8192fu.transport import RTL8192FUTransport
from wifit3.chips.rtl8192fu.tx import RTL8XXXU_TXDESC40_SIZE, rtl8xxxu_tx
from wifit3.dot11.parser import WlanFrameParser

CAP_DIR = REPO / "driver_captures" / "captures_rtl8xxxu_8192fu"
FIRMWARE = REPO / "src/wifit3/chips/rtl8192fu/assets/rtl8192fufw.bin"


class ReplayTransport(rp.ReplayTransport):
    def __init__(self, ops: list[dict], bulk_out_endpoints: dict[int, int]) -> None:
        super().__init__(ops)
        self.bulk_out_endpoints = bulk_out_endpoints

    def bulk_write(self, endpoint: int, data: bytes) -> None:
        op = self.ops[self.i] if self.i < len(self.ops) else None
        if op is not None and op["kind"] == "B" and self.bulk_out_endpoints[op["frame"]] != endpoint:
            raise rp.Divergence(f"op#{self.i} frame {op['frame']}: port sent on ep 0x{endpoint:02x}, "
                                f"capture has ep 0x{self.bulk_out_endpoints[op['frame']]:02x}")
        self.bulk_out(data)


def _bulk_out_endpoints(pcap: Path, device: int) -> dict[int, int]:
    out = subprocess.run(
        ["tshark", "-r", str(pcap), "-Y",
         f"usb.device_address=={device} && usb.transfer_type==0x03 && usb.endpoint_address.direction==0",
         "-T", "fields", "-e", "frame.number", "-e", "usb.endpoint_address"],
        capture_output=True, text=True, check=True).stdout
    return {int(frame): int(endpoint, 16) for frame, endpoint in (line.split("	") for line in out.splitlines())}


def _endpoints(pcap: Path, device: int) -> list[SimpleNamespace]:
    out = subprocess.run(
        ["tshark", "-r", str(pcap), "-Y", f"usb.device_address=={device} && usb.bDescriptorType==5",
         "-T", "fields", "-e", "usb.bEndpointAddress", "-e", "usb.bmAttributes"],
        capture_output=True, text=True, check=True).stdout.splitlines()[0]
    addresses, attributes = (column.split(",") for column in out.split("\t"))
    return [SimpleNamespace(bEndpointAddress=int(a, 16), bmAttributes=int(b, 16))
            for a, b in zip(addresses, attributes, strict=True)]


def _channel_of_next_tune(ops: list[dict], i: int) -> int | None:
    """The channel a config burst tunes to: the first RF 0x18 (RF6052_REG_MODE_AG) LSSI write."""
    for op in ops[i:i + 400]:
        if (op["kind"] == "W" and op["addr"] == C.REG_FPGA0_XA_LSSI_PARM
                and op["value"] >> C.FPGA0_LSSI_PARM_ADDR_SHIFT == C.RF6052_REG_MODE_AG):
            return op["value"] & 0xFF
    return None


def _handler(ops: list[dict], i: int):
    """(label, callable(priv)) for the burst opening at ops[i], or None."""
    op = ops[i]
    following = ops[i + 1] if i + 1 < len(ops) else None
    if op["kind"] == "B":
        frame = op["data"][RTL8XXXU_TXDESC40_SIZE:]
        return "tx", lambda priv: priv.transport.bulk_write(*rtl8xxxu_tx(priv, frame))
    if op["kind"] == "W" and op["addr"] == C.REG_RF_CTRL and op["width"] == 1:
        return "start", rtl8xxxu_start
    if op["kind"] == "W" and op["addr"] == C.REG_RCR and op["width"] == 4:
        return "configure_filter", lambda priv: rtl8xxxu_configure_filter(priv, MONITOR_FILTER_FLAGS)
    if op["kind"] == "R" and op["addr"] == C.REG_TX_AGC_A_CCK1_MCS32:
        channel = _channel_of_next_tune(ops, i)
        if channel is not None:
            return f"config ch{channel}", lambda priv: rtl8xxxu_config(priv, channel)
    if op["kind"] == "R" and op["addr"] == C.REG_BEACON_CTRL and op["width"] == 1 and following is not None:
        if following["value"] & C.BEACON_DISABLE_TSF_UPDATE:
            return "sw_scan_start", rtl8xxxu_sw_scan_start
        return "sw_scan_complete", rtl8xxxu_sw_scan_complete
    if (op["kind"] == "W" and op["addr"] == C.REG_TXPAUSE and op["value"] == 0xFF
            and following is not None and following["addr"] == C.REG_RXFLTMAP0):
        return "stop", rtl8xxxu_stop
    return None


def _describe(op: dict) -> str:
    if op["kind"] == "B":
        return f"bulk-OUT {len(op['data'])}B (frame {op['frame']})"
    return f"{op['kind']} 0x{op['addr']:04x}/{op['width']}=0x{op['value']:x} (frame {op['frame']})"


def run(cap: str | None = None) -> int:
    time.sleep = lambda *_args, **_kwargs: None
    pcap = Path(cap) if cap and cap.endswith(".pcap") else CAP_DIR / f"{cap or 'capture-1'}.pcap"
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1

    device = rp.find_card_device(pcap)
    ops = rp.extract_ops(pcap, device)
    transport = ReplayTransport(ops, _bulk_out_endpoints(pcap, device))
    priv = Rtl8xxxuPriv(transport, rtl8192fu_fops)
    RTL8192FUTransport.rtl8xxxu_parse_usb(SimpleNamespace(interface=_endpoints(pcap, device)), priv)
    print(f"{pcap.name}: card=dev{device}, {len(ops)} vendor ops, out_ep={[hex(e) for e in priv.out_ep]}")

    try:
        rtl8xxxu_probe(priv, FIRMWARE.read_bytes())
    except rp.Divergence as error:
        print(f"FAIL probe at op {transport.i}: {error}")
        return 1
    print(f"  probe: {transport.i} ops (cut={priv.chip_cut} vendor={priv.chip_vendor} "
          f"rfe={priv.rfe_type} xtal={priv.default_crystal_cap:#x} cck_new_agc={priv.cck_new_agc})")

    calls: dict[str, int] = {}
    while transport.i < len(ops):
        start = transport.i
        handler = _handler(ops, start)
        if handler is None:
            print(f"\nFRONTIER at op {start}/{len(ops)}: {_describe(ops[start])}")
            for op in ops[start:start + 12]:
                print(f"    {_describe(op)}")
            return 1
        label, call = handler
        try:
            call(priv)
        except rp.Divergence as error:
            print(f"\nFAIL {label} (from op {start}, frame {ops[start]['frame']}): {error}")
            return 1
        kind = label.split()[0]
        calls[kind] = calls.get(kind, 0) + 1

    print("  handlers: " + ", ".join(f"{name}x{count}" for name, count in calls.items()))
    print(f"  wire: {len(ops)} of {len(ops)} ops reproduced, zero waived")

    buffers = rp.extract_bulk_in_ops(pcap, device)
    frames = parsed = 0
    signals: list[int] = []
    for buffer in buffers:
        for frame, signal in rtl8xxxu_parse_rxdesc24(priv, buffer):
            frames += 1
            if signal is not None:
                signals.append(signal)
            if WlanFrameParser.parse_80211_frame(frame, 0 if signal is None else signal) is not None:
                parsed += 1
    if not parsed:
        print(f"FAIL RX: {len(buffers)} bulk-IN buffers gave {frames} frames, none parsed")
        return 1
    print(f"  rx: {len(buffers)} bulk-IN buffers -> {frames} frames, {parsed} parsed, "
          f"signal {min(signals)}..{max(signals)} dBm, {len(priv.c2hcmd_queue)} C2H queued")
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1] if len(sys.argv) > 1 else None))
