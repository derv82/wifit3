"""Byte-for-byte replay-diff of the rtl8821cu_dkms port vs the vendor cold-boot capture.

One monotonic cursor walks the recorded control stream; the port must reproduce every
vendor register op (the ON-section 0x4E0 mirror included). The first op the port does
NOT reproduce is the frontier — the next thing to port.

Milestone 1 scope: the HALMAC card-enable power sequence (``bringup.power_on``). The
frontier after a clean M1 is whatever the vendor does next (chip-id/EFUSE, firmware
download) — that is milestone 2, not a failure.

    uv run python scripts/verify_pcap.py rtl8821cu_dkms
    uv run python scripts/rtl8821cu_dkms/verify_pcap.py [path/to/capture.pcap]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8821cu_dkms import bringup  # noqa: E402
from wifit3.chips.rtl8821cu_dkms.transport import Rtl8821cuTransport  # noqa: E402

DEFAULT_CAP = REPO / "usb_dumps_new2" / "captures_rtl8821cu" / "capture-1.pcap"


def _fmt(op: dict) -> str:
    if op.get("dir") == "BULK":
        return f"BULK[{len(op['data'])}B]"
    d = op.get("data", b"")
    val = f"=0x{int.from_bytes(d, 'little'):0{max(len(d) * 2, 2)}x}" if d else ""
    return f"{op['dir']} 0x{op['wval']:04x}/{op['width']}{val}"


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None       # replay needs no settle delays

    pcap = Path(cap) if cap else DEFAULT_CAP
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1

    dev_addr = rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev_addr)
    ops = rp.extract_ctrl_ops(pcap, dev_addr)
    print(f"{pcap.name}: card=dev{dev_addr}, {len(ops)} control ops")
    print("  first 40 control ops (* = 0x4E0 ON-section mirror):")
    for k, o in enumerate(ops[:40]):
        tag = " *" if o["wval"] == 0x04E0 else ""
        print(f"    [{k:3}] f{o['frame']:<7} {_fmt(o)}{tag}")

    dev = rp.ReplayDevice(ops)
    t = Rtl8821cuTransport(dev)
    try:
        bringup.power_on(t)
    except rp.Divergence as e:
        print(f"\nDIVERGENCE after {dev.i} ops:\n  {e}")
        return 1

    consumed = dev.i
    print(f"\nM1 power-on reproduced {consumed}/{len(ops)} control ops clean.")
    if consumed < len(ops):
        nxt = ops[consumed]
        print(f"FRONTIER -> op #{consumed} (frame {nxt['frame']}): {_fmt(nxt)}")
        print("  (the next op to port — milestone 2; M1 power-on itself is clean above)")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
