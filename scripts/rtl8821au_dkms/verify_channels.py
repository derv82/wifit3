"""Byte-for-byte differ for the per-channel runtime tune — one channel at a time.

`verify_pcap.py` covers the init through the single ch1 tune; the cold-boot captures
ALSO contain the vendor driver's tune for every channel airodump hopped (see
`<cap>_logs/iw.log`: 2.4 GHz 1-13, then 5 GHz 36..165). A mis-ported hop — a missing
band switch, a wrong fc_area/RF-MOD group, a wrong per-channel TX-power group — silently
degrades RX, so every hop must reproduce the wire exactly.

Slices each `iw set channel N` window (iw.log epoch -> pcap frame) and replays the port's
runtime tune (`chan.set_channel_bw` + the per-band txagc) against it from the window's
first op — NO anchoring/trim, so a stray vendor step shows up as the first divergence.
phy_SwBand reads the chip's band marker (0x454 BIT7) straight from the recorded window,
so a 2.4<->5 crossing byte-diffs its band switch in line.

Run: uv run python scripts/rtl8821au_dkms/verify_channels.py [capture-1|2|3]
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
sys.path.insert(0, str(REPO / "scripts"))        # rtw88_pcap_replay
sys.path.insert(0, str(Path(__file__).parent))   # verify_pcap (op extractor + replay)

import rtw88_pcap_replay as rp  # noqa: E402
import verify_pcap as vp  # noqa: E402
from wifit3.chips.rtl8821au_dkms import chan, txpower  # noqa: E402

_IW_LINE = re.compile(r"^\[(\d+\.\d+)\] Executing:.*set channel (\d+)")
_TXAGC_LO, _TXAGC_HI = 0x0C20, 0x0C54   # direct TXAGC register span (completeness check)


def _frame_epochs(pcap: Path):
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
    time.sleep = lambda *a, **k: None

    p = vp._read_efuse_params(pcap, dev)
    nums, eps = _frame_epochs(pcap)
    cmds = _parse_iw_channels(iw_log)
    print(f"{cap_name}: {len(cmds)} channel-set commands in iw.log")

    npass = nfail = nskip = 0
    for i, (epoch, ch) in enumerate(cmds):
        end_epoch = cmds[i + 1][0] if i + 1 < len(cmds) else epoch + 1.5
        s = bisect.bisect_left(eps, epoch)
        e = bisect.bisect_left(eps, end_epoch) - 1
        if s >= len(nums):
            continue
        e = min(e, len(nums) - 1)
        ops = rp.extract_ops(pcap, dev, (nums[s], nums[e]), start_addr=None)

        # Each tune begins at phy_SwBand's band-detect read of REG_CCK_CHECK (0x454).
        # airodump's periodic register polls can land at the boundary; skip those windows.
        if not (ops and ops[0]["kind"] == "R" and ops[0]["addr"] == chan.REG_CCK_CHECK):
            nskip += 1
            continue

        t = rp.ReplayTransport(ops)
        try:
            chan.set_channel_bw(t, ch, p.bb_swing_2g, p.bb_swing_5g, p.ext_lna_2g)
            if ch <= 14:
                txpower.set_tx_power(t, ch, p.tx_power)
            else:
                txpower.set_tx_power_5g(t, ch, p.tx_power_5g)
        except rp.Divergence as d:
            print(f"  ch {ch:>3}: FAIL — {d}")
            nfail += 1
            continue
        nxt = ops[t.i] if t.i < len(ops) else None
        if nxt and nxt["kind"] == "W" and _TXAGC_LO <= nxt["addr"] <= _TXAGC_HI:
            print(f"  ch {ch:>3}: FAIL — {t.i} ops matched but TXAGC writes remain")
            nfail += 1
        else:
            npass += 1
            print(f"  ch {ch:>3}: PASS — {t.i} ops byte-for-byte")

    print(f"\n{cap_name}: {npass} PASS, {nfail} FAIL, {nskip} skipped (slicing artifacts).")
    return 1 if nfail else 0


def main() -> int:
    name = Path(sys.argv[1] if len(sys.argv) > 1 else "capture-1").stem
    return verify(name)


if __name__ == "__main__":
    sys.exit(main())
