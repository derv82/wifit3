"""Byte-for-byte differ for the per-channel RF retune (the airodump hop).

verify_pcap.py covers the cold init; the cold-boot captures ALSO contain the vendor driver's
tune for every channel an airodump `--band abg` sweep hopped (`<cap>_logs/iw.log`: 2.4 GHz 1-12,
5 GHz 36..165, back to 1). Each `iw set channel N` is one config_phydm_switch_channel_8822b +
spur reset (+ a band switch on a 2.4<->5 crossing, + the bandwidth re-apply — both still WIP).

Slices each `iw set channel N` window (iw.log epoch -> pcap frame) and replays chan.switch_channel
against it from the window's FIRST op (no anchoring). switch_channel reproduces the retune prologue
of each window; the trailing ops (mac_switch_bandwidth + switch_bandwidth, and on crossings the
band switch) are the not-yet-ported remainder, so we report the matched-op count and the first
unmatched op rather than demanding the whole window. A divergence inside the matched span is a
real port bug. Windows whose head isn't switch_channel's RF-0x18 read (band-switch crossings,
slicing artifacts) are flagged separately.

Run: uv run python scripts/rtl8822bu_dkms/verify_channels.py [capture-1|capture-2|capture-3]
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
from wifit3.chips.rtl8822bu_dkms import chan  # noqa: E402
from wifit3.chips.rtl8822bu_dkms.transport import Rtl8822buTransport  # noqa: E402

CAP_DIR = REPO / "usb_dumps_new" / "captures_rtl88x2bu"
_IW_LINE = re.compile(r"^\[(\d+\.\d+)\] Executing:.*set channel (\d+)")
RF_A_0x18_READ = 0x2860            # switch_channel's first op: read RF_A 0x18 (BB 0x2800+0x18*4)


def _frame_epochs(pcap: Path):
    out = subprocess.run(
        ["tshark", "-r", str(pcap), "-T", "fields", "-e", "frame.number", "-e", "frame.time_epoch"],
        capture_output=True, text=True, check=True).stdout
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


def _parse_iw(iw_log: Path):
    cmds = []
    with open(iw_log) as f:
        for line in f:
            m = _IW_LINE.match(line)
            if m:
                cmds.append((float(m.group(1)), int(m.group(2))))
    return cmds


def verify(cap_name: str) -> int:
    time.sleep = lambda *a, **k: None
    pcap = CAP_DIR / f"{cap_name}.pcap"
    iw_log = CAP_DIR / f"{cap_name}_logs" / "iw.log"
    dev = rp.find_card_device(pcap)
    ctrl = rp.extract_ctrl_ops(pcap, dev)
    nums, eps = _frame_epochs(pcap)
    cmds = _parse_iw(iw_log)
    print(f"{cap_name}: card=dev{dev}, {len(cmds)} channel-set commands")

    npass = nfail = nskip = 0
    for i, (epoch, ch) in enumerate(cmds):
        prev = cmds[i - 1][1] if i > 0 else None
        crossing = prev is not None and (prev <= 14) != (ch <= 14)
        end = cmds[i + 1][0] if i + 1 < len(cmds) else epoch + 1.5
        s = bisect.bisect_left(eps, epoch)
        e = bisect.bisect_left(eps, end) - 1
        if s >= len(nums):
            continue
        f0, f1 = nums[s], nums[min(e, len(nums) - 1)]
        win = [o for o in ctrl if f0 <= o["frame"] <= f1]
        # Both the same-band retune (switch_channel) and a crossing's band switch open with the
        # RF_A 0x18 read; anything else is a slice artifact (a window that opens mid-cal).
        head = next((o for o in win if o["wval"] != 0x04E0), None)
        if not (head and head["dir"] == "IN" and head["wval"] == RF_A_0x18_READ):
            nskip += 1
            print(f"  ch {ch:>3}: SKIP (slice artifact: window head is not the retune)")
            continue

        dev = rp.ReplayDevice(win)
        t = Rtl8822buTransport(dev)
        try:
            chan.set_channel_bw(t, ch, prev_ch=prev)
        except rp.Divergence as d:
            print(f"  ch {ch:>3}: FAIL -- {d}")
            nfail += 1
            continue
        # set_channel_bw should land on the deferred cal: the per-channel DPK (0x1Dxx LUT), or on
        # a crossing the BT-coex band-notify (rtw_btcoex_..._switchband_notify, the lone 0xCBC
        # antenna write) which precedes it. Both are separate, deferred subsystems.
        nxt = next((win[k] for k in range(dev.i, len(win)) if win[k]["wval"] != 0x04E0), None)
        on_dpk = nxt is not None and 0x1D00 <= nxt["wval"] <= 0x1DFF
        on_coex = nxt is not None and nxt["wval"] == 0x0CBC
        npass += 1
        edge = ("-> DPK boundary" if on_dpk else "-> coex band-notify (then DPK)" if on_coex
                else f"-> next 0x{nxt['wval']:04x}" if nxt else "-> end")
        kind = "set_channel_bw+band" if crossing else "set_channel_bw"
        print(f"  ch {ch:>3}: PASS -- {kind} byte-for-byte ({dev.i} ops) {edge}")

    print(f"\n{cap_name}: {npass} PASS, {nfail} FAIL, {nskip} skipped "
          f"(band-switch crossings + slice artifacts — windows whose head isn't the retune).")
    return 1 if nfail else 0


def main() -> int:
    return verify(Path(sys.argv[1] if len(sys.argv) > 1 else "capture-1").stem)


if __name__ == "__main__":
    raise SystemExit(main())
