"""Shared core for the card health check.

Both collectors feed the SAME call (``feed(timestamp, parsed, rssi, channel)``),
so a beacon stream from our driver and one from a Linux pcap get grouped by
identical code. Any difference in the rollup is therefore the driver or the RF,
never the maths.

- ``Health`` accumulates a stream into a per-channel / per-BSSID rollup and dumps
  it to JSON (aggregated only, never raw frames, so it stays small + readable).
- ``diff`` reads two rollups (wifit3 vs linux) and prints a plain-sentence
  comparison. It is a pure function of the JSONs, so it is recomputed on demand
  and never stored.

Run::

    python driver_health.py --diff wifit3-rt5370.json linux-rt5370.json
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wifit3.dot11.packet import Packet


def band(channel: int) -> str:
    if 1 <= channel <= 14:
        return "2.4"
    if channel >= 36:
        return "5"
    return "?"


def _impossible(tuned: int, adv: int | None) -> bool:
    """True if a beacon advertising channel ``adv`` cannot physically have been
    heard while tuned to ``channel``: a cross-band sighting, or a 2.4 GHz
    channel more than 4 apart (non-overlapping). Adjacent-channel bleed in 2.4
    GHz is real RF and is NOT flagged."""
    if adv is None:
        return False
    if band(tuned) != band(adv):
        return True
    if band(tuned) == "2.4":
        return abs(tuned - adv) > 4
    return tuned != adv


class Health:
    def __init__(self, chip: str, source: str) -> None:
        self.chip = chip
        self.source = source  # "wifit3" | "linux"
        # channel -> bssid -> {beacons, rssi[list], secs{sec:count}, adv_channel}
        self._ch: dict[int, dict[str, dict]] = {}
        self._t0: dict[int, float] = {}

    def feed(self, ts: float, parsed: "Packet | None", rssi: int | None, channel: int) -> None:
        if not parsed or parsed.type != "beacon":
            return
        bssid = (parsed.bssid or "").lower()
        if not bssid:
            return
        aps = self._ch.setdefault(channel, {})
        ap = aps.setdefault(
            bssid, {"beacons": 0, "rssi": [], "secs": {}, "adv_channel": parsed.channel}
        )
        ap["beacons"] += 1
        if rssi is not None:
            ap["rssi"].append(rssi)
        t0 = self._t0.setdefault(channel, ts)
        sec = str(int(ts - t0))
        ap["secs"][sec] = ap["secs"].get(sec, 0) + 1

    def rollup(self) -> dict:
        chans: dict[str, dict] = {}
        for ch, aps in sorted(self._ch.items()):
            span = max(
                (max((int(s) for s in ap["secs"]), default=0) for ap in aps.values()),
                default=0,
            ) + 1
            ap_out = {}
            for bssid, ap in aps.items():
                rssi = ap["rssi"]
                ap_out[bssid] = {
                    "beacons": ap["beacons"],
                    "beacons_per_sec": round(ap["beacons"] / span, 2),
                    "mean_rssi": round(statistics.mean(rssi), 1) if rssi else None,
                    "adv_channel": ap["adv_channel"],
                    "per_sec": ap["secs"],
                }
            chans[str(ch)] = {"seconds": span, "aps": ap_out}
        return {"chip": self.chip, "source": self.source, "channels": chans}

    def to_json(self, path: str | Path) -> None:
        path = Path(path)
        _archive(path)   # snapshot the prior run before overwriting, so history isn't lost
        path.write_text(json.dumps(self.rollup(), indent=2), encoding="utf-8")
        print(f"[+] wrote {path}", file=sys.stderr)


def _archive(path: Path) -> None:
    """Copy an existing rollup into ``history/`` before it is overwritten, stamped with the
    file's own mtime (i.e. when THAT run was captured, not now). The working files keep their
    fixed names (the diff stays fresh + recomputable) while every prior run is preserved.
    ``history/`` is gitignored (rollups carry live BSSIDs, which never go to git)."""
    if not path.exists():
        return
    hist = path.parent / "history"
    hist.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(path.stat().st_mtime))
    shutil.copy2(path, hist / f"{path.stem}-{stamp}.json")


def load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _bssids_by_band(rollup: dict, b: str) -> set[str]:
    out: set[str] = set()
    for ch, data in rollup["channels"].items():
        if band(int(ch)) == b:
            out.update(data["aps"])
    return out


def _best_rate(rollup: dict) -> tuple[str | None, float]:
    """The single most-heard BSSID and its best per-second rate: the reference AP."""
    best_b, best_n, best_rate = None, -1, 0.0
    for data in rollup["channels"].values():
        for bssid, ap in data["aps"].items():
            if ap["beacons"] > best_n:
                best_b, best_n, best_rate = bssid, ap["beacons"], ap["beacons_per_sec"]
    return best_b, best_rate


def _rate_for(rollup: dict, bssid: str) -> float:
    best = 0.0
    for data in rollup["channels"].values():
        ap = data["aps"].get(bssid)
        if ap:
            best = max(best, ap["beacons_per_sec"])
    return best


def _mean_rssi(rollup: dict, bssid: str) -> float | None:
    vals = [
        data["aps"][bssid]["mean_rssi"]
        for data in rollup["channels"].values()
        if bssid in data["aps"] and data["aps"][bssid]["mean_rssi"] is not None
    ]
    return statistics.mean(vals) if vals else None


def _peers(path: Path, source: str) -> list[dict]:
    """Other cards' rollups in the same dir (``<source>-*.json``) for best-card gaps."""
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
    """Beacon rate for ``bssid`` in each rollup over a COMMON window: the min of the
    two observed spans. Removes the dwell/span asymmetry (baseline-wifit3 dwells ~16 s
    vs baseline-linux ~14 s, and rate = beacons/span) that otherwise reads as a
    systematic ~12% penalty on wifit3 on every AP. Returns (wrate, lrate, window)."""
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

    # Beacon rate from the reference AP(s), compared over a matched window so the
    # baseline-wifit3-vs-baseline-linux dwell asymmetry doesn't masquerade as a driver
    # gap. 0.3/s tolerance = sampling noise, not a real deficit. --ref pins one or more
    # stable reference BSSIDs (e.g. a fixed AP per band); default is the single AP linux
    # heard most, which can drift to a different transient AP run-to-run.
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

    # RSSI agreement on common BSSIDs (same card => a consistent gap is a decode bug).
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
    p = argparse.ArgumentParser(description="Diff two health rollups (wifit3 vs linux).")
    p.add_argument("--diff", nargs=2, metavar=("WIFIT3_JSON", "LINUX_JSON"), required=True)
    p.add_argument("--ref", nargs="+", metavar="BSSID", default=None,
                   help="Pin one or more reference BSSIDs for the beacon-rate line "
                        "(e.g. a fixed AP per band). Default: the single AP linux heard most.")
    args = p.parse_args()
    diff(args.diff[0], args.diff[1], ref_bssids=args.ref)
    return 0


if __name__ == "__main__":
    sys.exit(main())
