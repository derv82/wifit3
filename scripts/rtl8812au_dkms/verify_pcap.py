"""Byte-for-byte replay-diff of the rtl8812au_dkms port against a vendor cold-boot capture.

PASS means ONLY: for this captured boot, the port emits the same USB bytes the vendor
driver did. It does NOT mean the code is correct or robust (the driver may poll a loop that
iterated once on this boot where we hardcode one write -- same bytes today, divergent
behaviour on a colder boot). The only real pass is beacons off the antenna. This is a
faithfulness gate, not a correctness proof.

Rides the shared rtw88-family replay engine (`scripts/rtw88_pcap_replay`): tshark extraction
+ a transport that feeds back the chip's recorded reads (so read-modify-writes see the real
values) and checks every write + FW-page byte-for-byte, raising at the first mismatch. Fully
offline -- no hardware.

Capture-agnostic: auto-detects the card's USB device address and replays its entire vendor
transaction stream from the first op (the bring-up consumes the prefix it needs; the rest is
ignored). Run it against more than one capture -- a stream that matches one boot but diverges
on another is dynamic/state-dependent behaviour we flattened.

    uv run python scripts/rtl8812au_dkms/verify_pcap.py [path/to/capture.pcap]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8812au_dkms import (  # noqa: E402
    bb, chan, dig, efuse, firmware, mac, monitor, rf, txpower,
)

REG_SYS_CFG = 0x00F0
CHANNEL = 1
DEFAULT_CAP = REPO / "usb_dumps_new" / "captures_8812au" / "capture-2.pcap"


def run(cap: str | None = None) -> int:
    time.sleep = lambda *a, **k: None        # replay needs no real settle delays

    pcap = Path(cap) if cap else DEFAULT_CAP
    if not pcap.exists():
        print(f"FAIL: no such capture {pcap}")
        return 1
    dev = rp.find_card_device(pcap)
    rp.audit_coverage(pcap, dev)
    ops = rp.extract_ops(pcap, dev)          # window=None -> the whole capture
    n_w = sum(o["kind"] == "W" for o in ops)
    n_r = sum(o["kind"] == "R" for o in ops)
    n_b = sum(o["kind"] == "B" for o in ops)
    print(f"{pcap.name}: card=dev{dev}, {len(ops)} vendor ops ({n_r} R, {n_w} W, {n_b} bulk)")
    if not ops:
        return 1
    print(f"  first op: {rp.ReplayTransport._fmt(ops[0])} @f{ops[0]['frame']}")

    # build_jaguar_params needs REG_SYS_CFG; PEEK the recorded read (read_chip_params
    # already issues the driver's single 0xF0 read -- a second one would be a false diff).
    sys_cfg = next((o["value"] for o in ops if o["kind"] == "R" and o["addr"] == REG_SYS_CFG),
                   None)
    if sys_cfg is None:
        print("FAIL: no recorded 0xF0 (REG_SYS_CFG) read to seed build_jaguar_params")
        return 1

    t = rp.ReplayTransport(ops)
    fw = firmware.load_firmware_blob()
    miles = []
    try:
        p = efuse.read_chip_params(t)
        miles.append(("efuse", t.i))
        jp = efuse.build_jaguar_params(p, sys_cfg)
        firmware.bring_up(t, fw)
        miles.append(("M1 fw", t.i))
        mac.phy_mac_config(t)
        mac.mac_init_misc(t)
        miles.append(("M2 mac", t.i))
        bb.phy_bb_config(t, crystal_cap=p.crystal_cap, params=jp)
        rf.phy_rf_config(t, params=jp)
        miles.append(("M3 bb+rf", t.i))
        chan.set_chnl_bw(t, ch=CHANNEL, bb_swing_2g_a=p.bb_swing_2g[0],
                         bb_swing_2g_b=p.bb_swing_2g[1], rfe_type=p.rfe_type)
        miles.append(("M4 chan", t.i))
        txpower.set_tx_power(t, CHANNEL, p.tx_power_2g)
        miles.append(("M-TXPWR", t.i))
        mac.hal_init_misc_pre(t)
        dig.init_hal_dm(t, search_edcca=True)   # the live PWDB-EDCCA search reproduces against
        mac.hal_init_misc_post(t)               # the capture's own recorded PSD reads -- not stripped
        miles.append(("M5 init-dm", t.i))
        # morrownr/airmon's RX-START tail: monitor opmode + nl80211 set-channel (the channel
        # re-tune + TX-power are wifit3's own functions re-run). Reproduced byte-for-byte.
        monitor.set_monitor_mode(t, CHANNEL, p)
        miles.append(("M5 monitor", t.i))
    except rp.Divergence as e:
        print(f"\nFAIL @ first divergence:\n  {e}")
        print(f"  reproduced {t.i} of {len(ops)} ops; "
              f"last clean milestone: {miles[-1][0] if miles else '(none)'}")
        return 1
    except Exception as e:  # noqa: BLE001 - surface harness/port errors distinctly
        print(f"\nERROR (harness/port bug, not a divergence): {type(e).__name__}: {e} @ op {t.i}")
        return 2

    print(f"\nPASS: reproduced {t.i}/{len(ops)} ops byte-for-byte -- the full cold-boot "
          f"bring-up through morrownr's monitor opmode + set-channel (monitor RCR "
          f"0x90000001). Remaining ops are the runtime RX session (bulk-IN + channel hops).")
    prev = 0
    for label, end in miles:
        print(f"      {label:12} {end - prev:5} ops")
        prev = end
    return 0


def main() -> int:
    return run(sys.argv[1] if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    raise SystemExit(main())
