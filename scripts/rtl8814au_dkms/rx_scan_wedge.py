"""RTL8814AU — reproduce the REAL scanner-path (scan=True) 5->2 RX wedge, rigorously.

Every prior wedge script tested the wrong thing: rx_wedge_regdiff.py ran with DIG OFF
(the --no-dig confound) AND used scan=False tunes (the reader-pause + pipe-reset branch,
which the scanner never hits); rx_pipe_probe.py dwelled scan=False too. So a "fix" gated
on scan=False could look verified while the scanner path stayed broken.

This drives the EXACT app path:
  * hop 5 GHz with scan=True (like the scanner filtered to 5 GHz),
  * then ONE scan=True tune to a 2.4 GHz channel and DWELL WITHOUT re-tuning
    (modelling _hop_loop's same-channel skip: a single-channel filter tunes once),
  * DIG stays ON (the real path; no --no-dig).

It repeats N trials and reports a wedge RATE, not a single verdict — a probabilistic
wedge + single-trial checks is how the earlier "fixes" got lucky. On every trial it also
snapshots a curated RF+BB front-end register fingerprint, INCLUDING the RF chip's own 0x18
per path (channel/band/BW) which rx_wedge_regdiff never read, and at the end diffs the
wedged-trial fingerprints against the healthy ones — to see whether the 5->2 cross leaves a
specific front-end register in a receive-off state (which is what fa=0 in the field implies).

Read-only: set_channel + register READS only. No TX.

    uv run python scripts/rtl8814au_dkms/rx_scan_wedge.py --trials 10 [--ref <BSSID>]
        [--dwell-ch 6] [--variant cross5|same2|cold]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wifit3.chips.rtl8814au_dkms.constants import CHANNELS_5G_NON_DFS  # noqa: E402
from wifit3.chips.rtl8814au_dkms.rf import _rf_read  # noqa: E402
from wifit3.wlan.manager import WlanDeviceManager  # noqa: E402
from wifit3.wlan.packet import WlanFrameParser  # noqa: E402

_HOP5 = list(CHANNELS_5G_NON_DFS)          # the scanner's 5 GHz set
_LAST5 = 149                               # always cross from this channel, for comparable trials
_SAME2 = [1, 6, 11]                        # same-band hop set for the control variant


def snap_regs(t) -> dict:
    """Curated, non-destructive front-end fingerprint. RF-chip regs read via the memory-mapped
    SIPI path (base + addr*4); BB regs are counters/config, not read-to-clear FIFOs."""
    d: dict[str, int] = {}
    for p in ("a", "b", "c", "d"):
        d[f"RF{p}.18_chnlbw"] = _rf_read(t, p, 0x18)   # channel | band | bw
        d[f"RF{p}.00_mode"] = _rf_read(t, p, 0x00)     # RF mode/enable
    d["0454_cckcheck"] = t.read8(0x454)                # bit7 = HW band (5G if set)
    d["1002_clkgate"] = t.read8(0x1002)                # bit0 = CCK/OFDM clock gate
    d["0808_ofdmccken"] = t.read32(0x808)              # bit29 OFDM en, bit28 CCK en / RX path
    d["080c_txpath"] = t.read32(0x80C)
    d["0a04_cckrx"] = t.read32(0xA04)
    d["08ac_rfmod"] = t.read32(0x8AC)                  # ADC bw
    d["082c_agcbw"] = t.read32(0x82C)                  # AGC bw
    d["0958_agcsel"] = t.read32(0x958)                 # AGC-table select
    d["0860_fcarea"] = t.read32(0x860)                 # center-freq area
    d["0a80"] = t.read32(0xA80)
    d["1abc_rfeinv"] = t.read32(0x1ABC)                # RFE inversion (2.4G 0x77 / 5G 0x33)
    for reg, name in ((0xC50, "IGIa"), (0xE50, "IGIb"), (0x1850, "IGIc"), (0x1A50, "IGId")):
        d[f"{reg:04x}_{name}"] = t.read32(reg) & 0x7F
    d["0f48_ofdmfa"] = t.read16(0xF48)
    d["0a5c_cckfa"] = t.read16(0xA5C)
    return d


async def run(args: argparse.Namespace) -> int:
    ref = args.ref.lower() if args.ref else None
    mgr = WlanDeviceManager()
    ifaces = await mgr.refresh()
    if not ifaces:
        print("[-] no device", file=sys.stderr)
        return 1
    iface = ifaces[0]
    print(f"[*] bring up {iface.name} (DIG ON — the real path)", file=sys.stderr)
    if not await iface.connect(progress_cb=lambda p, m: None):
        print("[-] bring-up failed", file=sys.stderr)
        await mgr.close_all()
        return 1
    t = iface.driver.transport
    cnt = {"frames": 0, "ref": 0}

    def on_rx(raw: bytes, rssi: int, ts: float) -> None:
        cnt["frames"] += 1
        if not ref:
            return
        try:
            p = WlanFrameParser.parse_80211_frame(raw, rssi)
        except Exception:  # noqa: BLE001
            return
        if p and p.type == "beacon" and p.bssid and p.bssid.lower() == ref:
            cnt["ref"] += 1
    iface.register_rx_callback(on_rx)

    async def snap() -> dict:
        async with iface.driver._io_lock:       # don't race the DIG watchdog / a tune on EP0
            return await asyncio.get_running_loop().run_in_executor(None, snap_regs, t)

    async def hop(channels: list[int], secs: float) -> None:
        loop_end = asyncio.get_running_loop().time() + secs
        i = 0
        while asyncio.get_running_loop().time() < loop_end:
            await iface.set_channel(channels[i % len(channels)], scan=True)
            await asyncio.sleep(0.5)
            i += 1

    dch = args.dwell_ch
    variant = args.variant
    print(f"[*] variant={variant} dwell_ch={dch} trials={args.trials} "
          f"hop5={args.hop5_secs}s dwell={args.dwell_secs}s ref={ref or '-'}\n", file=sys.stderr)

    # Healthy baseline: a plain scan=True tune to the dwell channel, no band cross, then observe.
    await iface.set_channel(dch, scan=True)
    await asyncio.sleep(1.0)
    base_frames = cnt["frames"]
    await asyncio.sleep(3.0)
    base_rate = (cnt["frames"] - base_frames) / 3.0
    base_fp = await snap()
    print(f"[baseline] cold ch{dch}: {base_rate:.1f} frames/s  "
          f"(IGIa=0x{base_fp['0c50_IGIa']:02x} RFa.18=0x{base_fp['RFa.18_chnlbw']:05x})\n",
          file=sys.stderr)

    results = []       # (trial, wedged: bool, per_sec: list[int], fingerprint)
    try:
        for trial in range(1, args.trials + 1):
            if variant == "cross5":
                await hop(_HOP5, args.hop5_secs)
                await iface.set_channel(_LAST5, scan=True)     # deterministic last 5 GHz channel
                await asyncio.sleep(0.5)
            elif variant == "same2":
                await hop([c for c in _SAME2 if c != dch] or _SAME2, args.hop5_secs)
            # 'cold': no hop — just (re)tune the dwell channel below.

            # THE dwell tune — one scan=True tune, then observe WITHOUT re-tuning (channel-skip).
            # --pause-cross wraps ONLY this cross tune in the scan=False dance (pause reader ->
            # tune -> reprime pipe -> resume), to isolate whether the live reader racing the band
            # switch is what wedges the RF. No driver change: the hypothesis is tested reversibly.
            if args.pause_cross:
                loop = asyncio.get_running_loop()
                reader = iface.driver._reader
                await loop.run_in_executor(None, reader.pause)
                try:
                    await iface.set_channel(dch, scan=True)
                    await loop.run_in_executor(None, t.reset_rx_pipe)
                finally:
                    reader.resume()
            else:
                await iface.set_channel(dch, scan=True)
            per_sec = []
            fp = None
            f_prev = cnt["frames"]
            for s in range(int(args.dwell_secs)):
                await asyncio.sleep(1.0)
                f_now = cnt["frames"]
                per_sec.append(f_now - f_prev)
                f_prev = f_now
                if s == 3:                     # snapshot after classification is clear, still wedged
                    fp = await snap()
            wedged = sum(per_sec[:3]) < args.wedge_th
            results.append((trial, wedged, per_sec, fp or await snap()))
            tag = "WEDGED" if wedged else "ok"
            print(f"[trial {trial:>2}] {tag:>6}  per-sec={per_sec}  "
                  f"IGIa=0x{results[-1][3]['0c50_IGIa']:02x} "
                  f"fa(ofdm/cck)={results[-1][3]['0f48_ofdmfa']}/{results[-1][3]['0a5c_cckfa']}",
                  file=sys.stderr)
    finally:
        await mgr.close_all()

    n_wedge = sum(1 for _, w, _, _ in results if w)
    print(f"\n=== WEDGE RATE: {n_wedge}/{len(results)} "
          f"(variant={variant}, dwell ch{dch}) ===", file=sys.stderr)

    # Fingerprint diff: any register that differs between the wedged and healthy populations.
    wedged_fps = [fp for _, w, _, fp in results if w]
    healthy_fps = [fp for _, w, _, fp in results if not w] + [base_fp]
    if wedged_fps and healthy_fps:
        print("\n=== register diff: WEDGED vs HEALTHY (only differing keys) ===", file=sys.stderr)
        w0, h0 = wedged_fps[0], healthy_fps[0]
        for k in base_fp:
            wv = [fp[k] for fp in wedged_fps]
            hv = [fp[k] for fp in healthy_fps]
            if set(wv) != set(hv):
                consistent = " <== CONSISTENT" if len(set(wv)) == 1 and len(set(hv)) == 1 else ""
                print(f"  {k:>16}: wedged={[hex(v) for v in wv]} healthy={[hex(v) for v in hv]}{consistent}",
                      file=sys.stderr)
    elif not wedged_fps:
        print("  (no wedge reproduced this run — nothing to diff)", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="8814au: rigorous scan=True 5->2 RX-wedge repro + register fingerprint.")
    p.add_argument("--ref", default=None, help="pin a 2.4 GHz reference BSSID (runtime only, never committed).")
    p.add_argument("--dwell-ch", type=int, default=6, help="the 2.4 GHz channel to dwell on (default 6).")
    p.add_argument("--variant", choices=("cross5", "same2", "cold"), default="cross5",
                   help="cross5: hop 5 GHz then cross to 2.4 (the real trigger); "
                        "same2: hop 2.4 only (isolates band-cross vs hop-dwell); cold: no hop (control).")
    p.add_argument("--trials", type=int, default=10)
    p.add_argument("--hop5-secs", type=float, default=15.0)
    p.add_argument("--dwell-secs", type=float, default=10.0)
    p.add_argument("--wedge-th", type=int, default=10, help="frames in the first 3 dwell seconds below this = wedged.")
    p.add_argument("--pause-cross", action="store_true",
                   help="pause the RX reader + reprime the pipe around the 5->2 cross tune "
                        "(the candidate fix, tested reversibly without a driver change).")
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
