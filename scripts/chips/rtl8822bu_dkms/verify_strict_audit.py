"""Strict per-phase, whole-window audit — closes verify_channels' matched-prologue blind spot.

verify_channels replays each hop's set_channel_bw STRICTLY (good: any wrong-write screams) but then
reports only "matched N ops, first unmatched" and waves the rest of the window off as "post-power cal".
The 2.4 GHz antenna mux hid in exactly that unchecked tail. This tool keeps the strict replay (no
resync, so no cascade — a divergence is a real wrong-write) and then ENUMERATES the entire remaining
window as DARK, with a per-register histogram, so a hidden RX write in a tail can't slide by as
"post-power cal". Run per phase (initial-tune + every iw hop); roll up a DARK-register census.

A DARK tail legitimately contains: the per-channel DPK (TX pre-distortion), a watchdog tick if the 2 s
loop fired mid-window (0x994/0x9a4/0xa2c/0xb58/0x198c...), and on a crossing the coex band-notify.
Anything ELSE — especially a BB RX-config register — is a candidate gap to investigate.

cap-1 only (cap-2/3 are warm). Run: uv run python scripts/chips/rtl8822bu_dkms/verify_strict_audit.py
"""
from __future__ import annotations

import bisect
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts" / "porting"))

import rtw88_pcap_replay as rp  # noqa: E402
from wifit3.chips.rtl8822bu_dkms import chan, chipid, efuse, txpower, usbphy  # noqa: E402
from wifit3.chips.rtl8822bu_dkms.transport import Rtl8822buTransport  # noqa: E402

CAP_DIR = REPO / "driver_captures" / "captures_rtl88x2bu"
_IW_LINE = re.compile(r"^\[(\d+\.\d+)\] Executing:.*set channel (\d+)")
RF_A_0x18_READ = 0x2860
INITIAL_TUNE_FROM = 19966

# Registers a DARK hop-tail may legitimately contain (so the histogram highlights the unexpected).
# Watchdog cycle: fa_cnt_statistics (F-page counters) + reg-reset + env-monitor NHM/CLM/FAHM + DIG.
WATCHDOG_REGS = {0x0994, 0x0990, 0x0998, 0x099c, 0x09a0, 0x09a4, 0x0a2c, 0x0b58, 0x0c50, 0x0e50,
                 0x0a0a, 0x08a4, 0x0a0c, 0x198c, 0x08f8, 0x08fc, 0x0fa0, 0x0fa8, 0x0fac, 0x0fb0,
                 0x0f50, 0x0f48, 0x0f08, 0x0f04, 0x0f14, 0x0f1c, 0x0f10, 0x0f18, 0x0f0c, 0x0f54,
                 0x0fcc, 0x0fc8, 0x0fc4, 0x0fc0, 0x0fbc, 0x0fd0, 0x0a5c, 0x08c8, 0x2908,
                 0x1c38, 0x1c78, 0x1c7c, 0x1cb8, 0x0b04}
# Per-channel DPK (TX pre-distortion, G4) — the recurring ~60-op post-TXAGC tail.
DPK_REGS = {0x0fa4, 0x0fb4, 0x0280, 0x0283, 0x010c, 0x1988, 0x0840, 0x08d8, 0x004e, 0x0c94, 0x0e94,
            0x0c1c, 0x0e1c, 0x1bcc, 0x1b00}
TXAGC_COEX = {r for r in range(0x1d00, 0x1da0, 4)} | {0x0cbc, 0x0ebc}  # TXAGC table + coex band-notify
# Opmode/monitor-entry + band-switch/rfe (bleed into the coarse iw.log windows from adjacent phases).
PHASE_BLEED = {0x0102, 0x0115, 0x0422, 0x0541, 0x0542, 0x0550, 0x05b0, 0x060f, 0x06a0, 0x06a2,
               0x06a4, 0x07d4, 0x0440, 0x0814, 0x0a80, 0x08cc, 0x0c04, 0x0e04, 0x0cb0, 0x0eb0,
               0x0cb4, 0x0eb4, 0x0ca0, 0x0ea0, 0x19a8, 0x0a24, 0x0a28, 0x0881, 0x0883, 0x089d,
               0x089e, 0x089f, 0x1cf8, 0x1d3c}
EXPECTED = WATCHDOG_REGS | DPK_REGS | TXAGC_COEX | PHASE_BLEED | {0x04e0}  # +page-switch mirror


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


def _parse_iw(iw_log):
    cmds = []
    with open(iw_log) as f:
        for line in f:
            m = _IW_LINE.match(line)
            if m:
                cmds.append((float(m.group(1)), int(m.group(2))))
    return cmds


def _dark_tail(win, start_i):
    """Enumerate the window ops from start_i on (skipping the 0x4E0 page mirror) as a DARK histogram."""
    regs = Counter()
    for o in win[start_i:]:
        if o.get("wval") != 0x04E0:
            regs[o["wval"]] += 1
    return regs


