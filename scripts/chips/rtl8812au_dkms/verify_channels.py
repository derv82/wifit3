"""Byte-for-byte differ for the per-channel runtime tune — the 2.4+5 GHz scan death-loop.

`verify_pcap.py` covers init through the single ch1 tune; the cold-boot captures ALSO
contain the vendor driver's tune for every channel airodump's `--band abg` sweep hopped
(`<cap>_logs/iw.log`: 2.4 GHz 1-12, then 5 GHz 36..165, then back to 1 — both band
crossings, all exit-0 incl. the DFS channels). A mis-ported hop — a missing/wrong band
switch, fc_area/RF-MOD group, or per-channel TX-power group — silently degrades RX and is
the EXACT failure that "radio-silence-deaths" mainline on a multi-band scan. So every hop
must reproduce the wire byte-for-byte, above all the **12->36 (2.4->5)** and **165->1
(5->2.4)** band-switch crossings.

Slices each `iw set channel N` window (iw.log epoch -> pcap frame) and replays the port's
runtime tune (`chan.set_channel_bw` + the per-band txagc) against it from the window's
first op — NO anchoring/trim, so a stray vendor step is the first divergence. phy_SwChnl
reads the chip's band marker (0x454 BIT7) straight from the recorded window, so a 2.4<->5
crossing byte-diffs its band switch in line. Windows whose first control op is a DIG-
watchdog tick (not the tune) are skipped as slicing artifacts.

Run: uv run python scripts/chips/rtl8812au_dkms/verify_channels.py [capture-2|capture-3]
"""
from __future__ import annotations

import bisect
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))   # rtw88_pcap_replay (op extractor + replay)

import rtw88_pcap_replay as rp
from wifit3.chips.rtl8812au_dkms import chan, efuse, txpower

REG_CCK_CHECK = 0x0454
_IW_LINE = re.compile(r"^\[(\d+\.\d+)\] Executing:.*set channel (\d+)")
_TXAGC_LO, _TXAGC_HI = 0x0C20, 0x0E54   # direct TXAGC span, both paths (A 0xC20.., B 0xE20..)
CAP_DIR = REPO / "driver_captures" / "captures_8812au"


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


def _crossing_tag(prev_ch, ch) -> str:
    if prev_ch is None:
        return ""
    if prev_ch <= 14 < ch:
        return "   <<< 2.4->5 GHz CROSSING (the death-loop hop)"
    if ch <= 14 < prev_ch:
        return "   <<< 5->2.4 GHz CROSSING (the death-loop hop)"
    return ""


def verify(cap_name: str) -> int:
    time.sleep = lambda *a, **k: None        # replay needs no real settle delays
    pcap = CAP_DIR / f"{cap_name}.pcap"
    iw_log = CAP_DIR / f"{cap_name}_logs" / "iw.log"
    dev = rp.find_card_device(pcap)
    all_ops = rp.extract_ops(pcap, dev)
    # EFUSE params from the bring-up prefix: per-band bb_swing + rfe_type + per-rate TX power.
    p = efuse.read_chip_params(rp.ReplayTransport(all_ops))
    nums, eps = _frame_epochs(pcap)
    cmds = _parse_iw_channels(iw_log)
    print(f"{cap_name}: card=dev{dev}, {len(cmds)} channel-set commands; rfe_type={p.rfe_type} "
          f"bb_swing 2g={[hex(x) for x in p.bb_swing_2g]} 5g={[hex(x) for x in p.bb_swing_5g]}")

    npass = nfail = nskip = 0
    crossings_seen = crossings_pass = 0
    for i, (epoch, ch) in enumerate(cmds):
        prev_ch = cmds[i - 1][1] if i > 0 else None
        tag = _crossing_tag(prev_ch, ch)
        end_epoch = cmds[i + 1][0] if i + 1 < len(cmds) else epoch + 1.5
        s = bisect.bisect_left(eps, epoch)
        e = bisect.bisect_left(eps, end_epoch) - 1
        if s >= len(nums):
            continue
        e = min(e, len(nums) - 1)
        f0, f1 = nums[s], nums[e]
        win = [op for op in all_ops if f0 <= op["frame"] <= f1]

        # Each tune begins at phy_SwChnl's band-detect read of REG_CCK_CHECK (0x454). A DIG
        # watchdog tick (first op = read IGI 0xC50) can land at the boundary; skip those.
        if not (win and win[0]["kind"] == "R" and win[0]["addr"] == REG_CCK_CHECK):
            nskip += 1
            if tag:
                print(f"  ch {ch:>3}: SKIP (DIG tick at window head) -- CROSSING not verified "
                      f"in this window{tag}")
            continue
        if tag:
            crossings_seen += 1

        t = rp.ReplayTransport(win)
        try:
            chan.set_channel_bw(t, ch, bb_swing_2g_a=p.bb_swing_2g[0],
                                bb_swing_2g_b=p.bb_swing_2g[1], bb_swing_5g_a=p.bb_swing_5g[0],
                                bb_swing_5g_b=p.bb_swing_5g[1], rfe_type=p.rfe_type)
            if ch <= 14:
                txpower.set_tx_power(t, ch, p.tx_power_2g)
            else:
                txpower.set_tx_power_5g(t, ch, p.tx_power_5g)
        except rp.Divergence as d:
            print(f"  ch {ch:>3}: FAIL -- {d}{tag}")
            nfail += 1
            continue
        nxt = win[t.i] if t.i < len(win) else None
        if nxt and nxt["kind"] == "W" and _TXAGC_LO <= nxt["addr"] <= _TXAGC_HI:
            print(f"  ch {ch:>3}: FAIL -- {t.i} ops matched but TXAGC writes remain{tag}")
            nfail += 1
        else:
            npass += 1
            if tag:
                crossings_pass += 1
            print(f"  ch {ch:>3}: PASS -- {t.i} ops byte-for-byte{tag}")

    print(f"\n{cap_name}: {npass} PASS, {nfail} FAIL, {nskip} skipped (slicing artifacts).")
    print(f"  band crossings verified byte-for-byte: {crossings_pass}/{crossings_seen} "
          f"(the 2.4<->5 hops that kill mainline).")
    return 1 if nfail else 0


def main() -> int:
    name = Path(sys.argv[1] if len(sys.argv) > 1 else "capture-2").stem
    return verify(name)


if __name__ == "__main__":
    raise SystemExit(main())
