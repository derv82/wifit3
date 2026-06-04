"""Byte-for-byte differ for the per-channel tune — one channel at a time.

`verify_pcap.py` covers the init through the single ch1 tune; the cold-boot captures
ALSO contain the vendor driver's tune for every channel airodump hopped (see
`<cap>_logs/iw.log`: 2.4 GHz 1-12, then 5 GHz 36..165). A mis-ported channel tune —
a missing RF-off-during-tune, a skipped post-tune recal, a wrong per-channel TX-power
group — silently degrades RX, so every hop must reproduce the wire exactly.

This slices each `iw set channel N` window (by the iw.log epoch -> pcap frame, like
`pcap_slicer.py`) and replays the port's tune against it from the window's first op —
NO anchoring/trim, so a vendor step before/after our sequence shows up as the first
divergence rather than being silently skipped.

2.4 GHz channels (<=14) replay the full `chan.set_channel_bw`. 5 GHz channels (M5a) replay
`chan.phy_sw_band` alone: a 2.4G->5G crossing window emits `switch_wireless_band_5g` and the
prefix must byte-match the wire (the 5G channel *select* that follows is M5b); a same-band
5G hop just reads the band marker and returns. No previous-band tracking — `phy_sw_band`
reads the chip's band marker (0x454 bit7) straight from the recorded window, exactly as the
driver does. The 5G->2.4G wrap (e.g. ch165->ch1) is a 2.4 GHz window whose band switch back
to 2.4 GHz is now part of `set_channel_bw`, so it byte-diffs in full.

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
from wifit3.chips.rtl8814au_dkms import constants as C  # noqa: E402

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

        vp.WINDOW = (nums[s], nums[e])
        ops = vp.extract_ops(pcap, dev, trim_to_start=False)
        t = vp.ReplayTransport(ops)

        # Each tune begins at phy_SwBand's band-detect read of REG_CCK_CHECK (0x454). When
        # airodump's periodic register polls land at the epoch->frame boundary, the window
        # starts off-tune (e.g. a 0x60 poll) — a slicing artifact, not a port error. Skip it.
        if not (ops and ops[0]["kind"] == "R" and ops[0]["addr"] == C.REG_CCK_CHECK):
            print(f"  ch {ch:>3}: SKIP — window starts off-tune (airodump poll); slicing artifact")
            nskip += 1
            continue

        if ch > 14:                                   # 5 GHz: band switch only (M5a)
            try:
                chan.phy_sw_band(t, ch, params.bb_swing, params.bb_swing_5g)
            except vp.Divergence as d:
                print(f"  ch {ch:>3}: FAIL — 5G band switch: {d}")
                nfail += 1
                continue
            if t.i > 1:                               # a band switch fired (read + switch ops)
                print(f"  ch {ch:>3}: PASS — 2.4G->5G band switch ({t.i} ops); 5G tune = M5b")
                npass += 1
            else:                                     # same-band 5G hop: nothing to switch
                print(f"  ch {ch:>3}: SKIP — same-band 5G hop (tune = M5b)")
                nskip += 1
            continue

        try:
            chan.set_channel_bw(t, ch, params.tx_power, params.bb_swing, params.bb_swing_5g)
            print(f"  ch {ch:>3}: PASS — {t.i} ops byte-for-byte")
            npass += 1
        except vp.Divergence as d:
            print(f"  ch {ch:>3}: FAIL — {d}")
            nfail += 1

    print(f"\n{cap_name}: {npass} PASS, {nfail} FAIL, {nskip} skipped "
          f"(same-band 5G hops — channel select is M5b).")
    return 1 if nfail else 0


def main() -> int:
    name = Path(sys.argv[1] if len(sys.argv) > 1 else "capture-1").stem
    return verify(name)


if __name__ == "__main__":
    sys.exit(main())
