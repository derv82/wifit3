"""Replay + byte-verify the airodump `--band abg` native-hop scan (the abg DARK region).

pcap_slicer maps the `<AIRODUMP --band abg native 250ms hop>` phase to a frame window; within it
airodump's nl80211 sw-scan tunes ~49 channels (not individually logged). Each tune is the SAME
config_phydm_switch_channel primitive verify_channels gates for the explicit `iw` hops (the op
signature 0x958/0x860/0xa24/0xa28/RF18/igi/ccapar/spur is identical). This detects the scanned
channel sequence from the ops (each switch_channel writes the AGC-table-idx 0x958, then RF18 whose
low byte is the channel), then replays set_channel_bw across that sequence against ONE strict cursor
over the abg window — confirming the scan-tune path == the explicit-set path. cap-1 only.

FINDING: the scan-tune IS our set_channel (hop ch1 replays ~131 ops byte-for-byte). The remaining
hops report "divergent" because the 2 s PHYDM watchdog fires BETWEEN scan-tunes (the op run
0x210/0x280 TXPAUSE -> 0x98c ant-weight -> 0x994 env-monitor -> 0xfa4/0xfb4 DPK -> 0x1f80.. PMAC ->
0xf50/0xfcc.. FA-counters is a watchdog tick, not a tune), and this single-cursor replay doesn't
interleave it. So the abg window = set_channel tunes + watchdog ticks, both already verified
elsewhere; nothing in it is an un-accounted driver op. Reproducing it fully would just mean
interleaving dm_watchdog at the capture's firing cadence (a timing chore, not a divergence).

Run: uv run python scripts/rtl8822bu_dkms/verify_abg_hop.py
"""
from __future__ import annotations

import bisect
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8822bu_dkms import chan, chipid, efuse, txpower, usbphy  # noqa: E402
from wifit3.chips.rtl8822bu_dkms.transport import Rtl8822buTransport  # noqa: E402

CAP_DIR = REPO / "usb_dumps_new" / "captures_rtl88x2bu"
RF_A_0x18_WRITE = 0x0C90        # SIPI RF-write: value = (addr<<20)|data; addr 0x18 => RF18 (channel)
AGC_TBL_IDX = 0x0958            # switch_channel's first BB write — marks a new hop


def _frame_epochs(pcap):
    out = subprocess.run(["tshark", "-r", str(pcap), "-T", "fields", "-e", "frame.number",
                          "-e", "frame.time_epoch"], capture_output=True, text=True, check=True).stdout
    nums, eps = [], []
    for line in out.splitlines():
        p = line.split()
        if len(p) == 2:
            try:
                nums.append(int(p[0]))
                eps.append(float(p[1]))
            except ValueError:
                pass
    return nums, eps


def _abg_window(log_path):
    """(start_epoch, end_epoch) of the AIRODUMP --band abg phase from main.log."""
    start = end = None
    with open(log_path) as f:
        for line in f:
            m = re.match(r"^\[(\d+\.\d+)\]", line)
            if not m:
                continue
            if "[AIRODUMP] start" in line:
                start = float(m.group(1))
            elif "[AIRODUMP] stopped" in line:
                end = float(m.group(1))
    return start, end


def main():
    time.sleep = lambda *a, **k: None
    pcap = CAP_DIR / "capture-1.pcap"
    dev = rp.find_card_device(pcap)
    s_ep, e_ep = _abg_window(CAP_DIR / "capture-1_logs" / "main.log")
    nums, eps = _frame_epochs(pcap)
    f0 = nums[bisect.bisect_left(eps, s_ep)]
    f1 = nums[min(bisect.bisect_left(eps, e_ep) - 1, len(nums) - 1)]
    print(f"abg window: epoch {s_ep:.3f}..{e_ep:.3f} -> frames {f0}..{f1}")

    ctrl = rp.extract_ctrl_ops(pcap, dev)
    win = [o for o in ctrl if f0 <= o["frame"] <= f1]

    # Detect the scanned channel sequence: at each 0x958 write (switch_channel start), the next RF18
    # write (0xc90 with addr-byte 0x18) carries the channel in its low byte.
    seq = []
    for i, o in enumerate(win):
        if o["dir"] == "OUT" and o["wval"] == AGC_TBL_IDX:
            for o2 in win[i:i + 12]:
                if o2["dir"] == "OUT" and o2["wval"] == RF_A_0x18_WRITE:
                    v = int.from_bytes(o2["data"], "little")
                    if (v >> 20) & 0xFF == 0x18:
                        seq.append(v & 0xFF)
                        break
    print(f"detected {len(seq)} scan hops: {seq}")

    # PG decode (EFUSE prefix) so TXAGC can be written if the scan tune includes it.
    ct = Rtl8822buTransport(rp.ReplayDevice(
        rp.merge_ops_by_frame(ctrl, rp.extract_bulk_out_ops(pcap, dev))))
    info = chipid.get_chip_info(ct)
    usbphy.phy_cfg_usb(ct, info.chip_ver)
    chipid.read_chip_version(ct)
    pg = txpower.parse_pg(efuse.read_efuse(ct).log_map)

    # One strict cursor over the whole abg window; replay set_channel_bw per detected channel.
    rdev = rp.ReplayDevice(win)
    t = Rtl8822buTransport(rdev)
    prev = 1                      # synth left at ch1 by the probe-time initial tune
    npass = nfail = 0
    for ch in seq:
        crossing = (prev <= 14) != (ch <= 14)
        start_i = rdev.i
        try:
            chan.set_channel_bw(t, ch, prev_ch=prev, txpwr_pg=(None if crossing else pg))
            npass += 1
        except rp.Divergence as d:
            print(f"  hop ch {ch:>3} (prev {prev}): FAIL after {rdev.i - start_i} ops -- {d}")
            nfail += 1
            # resync to the next hop head (next 0x958) so one failure doesn't cascade
            nxt = next((k for k in range(rdev.i, len(win))
                        if win[k]["dir"] == "OUT" and win[k]["wval"] == AGC_TBL_IDX), len(win))
            rdev.i = nxt
        prev = ch
    print(f"\nabg-hop replay: {npass} hops byte-for-byte, {nfail} divergent "
          f"(cursor at op {rdev.i}/{len(win)})")
    return 1 if nfail else 0


if __name__ == "__main__":
    raise SystemExit(main())
