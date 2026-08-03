"""The baseline rollup: collect a beacon stream into one card's summary, plus reference-AP loading.

A "rollup" is the compact JSON summary of one baseline run. For each channel, and each BSSID heard
on it: how many beacons arrived, the per-second arrival counts, mean RSSI, and the channel the
beacon advertised. ``Health`` builds a rollup from a fed beacon stream; ``baseline_diff`` reads two
rollups (our driver vs Linux) and compares them.

Both collectors feed the SAME ``Health.feed``, so a stream from our driver and one from a Linux pcap
group identically: any difference in the rollup is the driver or the RF, not the aggregation.

Reference APs (the fixed 2.4 / 5 GHz beacon sources the beacon-rate line pins to) load from
``driver_sources/reference_aps.txt`` (gitignored, since BSSIDs never enter git), overridable with
``--bssid2g/--channel2g/--bssid5g/--channel5g`` (same flag names as capture.py).
"""
from __future__ import annotations

import json
import shutil
import statistics
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wifit3.dot11.packet import Packet

_DATA = Path(__file__).resolve().parent.parent.parent / "driver_sources"
_REF_FILE = _DATA / "reference_aps.txt"


class Health:
    """Accumulates a fed beacon stream into a rollup (see module docstring) and writes it to JSON."""

    def __init__(self, chip: str, source: str) -> None:
        self.chip = chip
        self.source = source  # "wifit3" | "linux"
        # channel -> bssid -> {beacons, rssi[list], secs{sec:count}, adv_channel}
        self._ch: dict[int, dict[str, dict]] = {}
        self._t0: dict[int, float] = {}

    def feed(self, ts: float, parsed: "Packet | None", rssi: int | None, channel: int) -> None:
        """Fold one received frame into the running per-channel / per-BSSID tally (beacons only)."""
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
        """Collapse the tally into the JSON summary: per channel its dwell span, and per BSSID the
        beacon count, beacons/sec, mean RSSI, advertised channel, and per-second counts."""
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
        """Write the rollup to ``path``, first archiving any prior file at that path into history/."""
        path = Path(path)
        _archive(path)
        path.write_text(json.dumps(self.rollup(), indent=2), encoding="utf-8")
        print(f"[+] wrote {path}", file=sys.stderr)


def _archive(path: Path) -> None:
    """Copy an existing rollup into history/ (gitignored) before it is overwritten, stamped with the
    file's own mtime, so re-running a card keeps its fixed filename yet loses no prior run."""
    if not path.exists():
        return
    hist = path.parent / "history"
    hist.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(path.stat().st_mtime))
    shutil.copy2(path, hist / f"{path.stem}-{stamp}.json")


def add_reference_args(parser) -> None:
    """Add the reference-AP override flags (same names as capture.py) to an argparse parser."""
    parser.add_argument("--bssid2g", default=None,
                        help="2.4 GHz reference AP BSSID (pins the A/B beacon-rate line).")
    parser.add_argument("--channel2g", type=int, default=None, help="channel for --bssid2g.")
    parser.add_argument("--bssid5g", default=None,
                        help="5 GHz reference AP BSSID (pins the A/B beacon-rate line).")
    parser.add_argument("--channel5g", type=int, default=None, help="channel for --bssid5g.")


def _load_ref_file() -> dict:
    out: dict = {}
    if not _REF_FILE.exists():
        return out
    for line in _REF_FILE.read_text().splitlines():
        if line.strip().startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip().lower()] = v.strip()
    return out


def load_reference_aps(args=None) -> dict:
    """``{"2.4": {"bssid","channel"}, "5": {...}}`` from reference_aps.txt, CLI-overridden;
    a band is present only if it has a BSSID."""
    f = _load_ref_file()

    def pick(cli_attr: str, file_key: str):
        val = getattr(args, cli_attr, None) if args is not None else None
        return val if val not in (None, "") else f.get(file_key)

    refs: dict = {}
    b2, c2 = pick("bssid2g", "bssid2g"), pick("channel2g", "channel2g")
    b5, c5 = pick("bssid5g", "bssid5g"), pick("channel5g", "channel5g")
    if b2:
        refs["2.4"] = {"bssid": b2.lower(), "channel": int(c2) if c2 else 1}
    if b5:
        refs["5"] = {"bssid": b5.lower(), "channel": int(c5) if c5 else 36}
    return refs


def ref_channels(refs: dict) -> list[int]:
    return [r["channel"] for r in refs.values()]


def ref_bssids(refs: dict) -> list[str]:
    return [r["bssid"] for r in refs.values()]