def _fmt_hist(regs):
    known = sum(n for r, n in regs.items() if r in EXPECTED)
    unexp = {r: n for r, n in regs.items() if r not in EXPECTED}
    s = f"{sum(regs.values())} ops ({known} expected"
    if unexp:
        top = " ".join(f"0x{r:04x}x{n}" for r, n in sorted(unexp.items(), key=lambda x: -x[1])[:8])
        s += f", **{sum(unexp.values())} UNEXPECTED: {top}**"
    return s + ")"


def verify(cap_name="capture-1"):
    time.sleep = lambda *a, **k: None
    pcap = CAP_DIR / f"{cap_name}.pcap"
    dev = rp.find_card_device(pcap)
    ctrl = rp.extract_ctrl_ops(pcap, dev)
    nums, eps = _frame_epochs(pcap)
    cmds = _parse_iw(CAP_DIR / f"{cap_name}_logs" / "iw.log")

    ct = Rtl8822buTransport(rp.ReplayDevice(
        rp.merge_ops_by_frame(ctrl, rp.extract_bulk_out_ops(pcap, dev))))
    info = chipid.get_chip_info(ct)
    usbphy.phy_cfg_usb(ct, info.chip_ver)
    chipid.read_chip_version(ct)
    pg = txpower.parse_pg(efuse.read_efuse(ct).log_map)

    census = Counter()
    nfail = 0

    # --- Phase: initial 2.4 GHz tune (band switch + channel + bw + antenna notify) ---
    # Cap the window at the monitor-enable region (~f20405) so the tail is the tune's own, not the
    # entire rest of the capture.
    win = [o for o in ctrl if INITIAL_TUNE_FROM <= o["frame"] <= 20410]
    hi = next((i for i, o in enumerate(win) if o["dir"] == "IN" and o["wval"] == RF_A_0x18_READ), None)
    win = win[hi:]
    rdev = rp.ReplayDevice(win)
    t = Rtl8822buTransport(rdev)
    try:
        chan.set_channel_bw(t, 1, prev_ch=None, txpwr_pg=None)
        tail = _dark_tail(win, rdev.i)
        census.update(tail)
        print(f"initial-tune ch1: {rdev.i} ops byte-for-byte; DARK tail = {_fmt_hist(tail)}")
    except rp.Divergence as d:
        print(f"initial-tune ch1: FAIL (wrong write) -- {d}")
        nfail += 1

    # --- Phase: each iw hop, strict prologue + DARK-tail enumeration ---
    print("\nper-hop (strict set_channel_bw + DARK tail):")
    for i, (epoch, ch) in enumerate(cmds):
        prev = cmds[i - 1][1] if i > 0 else None
        crossing = prev is not None and (prev <= 14) != (ch <= 14)
        end = cmds[i + 1][0] if i + 1 < len(cmds) else epoch + 1.5
        s = bisect.bisect_left(eps, epoch)
        e = bisect.bisect_left(eps, end) - 1
        if s >= len(nums):
            continue
        win = [o for o in ctrl if nums[s] <= o["frame"] <= nums[min(e, len(nums) - 1)]]
        head = next((o for o in win if o["wval"] != 0x04E0), None)
        if not (head and head["dir"] == "IN" and head["wval"] == RF_A_0x18_READ):
            continue  # slice artifact (window opens mid-cal); verify_channels already flags these
        rdev = rp.ReplayDevice(win)
        t = Rtl8822buTransport(rdev)
        try:
            chan.set_channel_bw(t, ch, prev_ch=prev, txpwr_pg=(None if crossing else pg))
        except rp.Divergence as d:
            print(f"  ch {ch:>3}: FAIL (wrong write) -- {d}")
            nfail += 1
            continue
        tail = _dark_tail(win, rdev.i)
        census.update(tail)
        flag = "  <-- UNEXPECTED" if any(r not in EXPECTED for r in tail) else ""
        print(f"  ch {ch:>3}: {rdev.i:>3} ops clean; DARK tail = {_fmt_hist(tail)}{flag}")

    print("\n=== DARK-register census across all phases ===")
    unexp = {r: n for r, n in census.items() if r not in EXPECTED}
    print(f"  expected (watchdog/DPK/TXAGC/coex/mirror): "
          f"{sum(n for r, n in census.items() if r in EXPECTED)} ops")
    if unexp:
        print("  UNEXPECTED DARK registers (investigate — candidate gaps):")
        for r, n in sorted(unexp.items(), key=lambda x: -x[1]):
            print(f"    0x{r:04x}: {n}")
    else:
        print("  no unexpected DARK registers — every hop-tail op is a known watchdog/DPK/coex reg.")
    return 1 if nfail else 0


if __name__ == "__main__":
    raise SystemExit(verify())
