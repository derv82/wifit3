"""Byte-for-byte replay-diff of the rtl8822bu_dkms port vs the morrownr rtl88x2bu
cold-boot capture.

The 8822b transport is dev-centric (it calls ``dev.ctrl_transfer`` so it can emit
the 0x4E0 page-switch mirror), so the gate replays at the ctrl_transfer layer:
``extract_ctrl_ops`` + ``rtw88_pcap_replay.ReplayDevice`` feed recorded reads back
and byte-check every write (mirror included). One monotonic cursor walks the whole
capture; the first op the port does NOT reproduce is the frontier — the next thing
to port.

    uv run python scripts/verify_pcap.py rtl8822bu_dkms
    uv run python scripts/rtl8822bu_dkms/verify_pcap.py [path/to/capture.pcap]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8822bu_dkms import bringup  # noqa: E402
from wifit3.chips.rtl8822bu_dkms.transport import Rtl8822buTransport  # noqa: E402

DEFAULT_CAP = REPO / "usb_dumps_new" / "captures_rtl88x2bu" / "capture-1.pcap"

# cold_bringup reproduces the ENTIRE vendor chip init `rtl8822b_init`, byte-for-byte to op 9855:
# chip-ID/EFUSE/power/FW/MAC/BB/RF -> the full odm_dm_init (DIG/CCK-PD/env-monitor/adaptivity/ra-info
# RX seed + the RF-cal tail cfo-tracking/rf-init/dc-cancellation/tx-current-cal/get-pa-bias/psd-init) ->
# the rtl8822b_init tail (phy_bf_init MU-MIMO seed, wifi-only coex antenna/RFE, init_misc CAM/RCR/
# sec-en/TXQ/AMPDU). Op 9855+ is NOT chip bring-up: it's the OS interface-up / opmode state machine
# (hw_var_set_opmode: MSR/network-type, MAC addr, beacon ctrl, RX-filter — the driver's connect()
# layer), then at op ~9873 the per-channel cal scan (the airodump hops, gated by verify_channels),
# then at op 28910 the aireplay-ng TX injection (a different program's traffic — the intentional stop).
CAL_SCAN_START = 9855


def _fmt(op: dict) -> str:
    if op.get("dir") == "BULK":
        return f"BULK[{len(op['data'])}B]"
    d = op.get("data", b"")
    val = f"=0x{int.from_bytes(d, 'little'):0{max(len(d) * 2, 2)}x}" if d else ""
    return f"{op['dir']} 0x{op['wval']:04x}/{op['width']}{val}"


def _bringup(t) -> None:
    """The ported cold init, driven against the replay device. The canonical sequence lives in
    `bringup.cold_bringup` (the driver's connect() path), so the gate verifies the exact code the
    hardware runs. The frontier past this (op ~9410) is the per-channel cal scan — see the doc."""
    bringup.cold_bringup(t)


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None       # replay needs no settle delays

    pcap = Path(cap) if cap else DEFAULT_CAP
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1

    dev_addr = rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev_addr)
    # Merge control + bulk-OUT into one frame-ordered stream so the FW download (vendor
    # register writes interleaved with bulk FW packets) replays against one ReplayDevice.
    ctrl = rp.extract_ctrl_ops(pcap, dev_addr)
    bulk = rp.extract_bulk_out_ops(pcap, dev_addr)
    ops = rp.merge_ops_by_frame(ctrl, bulk)
    print(f"{pcap.name}: card=dev{dev_addr}, {len(ctrl)} control + {len(bulk)} bulk-OUT ops")
    print("  first 40 control ops (* = 0x4E0 page-switch mirror):")
    for k, o in enumerate(ops[:40]):
        tag = " *" if o["wval"] == 0x04E0 else ""
        print(f"    [{k:3}] f{o['frame']:<7} {_fmt(o)}{tag}")

    dev = rp.ReplayDevice(ops)
    t = Rtl8822buTransport(dev)
    try:
        _bringup(t)
    except rp.Divergence as e:
        print(f"\nDIVERGENCE after {dev.i} ops:\n  {e}")
        return 1

    consumed = dev.i
    print(f"\nported bring-up reproduced {consumed}/{len(ops)} ops clean.")
    if consumed < len(ops):
        nxt = ops[consumed]
        print(f"FRONTIER -> op #{consumed} (frame {nxt['frame']}): {_fmt(nxt)}")
        if consumed >= CAL_SCAN_START:
            # The deterministic cold init is fully reproduced. Everything past here is the
            # vendor's all-channel RF cal scan (IQK/DPK/TSSI over every 2.4G+5G channel, twice);
            # per the Lead's decision we cal per-channel on-demand in set_channel, not by replaying
            # this scan. See RTL8822BU_DKMS.md "RF calibration".
            print("  PASS: the full vendor chip init (rtl8822b_init) is reproduced byte-for-byte.")
            print("  Remaining: OS interface-up/opmode (driver connect()), then the per-channel cal")
            print("  scan (verify_channels), then aireplay-ng TX injection at op 28910 (the stop).")
        else:
            print("  (this is the next op to port; not yet a full PASS)")
        return 0
    print("PASS: full single-cursor reproduction.")
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
