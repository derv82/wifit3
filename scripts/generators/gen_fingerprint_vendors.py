#!/usr/bin/env python3
"""Regenerate ``src/wifit3/wlan/fingerprint_vendors.py`` from the IEEE OUI registry.

Client fingerprinting's low-confidence tier (wlan/fingerprint.py) names a client's vendor
without claiming a specific device class -- unlike gen_router_ouis.py (which filters the ~40k-row
registry down to ~300 router-brand prefixes for a narrow purpose), this keeps every MA-L
organization: broad "who made this" coverage is the entire point of the low-confidence tier, so
there's no vendor allowlist to filter against. Organization names are cleaned up (corporate
suffixes stripped, ALL-CAPS title-cased) so the popup shows "Samsung Electronics", not
"SAMSUNG ELECTRONICS CO.,LTD".

Usage:
    uv run python scripts/generators/gen_fingerprint_vendors.py            # download a fresh registry
    uv run python scripts/generators/gen_fingerprint_vendors.py oui.csv    # use a local IEEE csv
"""
from __future__ import annotations

import csv
import io
import re
import sys
import urllib.request
from pathlib import Path

_IEEE_CSV = "https://standards-oui.ieee.org/oui/oui.csv"

_OUT = (Path(__file__).resolve().parents[2]
        / "src" / "wifit3" / "wlan" / "fingerprint_vendors.py")

# Stripped iteratively (some names stack two, e.g. "X Technology Co., Ltd."), so each pattern
# only needs to match one trailing suffix at a time. The second line's extras (JSC/OAO/ZAO/OOO
# and "closed/open joint stock company", both common on Russian/CIS registrants; "systems";
# "holding") are ported from Wireshark's make-manuf.py suffix list (GPLv2), which catches more
# than our original set did.
_SUFFIX_RE = re.compile(
    r",?\s*("
    r"incorporated|inc\.?|llc\.?|ltd\.?|co\.,?\s*ltd\.?|corporation|corp\.?|gmbh|"
    r"s\.a\.?|s\.p\.a\.?|b\.v\.?|pty\.?\s*ltd\.?|pvt\.?\s*ltd\.?|ag|company|limited|"
    r"closed joint stock company|open joint stock company|jsc|oao|zao|ooo|systems|holding"
    r")\.?\s*$",
    re.IGNORECASE,
)

# Corrections that don't fit a regex: .title() mangles capitalization around "&" and inside
# established acronyms/brand names since it can't tell a deliberate acronym from ALL-CAPS
# shouting, and a name mixing scripts (Chinese + a parenthetical English translation) isn't
# something _SUFFIX_RE's Latin-only patterns touch at all. "Advanced Micro Devices" -> "AMD"
# and the Chinese entry are ported from Wireshark's make-manuf.py special_case dict (GPLv2);
# the AT&T/Samsung casing fixes are ours, same underlying problem as the AMD one.
_SPECIAL_CASE = {
    "At&T": "AT&T",
    "Samsung Electronics": "SAMSUNG Electronics",
    "Advanced Micro Devices": "AMD",
    "杭州德澜科技有限公司（HangZhou Delan Technology Co.,Ltd）": "DelanTech",
}

# IEEE further subdivides some MA-L (OUI-24) blocks into per-organization OUI-28/OUI-36
# allocations; the MA-L record itself just says this, naming no real vendor. Not useful to
# fingerprinting (it would mislabel every device in the block with whichever nibble it doesn't
# actually belong to), so these are dropped rather than mapped to a name.
_NOT_A_VENDOR = {"IEEE Registration Authority"}

# Chinese registrants often lead with the city/province, which _SUFFIX_RE (trailing-only) can't
# touch. Ported from Wireshark's make-manuf.py (GPLv2), non-exhaustive by their own admission;
# strips just the leading word, same as they do, not the whole name.
_SKIP_START = {
    "shengzen", "shenzhen", "beijing", "shanghai", "wuhan", "hangzhou", "guangxi",
    "guangdong", "chengdu",
}


def _clean_name(org: str) -> str:
    name = org.strip().strip('"').strip()
    # The IEEE registry is free text: a handful of orgs registered trademark glyphs or an
    # em-dash into their own name (e.g. "Planet Bingo® — 3rd Rock Gaming®"), which the
    # project's own em-dash ban would otherwise trip on generated output.
    name = name.replace("—", "-").replace("–", "-")
    name = name.replace("®", "").replace("™", "")
    words = name.split()
    if len(words) > 1 and words[0].lower() in _SKIP_START:
        name = " ".join(words[1:])
    prev = None
    while prev != name and name:
        prev = name
        name = _SUFFIX_RE.sub("", name).strip()
    if not name:
        return org.strip()
    cleaned = name.title() if name.isupper() else name
    return _SPECIAL_CASE.get(cleaned, cleaned)


def _load(source: str | None) -> str:
    if source:
        return Path(source).read_text(encoding="utf-8", errors="replace")
    print(f"Downloading {_IEEE_CSV} ...")
    with urllib.request.urlopen(_IEEE_CSV, timeout=120) as r:  # noqa: S310 (fixed IEEE https host)
        return r.read().decode("utf-8", "replace")


def main(source: str | None = None) -> None:
    mapping: dict[str, str] = {}
    for row in csv.reader(io.StringIO(_load(source))):
        if len(row) < 3 or row[0] != "MA-L":          # header + MA-M/MA-S rows skipped
            continue
        oui = row[1].strip().upper()
        if len(oui) != 6 or not all(c in "0123456789ABCDEF" for c in oui):
            continue
        name = _clean_name(row[2])
        if name in _NOT_A_VENDOR:
            continue
        mapping[oui] = name

    header = (
        '"""OUI -> cleaned vendor name, for client fingerprinting\'s low-confidence tier\n'
        "(wlan/fingerprint.py): every registered vendor, since naming who made a device --\n"
        "without claiming which kind -- is the entire point of that tier.\n\n"
        "GENERATED by scripts/generators/gen_fingerprint_vendors.py from the IEEE OUI registry\n"
        '(standards-oui.ieee.org). Do NOT edit by hand; rerun the script to refresh.\n"""\n\n'
    )
    body = "VENDOR_BY_OUI = {\n"
    body += "".join(f'    "{o}": {mapping[o]!r},\n' for o in sorted(mapping))
    body += "}\n"
    _OUT.write_text(header + body, encoding="utf-8")
    print(f"Wrote {len(mapping)} OUIs -> {_OUT}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
