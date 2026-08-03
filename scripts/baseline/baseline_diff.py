"""Compare two baseline rollups (our driver vs Linux) and print a plain-sentence report.

A pure function of the two JSON rollups written by baseline_wifit3.py / baseline_linux.py (shared.py
says what a rollup is), so it is recomputed on demand and never stored. baseline_wifit3.py calls
diff() automatically after collecting; run it by hand to re-compare existing rollups:

    python baseline_diff.py --diff wifit3-rt5370.json linux-rt5370.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path


def band(channel: int) -> str:
    if 1 <= channel <= 14:
        return "2.4"
    if channel >= 36:
        return "5"
    return "?"


def _impossible(tuned: int, adv: int | None) -> bool:
    """True if a beacon advertising ``adv`` can't physically be heard while tuned to ``tuned``:
    a cross-band sighting, or (within 2.4 GHz) more than 4 channels apart. Adjacent-channel is real."""
    if adv is None:
        return False
    if band(tuned) != band(adv):
        return True
    if band(tuned) == "2.4":
        return abs(tuned - adv) > 4
    return tuned != adv


def load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _bssids_by_band(rollup: dict, b: str) -> set[str]:
    out: set[str] = set()
    for ch, data in rollup["channels"].items():
        if band(int(ch)) == b:
            out.update(data["aps"])
    return out


def _best_rate(rollup: dict) -> tuple[str | None, float]:
    """The single most-heard BSSID and its per-second rate: the default reference AP."""
    best_b, best_n, best_rate = None, -1, 0.0
    for data in rollup["channels"].values():
        for bssid, ap in data["aps"].items():
            if ap["beacons"] > best_n:
                best_b, best_n, best_rate = bssid, ap["beacons"], ap["beacons_per_sec"]
    return best_b, best_rate


def _mean_rssi(rollup: dict, bssid: str) -> float | None:
    vals = [
        data["aps"][bssid]["mean_rssi"]
        for data in rollup["channels"].values()
        if bssid in data["aps"] and data["aps"][bssid]["mean_rssi"] is not None
    ]
    return statistics.mean(vals) if vals else None


def _peers(path: Path, source: str) -> list[dict]:
    """Other cards' rollups in the same dir (``<source>-*.json``), for the best-card comparison."""
    out = []
    for p in sorted(path.parent.glob(f"{source}-*.json")):
        if p.resolve() == path.resolve():
            continue
        try:
            out.append(load(p))
        except (OSError, ValueError):
            pass
    return out


def _best_channel_persec(rollup: dict, bssid: str) -> tuple[dict, int]:
    """(per_sec, span) for ``bssid`` on the channel where it was heard most."""
    best = None
    for d in rollup["channels"].values():
        ap = d["aps"].get(bssid)
        if ap and (best is None or ap["beacons"] > best[0]):
            best = (ap["beacons"], ap["per_sec"], d["seconds"])
    return (best[1], best[2]) if best else ({}, 0)


def _rate_matched(w: dict, lin: dict, bssid: str) -> tuple[float, float, int]:
    """Beacon rate for ``bssid`` in each rollup (``w``=wifit3, ``lin``=linux) over their common
    window (the shorter dwell span, so a span difference doesn't skew it). Returns (wrate, lrate, window)."""
    wps, wspan = _best_channel_persec(w, bssid)
    lps, lspan = _best_channel_persec(lin, bssid)
    window = min(wspan, lspan) or max(wspan, lspan)
    if window == 0:
        return 0.0, 0.0, 0
    wsum = sum(v for s, v in wps.items() if int(s) < window)
    lsum = sum(v for s, v in lps.items() if int(s) < window)
    return wsum / window, lsum / window, window


def diff(wifit3_path: str | Path, linux_path: str | Path,
         ref_bssids: list[str] | None = None) -> None:
    w, lin = load(wifit3_path), load(linux_path)
    chip = w.get("chip", "?")
    peers = _peers(Path(wifit3_path), "wifit3")
    out = [f"\nwifit3 vs linux   {chip}\n"]
    ref_bssids = [b.lower() for b in ref_bssids] if ref_bssids else None

    # Breadth, per band.
    for b in ("2.4", "5"):
        wn, ln = len(_bssids_by_band(w, b)), len(_bssids_by_band(lin, b))
        if not wn and not ln:
            continue
        line = f"Breadth {b} GHz: {wn} access points | "
        line += "matches linux" if wn >= ln else f"{ln - wn} fewer than linux ({ln})"
        if peers:
            best = max((len(_bssids_by_band(p, b)) for p in peers), default=0)
            best = max(best, wn)
            line += " | best so far" if wn >= best else f" | {best - wn} fewer than best card ({best})"
        out.append(line)

    # Beacon rate off the reference AP(s), over a matched window (see _rate_matched). 0.3/s is
    # sampling-noise tolerance. --ref pins stable BSSIDs; default is the AP linux heard most.
    refs = ref_bssids or ([b] if (b := _best_rate(lin)[0]) else [])
    for ref in refs:
        label = ref if ref_bssids else "reference AP"
        wrate, lrate, window = _rate_matched(w, lin, ref)
        if window == 0:
            out.append(f"Beacon rate ({label}): not heard in either capture")
            continue
        line = f"Beacon rate ({label}, matched {window}s window): {wrate:.1f}/sec | "
        line += ("matches linux" if wrate >= lrate - 0.3
                 else f"{lrate - wrate:.1f} below linux ({lrate:.1f}/sec)")
        out.append(line)

    # RSSI agreement on common BSSIDs (same card, so a consistent gap is a decode bug).
    common = set().union(*(set(d["aps"]) for d in w["channels"].values())) & set().union(
        *(set(d["aps"]) for d in lin["channels"].values())
    ) if w["channels"] and lin["channels"] else set()
    deltas = []
    for bssid in common:
        wr, lr = _mean_rssi(w, bssid), _mean_rssi(lin, bssid)
        if wr is not None and lr is not None:
            deltas.append(wr - lr)
    if deltas:
        med = statistics.median(deltas)
        worst = max(deltas, key=abs)
        out.append(
            f"RSSI: median {med:+.1f} dB vs linux ({len(deltas)} common APs) | worst {worst:+.0f} dB"
        )

    # Channel tune: per tuned channel, heard-its-own / silent / cross-channel.
    heard = silent = cross = 0
    total = len(w["channels"])
    for ch, data in w["channels"].items():
        ch = int(ch)
        aps = data["aps"]
        if not aps:
            silent += 1
            continue
        if any(not _impossible(ch, ap["adv_channel"]) for ap in aps.values()):
            heard += 1
        cross += sum(1 for ap in aps.values() if _impossible(ch, ap["adv_channel"]))
    out.append(
        f"Channel tune: {heard}/{total} channels heard their own beacons | "
        f"{silent} silent | {cross} cross-channel"
    )

    print("\n".join(out) + "\n")


def main() -> int:
    p = argparse.ArgumentParser(description="Diff two baseline rollups (wifit3 vs linux).")
    p.add_argument("--diff", nargs=2, metavar=("WIFIT3_JSON", "LINUX_JSON"), required=True)
    p.add_argument("--ref", nargs="+", metavar="BSSID", default=None,
                   help="Pin one or more reference BSSIDs for the beacon-rate line "
                        "(e.g. a fixed AP per band). Default: the single AP linux heard most.")
    args = p.parse_args()
    diff(args.diff[0], args.diff[1], ref_bssids=args.ref)
    return 0


if __name__ == "__main__":
    sys.exit(main())
