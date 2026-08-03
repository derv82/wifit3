"""Byte-for-byte replay-diff of the MAINLINE rtl8822bu port vs the in-kernel
rtw88 cold-boot capture.

Unlike the `_dkms` recipes (which drive a transport-centric DKMS port), the
mainline 8822bu bring-up is written against a real pyusb `dev`:
  - register access bottoms out at `dev.ctrl_transfer` (via Rtw88Transport)
  - FW upload chunks go out as `dev.write(EP, pkt)` bulk-OUT
So instead of the transport-surface `ReplayTransport`, we wrap it in a
device-layer `ReplayDev` and let the REAL transport + REAL firmware/phy/mac
code run unchanged. Reads return the chip's recorded values; every write +
FW bulk packet is checked against the wire; first mismatch raises Divergence.

This is a Pcap Replay accuracy probe — it short-circuits at the first divergence and
reports where. A PASS would mean the cold path is byte-identical (necessary,
not sufficient). Fully offline.

    uv run python scripts/chips/rtl8822bu/verify_pcap.py [path/to/capture.pcap]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import rtw88_pcap_replay as rp  # noqa: E402

from wifit3.chips.rtl8822bu.chan import set_channel_2g_20mhz  # noqa: E402
from wifit3.chips.rtl8822bu.constants import REG_SYS_CFG1  # noqa: E402
from wifit3.chips.rtl8822bu.firmware import (  # noqa: E402
    download_firmware, download_firmware_validate, load_firmware_blob,
)
from wifit3.chips.rtl8822bu.mac import (  # noqa: E402
    cut_mask_from_sys_cfg1, mac_init_for_rx, mac_power_on,
)
from wifit3.chips.rtl8822bu.phy import EfuseDefaults, phy_set_param  # noqa: E402
from wifit3.chips.rtl8822bu.transport import RTL8822BUTransport  # noqa: E402

CHANNEL = 1
DEFAULT_CAP = REPO / "driver_captures" / "captures_rtw88_8822bu" / "capture-1.pcap"


class ReplayDev:
    """pyusb-Device shim over a ReplayTransport: adapts ctrl_transfer + bulk
    write() to the op cursor so the real Rtw88Transport/firmware drive it."""

    def __init__(self, ops):
        self.t = rp.ReplayTransport(ops)

    @property
    def i(self):
        return self.t.i

    def ctrl_transfer(self, bmReqType, bReq, wValue, wIndex, data_or_len, timeout=None):
        if bmReqType & 0x80:                       # 0xC0 vendor IN (read)
            length = int(data_or_len)
            return self.t._read(wValue, length).to_bytes(length, "little")
        payload = bytes(data_or_len)               # 0x40 vendor OUT (write)
        self.t._write(wValue, len(payload), int.from_bytes(payload, "little"))
        return len(payload)

    def write(self, ep, data, timeout=None):       # bulk-OUT (FW chunks)
        self.t.bulk_out(bytes(data))
        return len(data)


def _dump_ops(ops, n):
    print(f"  first {n} vendor ops:")
    for k, o in enumerate(ops[:n]):
        print(f"    [{k:3}] f{o['frame']:<6} {rp.ReplayTransport._fmt(o)}")


def _ctx(ops, i, before=4, after=4):
    print("  capture context around the divergence:")
    for k in range(max(0, i - before), min(len(ops), i + after + 1)):
        mark = " <-- port stopped here" if k == i else ""
        print(f"    [{k:3}] f{ops[k]['frame']:<6} {rp.ReplayTransport._fmt(ops[k])}{mark}")


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None              # replay needs no settle delays

    pcap = Path(cap) if cap else DEFAULT_CAP
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1
    dev_addr = rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev_addr)
    ops = rp.extract_ops(pcap, dev_addr)           # whole capture

    # This mainline port omits 0x04E0 writes which appear in the diff.
    # These writes are real driver ops: the 8822B vendor USB layer
    # writes 1 byte here after each ON-section register access (addr<=0xff,
    # 0x1000-0x10ff) [SRC os_dep/linux/usb_ops_linux.c:171-201]. A complete
    # transport must emit them, so they are NOT filtered.
    n_w = sum(o["kind"] == "W" for o in ops)
    n_r = sum(o["kind"] == "R" for o in ops)
    n_b = sum(o["kind"] == "B" for o in ops)
    print(f"{pcap.name}: card=dev{dev_addr}, {len(ops)} vendor ops "
          f"({n_r} R, {n_w} W, {n_b} bulk)")
    if not ops:
        return 1
    _dump_ops(ops, 25)

    replay = ReplayDev(ops)
    transport = RTL8822BUTransport(replay)
    fw = load_firmware_blob()
    miles = []
    try:
        # Mirror test_hw: the driver's first vendor op is the REG_SYS_CFG1 read
        # that seeds cut_mask. Consuming it here keeps us aligned with the wire.
        sys_cfg = transport.read32(REG_SYS_CFG1)
        cut_mask = cut_mask_from_sys_cfg1(sys_cfg)
        print(f"  REG_SYS_CFG1=0x{sys_cfg:08x} -> cut_mask=0x{cut_mask:02x}\n")
        mac_power_on(transport, cut_mask=cut_mask)
        miles.append(("M1 power-on", replay.i))
        download_firmware(replay, transport, fw)
        miles.append(("M2 fw upload", replay.i))
        download_firmware_validate(transport)
        miles.append(("M3 fw validate", replay.i))
        phy_set_param(transport, EfuseDefaults())
        miles.append(("M4 phy", replay.i))
        mac_init_for_rx(transport)
        miles.append(("M5 mac-init", replay.i))
        set_channel_2g_20mhz(transport, CHANNEL)
        miles.append(("M6 channel", replay.i))
    except rp.Divergence as e:
        print(f"FAIL @ first divergence:\n  {e}\n")
        _ctx(ops, replay.i - 1)
        last = miles[-1][0] if miles else "(none — diverged before M1 completed)"
        print(f"\n  reproduced {replay.i} of {len(ops)} ops; last clean milestone: {last}")
        _milestones(miles)
        return 1
    except Exception as e:  # noqa: BLE001 — surface harness/port bugs distinctly
        print(f"ERROR (harness/port bug, not a divergence): "
              f"{type(e).__name__}: {e} @ op {replay.i}")
        import traceback
        traceback.print_exc()
        return 2

    print(f"\nPASS: reproduced {replay.i}/{len(ops)} ops byte-for-byte "
          f"(cold bring-up through channel tune).")
    _milestones(miles)
    return 0


def _milestones(miles):
    prev = 0
    for label, end in miles:
        print(f"      {label:16} {end - prev:6} ops")
        prev = end


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
