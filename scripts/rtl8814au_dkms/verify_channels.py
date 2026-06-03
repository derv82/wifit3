"""Byte-for-byte differ for the per-channel tune — one channel at a time.

`verify_pcap.py` covers the init through the single ch1 tune; the cold-boot captures
ALSO contain the vendor driver's tune for every channel airodump hopped (see
`<cap>_logs/iw.log`: 2.4 GHz 1-12, then 5 GHz 36..165). A mis-ported channel tune —
a missing RF-off-during-tune, a skipped post-tune recal, a wrong per-channel TX-power
group — silently degrades RX, so every hop must reproduce the wire exactly.

This slices each `iw set channel N` window (by the iw.log epoch -> pcap frame, like
`pcap_slicer.py`) and replays `chan.set_channel_bw(t, N, tx_power)` against it from the
window's first op — NO anchoring/trim, so a vendor step before/after our sequence shows
up as the first divergence rather than being silently skipped.

2.4 GHz channels (<=14) are verified now. 5 GHz channels are reported as M5 (not yet
ported) and skipped — when M5 lands, the same differ becomes its acceptance gate.

Run: uv run python scripts/rtl8814au_dkms/verify_channels.py [capture-1|2|3]
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
sys.path.insert(0, str(Path(__file__).parent))   # verify_pcap (op extractor + replay)

import verify_pcap as vp  # noqa: E402
from wifit3.chips.rtl8814au_dkms import chan  # noqa: E402

_IW_LINE = re.compile(r"^\[(\d+\.\d+)\] Executing:.*set channel (\d+)")


def _frame_epochs(pcap: Path):
    """(frame_numbers, epochs) for the whole pcap — monotonic, for epoch->frame bisect."""
    out = subprocess.run(
        ["tshark", "-r", str(pcap), "-T", "fields",
         "-e", "frame.number", "-e", "frame.time_epoch"],
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


def _parse_iw_channels(iw_log: Path):
    """Ordered [(epoch, channel)] from the iw.log `Executing: ... set channel N` lines."""
    cmds = []
    with open(iw_log) as f:
        for line in f:
            m = _IW_LINE.match(line)
            if m:
                cmds.append((float(m.group(1)), int(m.group(2))))
    return cmds


def verify(cap_name: str) -> int:
    pcap = vp.CAP_DIR / f"{cap_name}.pcap"
    dev = vp.DEV_ADDR[cap_name]
    iw_log = vp.CAP_DIR / f"{cap_name}_logs" / "iw.log"
    time.sleep = lambda *a, **k: None   # replay needs no real settle delays

    params = vp._read_efuse_params(pcap, dev)
    nums, eps = _frame_epochs(pcap)
    cmds = _parse_iw_channels(iw_log)
    print(f"{cap_name}: {len(cmds)} channel-set commands in iw.log "
          f"(rfe_type={params.rfe_type})")

    npass = nfail = nskip = 0
    for i, (epoch, ch) in enumerate(cmds):
        end_epoch = cmds[i + 1][0] if i + 1 < len(cmds) else epoch + 1.5
        s = bisect.bisect_left(eps, epoch)
        e = bisect.bisect_left(eps, end_epoch) - 1
        if s >= len(nums):
            continue
        e = min(e, len(nums) - 1)

        if ch > 14:                                   # 5 GHz — M5, not ported yet
            print(f"  ch {ch:>3}: SKIP (5 GHz — M5)")
            nskip += 1
            continue

        vp.WINDOW = (nums[s], nums[e])
        ops = vp.extract_ops(pcap, dev, trim_to_start=False)
        t = vp.ReplayTransport(ops)
        try:
            chan.set_channel_bw(t, ch, params.tx_power)
            print(f"  ch {ch:>3}: PASS — {t.i} ops byte-for-byte")
            npass += 1
        except vp.Divergence as d:
            print(f"  ch {ch:>3}: FAIL — {d}")
            nfail += 1

    print(f"\n{cap_name}: {npass} channels PASS, {nfail} FAIL, {nskip} skipped (5 GHz).")
    return 1 if nfail else 0


def main() -> int:
    name = Path(sys.argv[1] if len(sys.argv) > 1 else "capture-1").stem
    return verify(name)


if __name__ == "__main__":
    sys.exit(main())
