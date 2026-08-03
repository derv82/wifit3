"""Reference-AP loading for the baseline scripts.

The fixed 2.4 / 5 GHz beacon sources the A/B pins its beacon-rate line to. BSSIDs never enter git,
so they load from ``driver_sources/reference_aps.txt`` (gitignored), with
``--bssid2g/--channel2g/--bssid5g/--channel5g`` overriding (same flag names as capture.py).
"""
from __future__ import annotations

from pathlib import Path

_DATA = Path(__file__).resolve().parent.parent.parent / "driver_sources"
_REF_FILE = _DATA / "reference_aps.txt"


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
